"""Career-owned qualification policy.

MIGRATE/REFACTOR from v1 `jobs/qualification.py`. The gate sequence (fit ->
market -> work mode -> compensation -> freshness) and the fail-closed
posture -- unresolved evidence routes to PASSED_REVIEW, never silent
exclusion -- are proven and reused. Generalized by removing v1's hardcoded
lane names/numeric floors (US Remote/Scale-up/Skilled Worker, "fit >= 78",
etc.): those are Jim-specific production policy and must never live in this
public repository. A `LaneConfig` is now a plain, source-agnostic value the
caller constructs (from private runtime configuration in production, from
synthetic fixtures in tests). Career owns the *shape* and *order* of the
gates; the actual thresholds are injected, never baked in.

Mail/Newsletter may extract facts (fit score, freshness, compensation) but
must not call this module's gates itself and must not encode its own
admission policy -- that would be Newsletter owning Career business policy,
which the platform contract prohibits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from lifeos.jobs.models import AdmissionStatus, FreshnessStatus, NormalizedCandidate, WorkMode


class QualificationError(ValueError):
    pass


@dataclass(frozen=True)
class LaneConfig:
    """Source-agnostic lane policy. Real values are private runtime
    configuration; only the shape lives in this public repository."""

    name: str
    market: str
    fit_floor: int
    target_review_floor: int | None
    work_mode_policy: str  # "remote_only" | "any"
    compensation_floor: int | None
    freshness_gate: bool
    freshness_max_days: int | None
    is_target_bucket: bool = False


@dataclass(frozen=True)
class QualificationResult:
    admission_status: AdmissionStatus
    review_reason: str | None
    freshness_status: FreshnessStatus


def freshness_for_policy(
    candidate: NormalizedCandidate, *, lane: LaneConfig, run_date: date
) -> tuple[FreshnessStatus, str | None]:
    if candidate.freshness_status == FreshnessStatus.CLOSED:
        return FreshnessStatus.CLOSED, "Vacancy definitively closed/removed"

    posting = candidate.job.posting_date
    if not lane.freshness_gate:
        return candidate.freshness_status, None

    if posting is None:
        return FreshnessStatus.UNRESOLVED, "Freshness unresolved"
    age_days = (run_date - posting).days
    if age_days < 0:
        return FreshnessStatus.UNRESOLVED, "Posting date is in the future"
    max_days = lane.freshness_max_days
    if max_days is None:
        raise QualificationError(f"{lane.name}: freshness_gate requires freshness_max_days")
    if age_days <= max_days:
        return FreshnessStatus.FRESH, None
    return FreshnessStatus.STALE, f"Posting older than configured {max_days}-day freshness maximum"


def qualify(candidate: NormalizedCandidate, *, lane: LaneConfig, run_date: date) -> QualificationResult:
    """Apply the shared gate sequence and return a terminal admission status.

    Never returns an ambiguous result: every branch is either ADMITTED,
    PASSED_REVIEW (unresolved evidence, not a rejection), or EXCLUDED (an
    explicit, evidenced disqualification).
    """
    if candidate.market and candidate.market != lane.market:
        return QualificationResult(
            AdmissionStatus.EXCLUDED,
            f"Market {candidate.market} does not match configured {lane.market} lane",
            candidate.freshness_status,
        )

    fit = candidate.fit
    if fit is not None and fit < lane.fit_floor:
        if lane.is_target_bucket and lane.target_review_floor is not None and fit >= lane.target_review_floor:
            return QualificationResult(
                AdmissionStatus.PASSED_REVIEW,
                f"Target review band: fit {fit} is below {lane.fit_floor} admission floor",
                candidate.freshness_status,
            )
        return QualificationResult(
            AdmissionStatus.EXCLUDED,
            f"Fit {fit} is below configured {lane.fit_floor} floor",
            candidate.freshness_status,
        )

    if lane.work_mode_policy == "remote_only":
        if candidate.job.work_mode == WorkMode.UNKNOWN:
            return QualificationResult(
                AdmissionStatus.PASSED_REVIEW, "Remote work mode unresolved", candidate.freshness_status
            )
        if candidate.job.work_mode != WorkMode.REMOTE:
            return QualificationResult(
                AdmissionStatus.EXCLUDED, "Lane requires remote work mode", candidate.freshness_status
            )

    comp_min = candidate.job.compensation_minimum
    if lane.compensation_floor is not None and comp_min is not None and comp_min < lane.compensation_floor:
        return QualificationResult(
            AdmissionStatus.EXCLUDED,
            f"Explicit compensation below configured {lane.compensation_floor} floor",
            candidate.freshness_status,
        )

    freshness, freshness_reason = freshness_for_policy(candidate, lane=lane, run_date=run_date)
    if freshness == FreshnessStatus.CLOSED:
        return QualificationResult(AdmissionStatus.EXCLUDED, freshness_reason, freshness)
    if lane.freshness_gate and freshness != FreshnessStatus.FRESH:
        status = AdmissionStatus.PASSED_REVIEW if freshness == FreshnessStatus.UNRESOLVED else AdmissionStatus.EXCLUDED
        return QualificationResult(status, freshness_reason, freshness)

    if fit is None:
        return QualificationResult(AdmissionStatus.PASSED_REVIEW, "Fit unresolved", freshness)
    return QualificationResult(AdmissionStatus.ADMITTED, None, freshness)
