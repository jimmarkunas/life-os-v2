"""Career lifecycle state machine.

MIGRATE/REFACTOR from v1 `jobs/ledger_core.py`. The append-only, human-state-
preserving merge semantics are proven and reused: absence never deletes,
closure requires exact evidence, and human-owned fields (applied,
applied_on, first_surfaced) are never overwritten by a re-run. v1's
duplicated per-lane RoutePolicy constants (SCALE_UP/SKILLED_WORKER/...) are
retired here -- that was Jim-specific production policy hardcoded into a
public-shaped module. The state machine itself (New -> Review -> Apply ->
Applied -> Historical -> Expired) is source-agnostic and kept.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import Enum

from lifeos.jobs.models import AdmissionStatus, Company, FitAuthority, Job, Opportunity, WorkMode


class LifecycleStatus(str, Enum):
    NEW = "new"
    REVIEW = "review"
    APPLY = "apply"
    APPLIED = "applied"
    HISTORICAL = "historical"
    EXPIRED = "expired"


@dataclass(frozen=True)
class LifecycleRecord:
    """The persisted, human-augmentable state for one Opportunity. This is
    the shape a repository (see repository.py) reads and writes; Career
    never reconstructs it from scratch on a re-run -- see
    apply_observation()'s preservation rules."""

    opportunity: Opportunity
    status: LifecycleStatus
    applied: bool
    applied_on: date | None
    first_surfaced: date
    review_ready_on: date
    last_seen: date
    live: bool


def new_record(opportunity: Opportunity, *, run_date: date) -> LifecycleRecord:
    live = opportunity.admission_status != AdmissionStatus.EXCLUDED
    status = LifecycleStatus.NEW if live else LifecycleStatus.HISTORICAL
    return LifecycleRecord(
        opportunity=opportunity,
        status=status,
        applied=False,
        applied_on=None,
        first_surfaced=run_date,
        review_ready_on=run_date + timedelta(days=1),
        last_seen=run_date,
        live=live,
    )


def _prefer_text(incoming: str | None, existing: str | None) -> str | None:
    return incoming if incoming else existing


def _prefer_work_mode(incoming: WorkMode, existing: WorkMode) -> WorkMode:
    return incoming if incoming != WorkMode.UNKNOWN else existing


def _merge_fit(existing: Opportunity, incoming: Opportunity) -> tuple[int | None, FitAuthority]:
    if incoming.fit is not None and incoming.fit_authority == FitAuthority.AUTHORITATIVE:
        return incoming.fit, FitAuthority.AUTHORITATIVE
    if existing.fit is not None and existing.fit_authority == FitAuthority.AUTHORITATIVE:
        return existing.fit, FitAuthority.AUTHORITATIVE
    if incoming.fit is not None:
        return incoming.fit, incoming.fit_authority
    return existing.fit, existing.fit_authority


def merge_canonical_observation(existing: Opportunity, incoming: Opportunity) -> Opportunity:
    """Apply canonical no-downgrade merge policy for one stable Job.

    Repositories serialize state; this function owns Jobs-domain precedence
    before persistence.
    """
    if existing.stable_job_key != incoming.stable_job_key:
        raise ValueError("cannot merge observations for different stable_job_key values")

    existing_job = existing.job
    incoming_job = incoming.job
    merged_job = Job(
        company=Company(
            name=_prefer_text(incoming_job.company.name, existing_job.company.name) or "",
            domain=_prefer_text(incoming_job.company.domain, existing_job.company.domain),
        ),
        role=_prefer_text(incoming_job.role, existing_job.role) or "",
        location=_prefer_text(incoming_job.location, existing_job.location),
        work_mode=_prefer_work_mode(incoming_job.work_mode, existing_job.work_mode),
        compensation_text=_prefer_text(incoming_job.compensation_text, existing_job.compensation_text),
        compensation_minimum=incoming_job.compensation_minimum if incoming_job.compensation_minimum is not None else existing_job.compensation_minimum,
        posting_date=incoming_job.posting_date or existing_job.posting_date,
        apply_url=incoming_job.apply_url or existing_job.apply_url,
        source_lane=incoming_job.source_lane or existing_job.source_lane,
        provider_job_id=incoming_job.provider_job_id or existing_job.provider_job_id,
        canonical_identity=existing.stable_job_key,
        description_text=incoming_job.description_text or existing_job.description_text,
        provider_score=incoming_job.provider_score if incoming_job.provider_score is not None else existing_job.provider_score,
        source_provider=incoming_job.source_provider or existing_job.source_provider,
    )
    fit, fit_authority = _merge_fit(existing, incoming)
    admission_status = existing.admission_status
    if incoming.admission_status == AdmissionStatus.ADMITTED or existing.admission_status != AdmissionStatus.ADMITTED:
        admission_status = incoming.admission_status
    return replace(
        incoming,
        stable_job_key=existing.stable_job_key,
        job=merged_job,
        admission_status=admission_status,
        source_lanes=tuple(sorted(set(existing.source_lanes) | set(incoming.source_lanes))),
        aliases=tuple(sorted(set(existing.aliases) | set(incoming.aliases))),
        fit=fit,
        fit_authority=fit_authority,
        source_providers=tuple(sorted(set(existing.source_providers) | set(incoming.source_providers))),
    )


def apply_observation(existing: LifecycleRecord, opportunity: Opportunity, *, run_date: date) -> LifecycleRecord:
    """Merge a new observation of the same stable_job_key into an existing
    record. Human-owned fields (applied, applied_on, first_surfaced) are
    never overwritten by this call -- only Career-owned fields (the
    opportunity snapshot, last_seen, live, and status derived from
    admission) advance."""
    if existing.opportunity.stable_job_key != opportunity.stable_job_key:
        raise ValueError("cannot merge observations for different stable_job_key values")

    opportunity = merge_canonical_observation(existing.opportunity, opportunity)
    live = opportunity.admission_status != AdmissionStatus.EXCLUDED

    if existing.applied:
        status = LifecycleStatus.APPLIED
    elif existing.status == LifecycleStatus.APPLY and live:
        status = LifecycleStatus.APPLY
    elif live:
        status = LifecycleStatus.REVIEW if run_date >= existing.review_ready_on else LifecycleStatus.NEW
    else:
        status = LifecycleStatus.HISTORICAL

    return replace(
        existing,
        opportunity=opportunity,
        status=status,
        live=live,
        last_seen=run_date,
    )


def close_definitively(record: LifecycleRecord) -> LifecycleRecord:
    """Explicit-evidence closure only. Never called on mere absence from a
    new source scan -- absence is not evidence of closure."""
    return replace(
        record,
        live=False,
        status=LifecycleStatus.APPLIED if record.applied else LifecycleStatus.EXPIRED,
    )


def mark_applied(record: LifecycleRecord, *, run_date: date) -> LifecycleRecord:
    """The Applied flag is submission authority: once set, it is preserved
    across every future merge regardless of admission status."""
    return replace(
        record,
        applied=True,
        applied_on=record.applied_on or run_date,
        status=LifecycleStatus.APPLIED,
    )
