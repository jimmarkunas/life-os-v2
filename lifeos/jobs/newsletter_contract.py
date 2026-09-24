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
from lifeos.jobs.identity import IdentityCollision, canonical_url, derive_identity_evidence, resolve_existing_identity, stable_job_key
from lifeos.jobs.lifecycle import apply_observation, new_record
from lifeos.jobs.models import AdmissionStatus, FitAuthority, NormalizedCandidate
from lifeos.newsletter.models import SourceVacancyObservation
from lifeos.jobs.qualification import LaneConfig, QualificationResult, qualify
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
    diagnostic: dict | None = None
    persistence_verified: bool = False
    terminal_evidence_satisfied: bool = False
    terminal_evidence_diagnostics: tuple[str, ...] = ()


def result_is_accounted(result: IngestResult | None) -> bool:
    """A degraded result is complete only after its canonical mutation read-back."""
    return result is not None and (
        result.disposition is not Disposition.REVIEW_DEGRADED
        or result.persistence_verified
    )


def derive_review_these_jobs(observations: list[SourceVacancyObservation], candidates: list[NormalizedCandidate], results: list[IngestResult]) -> list[dict[str, object]]:
    candidates_by_ref = {item.evidence_ref: item for item in candidates}
    results_by_ref = {item.evidence_ref: item for item in results}
    rows: dict[tuple[str, str], dict[str, object]] = {}
    first_seen = {}
    for observation in observations:
        result = results_by_ref.get(observation.evidence_ref)
        if result is None or result.disposition is not Disposition.REVIEW_DEGRADED:
            continue
        candidate = candidates_by_ref.get(observation.evidence_ref)
        evidence = candidate.fit_evidence_kind.value if candidate and candidate.fit_evidence_kind.value != "none" else None
        key = (observation.source_provider, observation.provider_job_id or observation.source_apply_url or observation.evidence_ref)
        surfaced = observation.source_received_at
        if key in first_seen and (surfaced is None or (first_seen[key] is not None and surfaced >= first_seen[key])):
            continue
        if surfaced is not None or key not in first_seen:
            first_seen[key] = surfaced
        available = (["job_title"] if observation.role else []) + (["source_description"] if observation.source_description_text else []) + ([evidence] if evidence else [])
        missing = ["employer_ats_jd"] if evidence != "employer_ats_jd" else []
        if candidate and getattr(candidate, "fit", None) is None and getattr(candidate, "fit_reason", None) == "missing_scoreable_jd_requirements":
            missing.append("scoreable_jd_requirements")
        rows[key] = {"Role": observation.role, "Company": observation.company, "Source": observation.source_provider, "Apply URL": (candidate.job.apply_url if candidate else None) or observation.source_apply_url, "Review Reason": result.detail, "Evidence Available": available, "Evidence Missing": missing, "First Surfaced": surfaced.isoformat() if surfaced else None, "Retry Status": "retryable"}
    return [rows[key] for key in sorted(rows)]


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
    qualification_by_index: dict[int, QualificationResult] = {}
    qualification_errors: dict[int, str] = {}

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
            results[i] = IngestResult(candidate.evidence_ref, Disposition.REVIEW_DEGRADED, None, str(exc), {"company": candidate.job.company.name, "role": candidate.job.role, "location": candidate.job.location, "source": candidate.job.source_provider, "provider_job_id": candidate.job.provider_job_id, "apply_url": candidate.job.apply_url, "missing": [name for name, value in (("company", candidate.job.company.name), ("role", candidate.job.role), ("location", candidate.job.location), ("apply_url", candidate.job.apply_url)) if not value]})
            continue

        evidence_by_index[i] = evidence
        candidate_stable_keys.extend(evidence.stable_job_keys)
        candidate_apply_urls.extend(evidence.canonical_apply_urls)

    records_by_stable_key = {}
    records_by_apply_url = {}
    if evidence_by_index:
        queried_stable_keys = set(candidate_stable_keys)
        lookup_failed = False
        try:
            known = getattr(repository, "known", None)
            records_by_stable_key = (
                known(list(dict.fromkeys(candidate_stable_keys)))
                if callable(known) else {}
            )
            unresolved_indices = {
                i for i, evidence in evidence_by_index.items()
                if not any(key in records_by_stable_key for key in evidence.stable_job_keys)
            }
            unresolved_keys = [
                key for i in unresolved_indices for key in evidence_by_index[i].stable_job_keys
            ]
            if unresolved_keys:
                records_by_stable_key.update(
                    repository.get_many(list(dict.fromkeys(unresolved_keys)))
                )
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
                unresolved_urls = [
                    url
                    for i in evidence_by_index
                    for url in evidence_by_index[i].canonical_apply_urls
                    if not any(key in records_by_stable_key for key in evidence_by_index[i].stable_job_keys)
                    if f"url:{canonical_url(url)}" not in queried_stable_keys
                ]
                if unresolved_urls:
                    records_by_apply_url = repository.get_by_apply_urls(list(dict.fromkeys(unresolved_urls)))
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
        existing_records[record.job.stable_job_key] = record

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
        try:
            qualification = qualify(candidate, lane=lane, run_date=run_date)
        except Exception as exc:
            qualification = QualificationResult(AdmissionStatus.PASSED_REVIEW, "Fit evaluation pending", candidate.freshness_status)
            qualification_errors[i] = f"evaluation pending: qualification error: {exc}"
        qualification_by_index[i] = qualification

        observations.append(
            LaneObservation(
                stable_job_key=key,
                lane=lane.name,
                job_observation=candidate.job,
                fit=candidate.fit,
                fit_authority=candidate.fit_authority,
                source_types=candidate.source_types,
                eligible_lanes=(lane.name,)
                if qualification.admission_status is AdmissionStatus.ADMITTED and i not in qualification_errors
                else (),
                admission_status=qualification.admission_status,
            )
        )
        live_by_index[i] = (candidate, key)
        primary_index_for_key.setdefault(key, i)

    if not observations:
        return results  # type: ignore[return-value]

    reconciled = reconcile(observations, lane_priority=lane_priority)

    pending = []
    for reconciled_job in reconciled:
        key = reconciled_job.job.stable_job_key
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
                record = new_record(reconciled_job.job, run_date=run_date)
                primary_disposition = Disposition.CREATED
            else:
                record = apply_observation(
                    existing,
                    reconciled_job.job,
                    run_date=run_date,
                    lane_priority=lane_priority,
                )
                primary_disposition = Disposition.UPDATED
        except Exception as exc:
            label = "read-back mismatch" if isinstance(exc, ReadBackMismatch) else f"repository failure ({type(exc).__name__})"
            for i in same_key_indices:
                candidate, _ = live_by_index[i]
                results[i] = IngestResult(candidate.evidence_ref, Disposition.REVIEW_DEGRADED, key, f"persistence {label}: {exc}", {"mismatches": getattr(exc, "mismatches", []), "error_type": type(exc).__name__, "operation": getattr(exc, "operation", None), "retry_limit": getattr(exc, "retry_limit", None), "attempts": getattr(exc, "attempts", None), "category": getattr(getattr(exc, "kind", None), "value", None), "retry_after_seconds": getattr(exc, "retry_after_seconds", None)})
            continue

        pending.append((key, record, same_key_indices, primary_index, primary_candidate, primary_disposition))
    persisted_items = []
    if pending:
        try:
            batch_upsert = getattr(repository, "upsert_many", None)
            persisted_by_key = batch_upsert([item[1] for item in pending]) if callable(batch_upsert) else {item[0]: repository.upsert(item[1]) for item in pending}
            persisted_items = [(*item[:1], persisted_by_key[item[0]], *item[2:]) for item in pending]
        except Exception as exc:
            label = "read-back mismatch" if isinstance(exc, ReadBackMismatch) else f"repository failure ({type(exc).__name__})"
            for key, _record, same_key_indices, _primary_index, _primary_candidate, _disposition in pending:
                for i in same_key_indices:
                    candidate = candidates[i]
                    results[i] = IngestResult(candidate.evidence_ref, Disposition.REVIEW_DEGRADED, key, f"persistence {label}: {exc}", {"mismatches": getattr(exc, "mismatches", []), "error_type": type(exc).__name__, "operation": getattr(exc, "operation", None), "retry_limit": getattr(exc, "retry_limit", None), "attempts": getattr(exc, "attempts", None), "category": getattr(getattr(exc, "kind", None), "value", None), "retry_after_seconds": getattr(exc, "retry_after_seconds", None)})
    for key, persisted, same_key_indices, primary_index, primary_candidate, primary_disposition in persisted_items:
        for i in same_key_indices:
            candidate, _ = live_by_index[i]
            qualification = qualification_by_index[i]
            evaluation_pending = qualification_errors.get(i) or (
                f"evaluation pending: {candidate.fit_reason or qualification.review_reason}" if candidate.fit is None else None
            )
            _tes = bool(
                persisted.job.fit is not None
                and persisted.job.fit_authority == FitAuthority.AUTHORITATIVE
                and persisted.job.job.apply_url is not None
                and i not in qualification_errors
            )
            _diagnostics = tuple(
                name for name, missing in (
                    ("missing_fit", persisted.job.fit is None),
                    ("non_authoritative_fit", persisted.job.fit is not None and persisted.job.fit_authority != FitAuthority.AUTHORITATIVE),
                    ("missing_apply_url", persisted.job.job.apply_url is None),
                    ("qualification_error", i in qualification_errors),
                ) if missing
            )
            if evaluation_pending:
                results[i] = IngestResult(candidate.evidence_ref, Disposition.REVIEW_DEGRADED, persisted.job.stable_job_key, evaluation_pending if isinstance(evaluation_pending, str) else "evaluation pending", {"company": candidate.job.company.name, "role": candidate.job.role, "source": candidate.job.source_provider, "fit_evidence": candidate.fit_evidence_kind.value}, True, _tes, _diagnostics)
                continue
            if i == primary_index:
                results[i] = IngestResult(
                    candidate.evidence_ref,
                    Disposition.EXCLUDED if qualification.admission_status is AdmissionStatus.EXCLUDED else primary_disposition,
                    persisted.job.stable_job_key,
                    qualification.review_reason if qualification.admission_status is AdmissionStatus.EXCLUDED else None,
                    None,
                    True,
                    _tes,
                    _diagnostics,
                )
            else:
                results[i] = IngestResult(
                    candidate.evidence_ref,
                    Disposition.DUPLICATE,
                    persisted.job.stable_job_key,
                    f"duplicate of evidence {primary_candidate.evidence_ref}; provenance merged into the canonical reconciliation",
                    None,
                    True,
                    _tes,
                    _diagnostics,
                )

    assert all(result is not None for result in results), "every input candidate must receive exactly one result"
    return results  # type: ignore[return-value]
