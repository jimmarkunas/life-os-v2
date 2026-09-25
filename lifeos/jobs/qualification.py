"""Career-owned qualification policy.

The gate sequence (fit -> market -> work mode -> compensation -> freshness)
and the fail-closed posture -- unresolved evidence routes to PASSED_REVIEW,
never silent exclusion -- are shared Jobs behavior. Jim's current product
decision sets one GitHub-owned universal Jobs Fit floor for all configured
lanes: ``UNIVERSAL_FIT_FLOOR = 72``. Lane/private runtime configuration owns
other opportunity-policy fields such as market, work mode, compensation, and
freshness, but cannot override the Jobs Fit floor or create a lower target
review persistence band.

Mail/Newsletter may extract facts (fit score, freshness, compensation) but
must not call this module's gates itself and must not encode its own
admission policy -- that would be Newsletter owning Career business policy,
which the platform contract prohibits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from lifeos.jobs.models import AdmissionStatus, EvidenceStatus, FreshnessStatus, NormalizedCandidate, WorkMode

UNIVERSAL_FIT_FLOOR = 72


class QualificationError(ValueError):
    pass


@dataclass(frozen=True)
class LaneConfig:
    """Lane opportunity policy with a GitHub-owned universal Fit floor.

    ``fit_floor`` and ``target_review_floor`` remain constructor-compatible for
    existing callers, but are normalized on construction: the effective Fit
    floor is always ``UNIVERSAL_FIT_FLOOR`` and no lower review band exists.
    Other lane policy remains runtime-configurable.
    """

    name: str
    market: str
    fit_floor: int
    target_review_floor: int | None
    work_mode_policy: str  # "remote_only" | "any"
    compensation_floor: int | None
    freshness_gate: bool
    freshness_max_days: int | None
    is_target_bucket: bool = False
    visa_route: str | None = None
    visa_route_gate: bool = False
    geography_gate: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "fit_floor", UNIVERSAL_FIT_FLOOR)
        object.__setattr__(self, "target_review_floor", None)


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

    if getattr(lane, "visa_route_gate", False):
        if candidate.route_evidence_status is EvidenceStatus.NEGATIVE:
            return QualificationResult(AdmissionStatus.EXCLUDED, f"{lane.visa_route or lane.name} route evidence is negative", freshness)
        if candidate.route_evidence_status is not EvidenceStatus.POSITIVE:
            return QualificationResult(AdmissionStatus.PASSED_REVIEW, f"{lane.visa_route or lane.name} route evidence unresolved", freshness)

    if getattr(lane, "geography_gate", False):
        if candidate.geography_evidence_status is EvidenceStatus.NEGATIVE:
            return QualificationResult(AdmissionStatus.EXCLUDED, "Scale-Up geography is non-qualifying", freshness)
        if candidate.geography_evidence_status is not EvidenceStatus.POSITIVE:
            return QualificationResult(AdmissionStatus.PASSED_REVIEW, "Scale-Up geography unresolved", freshness)

    if fit is None:
        return QualificationResult(AdmissionStatus.PASSED_REVIEW, "Fit unresolved", freshness)
    return QualificationResult(AdmissionStatus.ADMITTED, None, freshness)
