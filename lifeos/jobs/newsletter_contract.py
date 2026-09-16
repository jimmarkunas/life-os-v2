"""Career's public contract for Mail/Newsletter (Agent 2) ingestion.

Every NormalizedCandidate Newsletter hands to Career receives exactly one
terminal Disposition. There is no silent drop: a candidate this module
cannot confidently resolve becomes REVIEW_DEGRADED, never an omission from
the result list.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from lifeos.core.runtime import RunContext
from lifeos.jobs.dedupe import LaneObservation, reconcile
from lifeos.jobs.identity import stable_job_key
from lifeos.jobs.lifecycle import apply_observation, new_record
from lifeos.jobs.models import AdmissionStatus, NormalizedCandidate, Opportunity
from lifeos.jobs.qualification import LaneConfig, qualify
from lifeos.jobs.repository import CareerRepository, ReadBackMismatch


class Disposition(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    DUPLICATE = "duplicate"
    EXCLUDED = "excluded"
    REVIEW_DEGRADED = "review_degraded"


@dataclass(frozen=True)
class IngestResult:
    evidence_ref: str
    disposition: Disposition
    stable_job_key: str | None
    detail: str | None = None


def ingest(
    candidates: list[NormalizedCandidate],
    *,
    lane: LaneConfig,
    lane_priority: dict[str, int],
    repository: CareerRepository,
    run_date: date,
    context: RunContext | None = None,
) -> list[IngestResult]:
    """Resolve identity, qualify, converge duplicates, and idempotently
    persist. Returns exactly one IngestResult per input candidate, in input
    order (see the `results` pre-sizing below -- every branch writes to
    `results[i]` by original index, so grouping/reordering downstream can
    never desynchronize the input<->output correspondence).

    Duplicate determination happens AFTER every same-key observation has
    already contributed to reconcile() -- provenance, the strongest
    available canonical URL, and provider aliases are merged from ALL
    same-key observations before any of them is labeled a duplicate. At
    most one canonical mutation is written per stable_job_key; every other
    same-key observation is reported DUPLICATE but its evidence was not
    discarded -- it already shaped the persisted canonical Opportunity.

    Canonical mutations (repository.upsert()) remain strictly serialized --
    this loop never parallelizes them. When `context` is supplied, this
    function also checks it has time remaining before beginning EACH
    canonical mutation. Once the deadline is exhausted, no further upsert()
    begins: every still-unprocessed same-key group becomes REVIEW_DEGRADED,
    already-verified writes from earlier in the loop are left untouched,
    and every input still receives exactly one terminal disposition.
    """
    results: list[IngestResult | None] = [None] * len(candidates)
    observations: list[LaneObservation] = []
    # input index -> the candidate/key that produced its (still-live) observation
    live_by_index: dict[int, tuple[NormalizedCandidate, str]] = {}
    # stable_job_key -> the lowest input index observed for it (deterministic
    # "first source" -- see BLOCKER 2's example: first -> CREATED/UPDATED,
    # every later same-key observation -> DUPLICATE).
    primary_index_for_key: dict[str, int] = {}

    for i, candidate in enumerate(candidates):
        try:
            key = stable_job_key(candidate.job)
        except ValueError as exc:
            results[i] = IngestResult(
                evidence_ref=candidate.evidence_ref,
                disposition=Disposition.REVIEW_DEGRADED,
                stable_job_key=None,
                detail=str(exc),
            )
            continue

        try:
            result = qualify(candidate, lane=lane, run_date=run_date)
        except Exception as exc:  # qualification must never crash the batch
            results[i] = IngestResult(
                evidence_ref=candidate.evidence_ref,
                disposition=Disposition.REVIEW_DEGRADED,
                stable_job_key=key,
                detail=f"qualification error: {exc}",
            )
            continue

        if result.admission_status == AdmissionStatus.EXCLUDED:
            results[i] = IngestResult(
                evidence_ref=candidate.evidence_ref,
                disposition=Disposition.EXCLUDED,
                stable_job_key=key,
                detail=result.review_reason,
            )
            continue

        observations.append(
            LaneObservation(
                stable_job_key=key,
                lane=lane.name,
                job=candidate.job,
                fit=candidate.fit or 0,
                admission_status=result.admission_status,
            )
        )
        live_by_index[i] = (candidate, key)
        primary_index_for_key.setdefault(key, i)

    if not observations:
        return results  # type: ignore[return-value]  # every slot filled above

    reconciled = reconcile(observations, lane_priority=lane_priority)
    existing_records = repository.get_many([r.opportunity.stable_job_key for r in reconciled])

    for r in reconciled:
        key = r.opportunity.stable_job_key
        same_key_indices = [i for i, (_, k) in live_by_index.items() if k == key]
        primary_index = primary_index_for_key[key]
        primary_candidate = live_by_index[primary_index][0]

        if context is not None and context.expired():
            # Execution deadline exhausted -- do not begin another canonical
            # mutation. Already-verified writes from earlier in this loop
            # are untouched; every still-unprocessed same-key observation
            # becomes REVIEW_DEGRADED rather than a silent drop.
            for i in same_key_indices:
                cand, _ = live_by_index[i]
                results[i] = IngestResult(
                    evidence_ref=cand.evidence_ref,
                    disposition=Disposition.REVIEW_DEGRADED,
                    stable_job_key=key,
                    detail="execution deadline exhausted before canonical mutation",
                )
            continue

        existing = existing_records.get(key)
        try:
            if existing is None:
                record = new_record(r.opportunity, run_date=run_date)
                persisted = repository.upsert(record)
                primary_disposition = Disposition.CREATED
            else:
                record = apply_observation(existing, r.opportunity, run_date=run_date)
                persisted = repository.upsert(record)
                primary_disposition = Disposition.UPDATED
        except ReadBackMismatch as exc:
            # The one attempted canonical mutation for this key failed its
            # read-back; every same-key observation is unresolved, not just
            # the primary one -- report all of them REVIEW_DEGRADED.
            for i in same_key_indices:
                cand, _ = live_by_index[i]
                results[i] = IngestResult(
                    evidence_ref=cand.evidence_ref,
                    disposition=Disposition.REVIEW_DEGRADED,
                    stable_job_key=key,
                    detail=f"persistence read-back mismatch: {exc}",
                )
            continue

        for i in same_key_indices:
            cand, _ = live_by_index[i]
            if i == primary_index:
                results[i] = IngestResult(
                    evidence_ref=cand.evidence_ref,
                    disposition=primary_disposition,
                    stable_job_key=persisted.opportunity.stable_job_key,
                )
            else:
                results[i] = IngestResult(
                    evidence_ref=cand.evidence_ref,
                    disposition=Disposition.DUPLICATE,
                    stable_job_key=persisted.opportunity.stable_job_key,
                    detail=(
                        f"duplicate of evidence {primary_candidate.evidence_ref}; "
                        "provenance merged into the canonical reconciliation"
                    ),
                )

    assert all(result is not None for result in results), "every input candidate must receive exactly one result"
    return results  # type: ignore[return-value]
