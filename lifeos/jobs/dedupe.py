"""Cross-source Job identity convergence.

MIGRATE/REFACTOR from v1 `jobs/integration.py`. The convergence shape is
proven and reused: group already-qualified observations by stable_job_key,
select one deterministic "visible" observation by an injected lane-priority
order, preserve provenance (which lanes observed this Job), and take the
strongest available apply URL. v1's hardcoded lane-priority dict
(Scale-up=0, Skilled Worker=1, US Remote/Newsletter/US Web=2) is retired --
that is Jim-specific production policy. Lane priority is now caller-supplied.

This module does not qualify or score. It receives observations that have
already passed qualify() (see qualification.py) for their own lane and
collapses same-identity observations into one canonical Opportunity.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from lifeos.jobs.identity import provider_alias
from lifeos.jobs.models import AdmissionStatus, FitAuthority, Job, Opportunity


@dataclass(frozen=True)
class LaneObservation:
    """One source lane's already-qualified view of a Job."""

    stable_job_key: str
    lane: str
    job: Job
    fit: int | None
    admission_status: AdmissionStatus
    fit_authority: FitAuthority = FitAuthority.NON_AUTHORITATIVE


@dataclass(frozen=True)
class ReconciledOpportunity:
    opportunity: Opportunity
    source_lanes: tuple[str, ...]
    observation_count: int
    best_fit: int | None


def _fit_rank(fit: int | None) -> int:
    return fit if fit is not None else -1


def _strongest_url(observations: list[LaneObservation]) -> str | None:
    for obs in observations:
        if obs.job.apply_url:
            return obs.job.apply_url
    return None


def _best_admission(observations: list[LaneObservation]) -> AdmissionStatus:
    order = {AdmissionStatus.ADMITTED: 0, AdmissionStatus.PASSED_REVIEW: 1, AdmissionStatus.EXCLUDED: 2}
    return min((obs.admission_status for obs in observations), key=lambda status: order[status])


def _best_fit(observations: list[LaneObservation]) -> tuple[int | None, FitAuthority]:
    authoritative = [obs.fit for obs in observations if obs.fit is not None and obs.fit_authority == FitAuthority.AUTHORITATIVE]
    if authoritative:
        return authoritative[-1], FitAuthority.AUTHORITATIVE
    fit = max((obs.fit for obs in observations), key=_fit_rank)
    return fit, FitAuthority.NON_AUTHORITATIVE


def reconcile(
    observations: list[LaneObservation], *, lane_priority: dict[str, int]
) -> list[ReconciledOpportunity]:
    """Converge same-identity observations into one Opportunity per key.

    `lane_priority` is caller-supplied (lower sorts first / becomes visible)
    so this module carries no hardcoded product policy. Every input
    observation's stable_job_key must already be resolved by
    identity.stable_job_key(); this function does not derive identity
    itself -- an unresolvable observation is the caller's REVIEW-DEGRADED
    case, handled before reconciliation, not here.
    """
    grouped: dict[str, list[LaneObservation]] = {}
    for obs in observations:
        if obs.lane not in lane_priority:
            raise ValueError(f"observation lane has no configured priority: {obs.lane}")
        grouped.setdefault(obs.stable_job_key, []).append(obs)

    reconciled: list[ReconciledOpportunity] = []
    for key, group in grouped.items():
        ordered = sorted(group, key=lambda o: (lane_priority[o.lane], -_fit_rank(o.fit), o.lane))
        visible = ordered[0]
        best_fit, fit_authority = _best_fit(ordered)
        source_lanes = tuple(sorted({o.lane for o in group}, key=lambda lane: (lane_priority[lane], lane)))

        merged_job = visible.job
        strongest_url = _strongest_url(ordered)
        if strongest_url and strongest_url != merged_job.apply_url:
            from dataclasses import replace as _replace

            merged_job = _replace(merged_job, apply_url=strongest_url)

        # Cross-provider aliases: every observation in the group may carry a
        # different provider_job_id for what is now proven to be the same
        # canonical vacancy. Preserve all of them as provenance rather than
        # letting convergence silently discard the losing provider's ID.
        aliases = tuple(sorted({alias for o in group if (alias := provider_alias(o.job)) is not None}))
        source_providers = tuple(sorted({o.job.source_provider for o in group if o.job.source_provider}))

        opportunity = Opportunity(
            stable_job_key=key,
            job=merged_job,
            admission_status=_best_admission(group),
            source_lanes=source_lanes,
            aliases=aliases,
            fit=best_fit,
            fit_authority=fit_authority,
            source_providers=source_providers,
        )
        reconciled.append(
            ReconciledOpportunity(
                opportunity=opportunity,
                source_lanes=source_lanes,
                observation_count=len(group),
                best_fit=best_fit,
            )
        )

    return sorted(
        reconciled,
        key=lambda r: (lane_priority[r.source_lanes[0]], -_fit_rank(r.best_fit), r.opportunity.stable_job_key),
    )
