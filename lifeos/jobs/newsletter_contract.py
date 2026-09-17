"""Career's public contract for Mail/Newsletter ingestion.

Every NormalizedCandidate Newsletter hands to Career receives exactly one
terminal Disposition. There is no silent drop: a candidate this module
cannot confidently resolve becomes REVIEW_DEGRADED, never an omission.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import Enum

from lifeos.core.runtime import RunContext
from lifeos.jobs.dedupe import LaneObservation, reconcile
from lifeos.jobs.identity import IdentityCollision, derive_identity_evidence, resolve_existing_identity, stable_job_key
from lifeos.jobs.lifecycle import apply_observation, new_record
from lifeos.jobs.models import AdmissionStatus, NormalizedCandidate
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
    """Resolve identity, qualify, converge duplicates, and persist serially.

    Returns exactly one result per input candidate in input order. When a
    RunContext is supplied, no canonical mutation begins after its deadline.
    """
    results: list[IngestResult | None] = [None] * len(candidates)
    observations: list[LaneObservation] = []
    live_by_index: dict[int, tuple[NormalizedCandidate, str]] = {}
    primary_index_for_key: dict[str, int] = {}
    evidence_by_index = {}
    candidate_stable_keys: list[str] = []
    candidate_apply_urls: list[str] = []

    for i, candidate in enumerate(candidates):
        if candidate.unresolved_reason:
            results[i] = IngestResult(
                evidence_ref=candidate.evidence_ref,
                disposition=Disposition.REVIEW_DEGRADED,
                stable_job_key=None,
                detail=candidate.unresolved_reason,
            )
            continue

        evidence = derive_identity_evidence(candidate.job)
        if not evidence.stable_job_keys:
            results[i] = IngestResult(
                candidate.evidence_ref,
                Disposition.REVIEW_DEGRADED,
                None,
                "cannot derive stable Job identity without canonical_identity, a canonical apply URL, or company+role+location",
            )
            continue

        try:
            tentative_key = stable_job_key(candidate.job)
        except ValueError as exc:
            results[i] = IngestResult(candidate.evidence_ref, Disposition.REVIEW_DEGRADED, None, str(exc))
            continue

        try:
            qualification = qualify(candidate, lane=lane, run_date=run_date)
        except Exception as exc:
            results[i] = IngestResult(
                candidate.evidence_ref,
                Disposition.REVIEW_DEGRADED,
                tentative_key,
                f"qualification error: {exc}",
            )
            continue

        if qualification.admission_status == AdmissionStatus.EXCLUDED:
            results[i] = IngestResult(
                candidate.evidence_ref,
                Disposition.EXCLUDED,
                tentative_key,
                qualification.review_reason,
            )
            continue

        evidence_by_index[i] = evidence
        candidate_stable_keys.extend(evidence.stable_job_keys)
        candidate_apply_urls.extend(evidence.canonical_apply_urls)

    records_by_stable_key = {}
    records_by_apply_url = {}
    if evidence_by_index:
        lookup_failed = False
        try:
            records_by_stable_key = repository.get_many(list(dict.fromkeys(candidate_stable_keys)))
        except Exception as exc:
            lookup_failed = True
            for i in evidence_by_index:
                candidate = candidates[i]
                results[i] = IngestResult(
                    candidate.evidence_ref,
                    Disposition.REVIEW_DEGRADED,
                    None,
                    f"repository stable-key lookup failed: {type(exc).__name__}: {exc}",
                )
        if not lookup_failed:
            try:
                records_by_apply_url = repository.get_by_apply_urls(list(dict.fromkeys(candidate_apply_urls)))
            except Exception as exc:
                for i in evidence_by_index:
                    candidate = candidates[i]
                    results[i] = IngestResult(
                        candidate.evidence_ref,
                        Disposition.REVIEW_DEGRADED,
                        None,
                        f"repository apply-url lookup failed: {type(exc).__name__}: {exc}",
                    )

    existing_records = dict(records_by_stable_key)
    for record in records_by_apply_url.values():
        existing_records[record.opportunity.stable_job_key] = record

    for i, candidate in enumerate(candidates):
        if results[i] is not None:
            continue

        evidence = evidence_by_index[i]
        try:
            existing_key = resolve_existing_identity(
                evidence,
                records_by_stable_key=records_by_stable_key,
                records_by_apply_url=records_by_apply_url,
            )
        except IdentityCollision as exc:
            results[i] = IngestResult(candidate.evidence_ref, Disposition.REVIEW_DEGRADED, None, str(exc))
            continue

        if existing_key:
            job = replace(candidate.job, canonical_identity=existing_key)
            candidate = replace(candidate, job=job)

        key = stable_job_key(candidate.job)
        qualification = qualify(candidate, lane=lane, run_date=run_date)

        observations.append(
            LaneObservation(
                stable_job_key=key,
                lane=lane.name,
                job=candidate.job,
                fit=candidate.fit,
                fit_authority=candidate.fit_authority,
                admission_status=qualification.admission_status,
            )
        )
        live_by_index[i] = (candidate, key)
        primary_index_for_key.setdefault(key, i)

    if not observations:
        return results  # type: ignore[return-value]

    reconciled = reconcile(observations, lane_priority=lane_priority)

    for reconciled_opportunity in reconciled:
        key = reconciled_opportunity.opportunity.stable_job_key
        same_key_indices = [i for i, (_, observed_key) in live_by_index.items() if observed_key == key]
        primary_index = primary_index_for_key[key]
        primary_candidate = live_by_index[primary_index][0]

        if context is not None and context.expired():
            for i in same_key_indices:
                candidate, _ = live_by_index[i]
                results[i] = IngestResult(
                    candidate.evidence_ref,
                    Disposition.REVIEW_DEGRADED,
                    key,
                    "execution deadline exhausted before canonical mutation",
                )
            continue

        existing = existing_records.get(key)
        try:
            if existing is None:
                record = new_record(reconciled_opportunity.opportunity, run_date=run_date)
                persisted = repository.upsert(record)
                primary_disposition = Disposition.CREATED
            else:
                record = apply_observation(existing, reconciled_opportunity.opportunity, run_date=run_date)
                persisted = repository.upsert(record)
                primary_disposition = Disposition.UPDATED
        except Exception as exc:
            label = "read-back mismatch" if isinstance(exc, ReadBackMismatch) else f"repository failure ({type(exc).__name__})"
            for i in same_key_indices:
                candidate, _ = live_by_index[i]
                results[i] = IngestResult(
                    candidate.evidence_ref,
                    Disposition.REVIEW_DEGRADED,
                    key,
                    f"persistence {label}: {exc}",
                )
            continue

        for i in same_key_indices:
            candidate, _ = live_by_index[i]
            if i == primary_index:
                results[i] = IngestResult(
                    candidate.evidence_ref,
                    primary_disposition,
                    persisted.opportunity.stable_job_key,
                )
            else:
                results[i] = IngestResult(
                    candidate.evidence_ref,
                    Disposition.DUPLICATE,
                    persisted.opportunity.stable_job_key,
                    f"duplicate of evidence {primary_candidate.evidence_ref}; provenance merged into the canonical reconciliation",
                )

    assert all(result is not None for result in results), "every input candidate must receive exactly one result"
    return results  # type: ignore[return-value]
