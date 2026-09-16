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

from lifeos.jobs.models import AdmissionStatus, Opportunity


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


def apply_observation(existing: LifecycleRecord, opportunity: Opportunity, *, run_date: date) -> LifecycleRecord:
    """Merge a new observation of the same stable_job_key into an existing
    record. Human-owned fields (applied, applied_on, first_surfaced) are
    never overwritten by this call -- only Career-owned fields (the
    opportunity snapshot, last_seen, live, and status derived from
    admission) advance."""
    if existing.opportunity.stable_job_key != opportunity.stable_job_key:
        raise ValueError("cannot merge observations for different stable_job_key values")

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
