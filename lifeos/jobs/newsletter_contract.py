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
) -> list[IngestResult]:
    """Resolve identity, qualify, converge duplicates within this batch,
    and idempotently persist. Returns exactly one IngestResult per input
    candidate, in input order.
    """
    results: list[IngestResult] = []
    resolved: list[tuple[NormalizedCandidate, str]] = []

    for candidate in candidates:
        try:
            key = stable_job_key(candidate.job)
        except ValueError as exc:
            results.append(
                IngestResult(
                    evidence_ref=candidate.evidence_ref,
                    disposition=Disposition.REVIEW_DEGRADED,
                    stable_job_key=None,
                    detail=str(exc),
                )
            )
            continue
        resolved.append((candidate, key))

    # Within-batch duplicate detection happens before qualification so a
    # weaker duplicate observation never masks a stronger one's disposition.
    seen_in_batch: dict[str, str] = {}

    observations: list[LaneObservation] = []
    observation_index: dict[str, tuple[NormalizedCandidate, str]] = {}

    for candidate, key in resolved:
        if key in seen_in_batch:
            results.append(
                IngestResult(
                    evidence_ref=candidate.evidence_ref,
                    disposition=Disposition.DUPLICATE,
                    stable_job_key=key,
                    detail=f"duplicate of evidence {seen_in_batch[key]} in this batch",
                )
            )
            continue
        seen_in_batch[key] = candidate.evidence_ref

        try:
            result = qualify(candidate, lane=lane, run_date=run_date)
        except Exception as exc:  # qualification must never crash the batch
            results.append(
                IngestResult(
                    evidence_ref=candidate.evidence_ref,
                    disposition=Disposition.REVIEW_DEGRADED,
                    stable_job_key=key,
                    detail=f"qualification error: {exc}",
                )
            )
            continue

        if result.admission_status == AdmissionStatus.EXCLUDED:
            results.append(
                IngestResult(
                    evidence_ref=candidate.evidence_ref,
                    disposition=Disposition.EXCLUDED,
                    stable_job_key=key,
                    detail=result.review_reason,
                )
            )
            continue

        obs = LaneObservation(
            stable_job_key=key,
            lane=lane.name,
            job=candidate.job,
            fit=candidate.fit or 0,
            admission_status=result.admission_status,
        )
        observations.append(obs)
        observation_index[key] = (candidate, key)

    if not observations:
        return results

    reconciled = reconcile(observations, lane_priority=lane_priority)
    existing_records = repository.get_many([r.opportunity.stable_job_key for r in reconciled])

    for r in reconciled:
        candidate, key = observation_index[r.opportunity.stable_job_key]
        existing = existing_records.get(key)
        try:
            if existing is None:
                record = new_record(r.opportunity, run_date=run_date)
                persisted = repository.upsert(record)
                disposition = Disposition.CREATED
            else:
                record = apply_observation(existing, r.opportunity, run_date=run_date)
                persisted = repository.upsert(record)
                disposition = Disposition.UPDATED
        except ReadBackMismatch as exc:
            results.append(
                IngestResult(
                    evidence_ref=candidate.evidence_ref,
                    disposition=Disposition.REVIEW_DEGRADED,
                    stable_job_key=key,
                    detail=f"persistence read-back mismatch: {exc}",
                )
            )
            continue

        results.append(
            IngestResult(
                evidence_ref=candidate.evidence_ref,
                disposition=disposition,
                stable_job_key=persisted.opportunity.stable_job_key,
            )
        )

    return results
