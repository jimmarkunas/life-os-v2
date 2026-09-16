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

from lifeos.jobs.models import AdmissionStatus, Job, Opportunity


@dataclass(frozen=True)
class LaneObservation:
    """One source lane's already-qualified view of a Job."""

    stable_job_key: str
    lane: str
    job: Job
    fit: int
    admission_status: AdmissionStatus


@dataclass(frozen=True)
class ReconciledOpportunity:
    opportunity: Opportunity
    source_lanes: tuple[str, ...]
    observation_count: int
    best_fit: int


def _strongest_url(observations: list[LaneObservation]) -> str | None:
    for obs in observations:
        if obs.job.apply_url:
            return obs.job.apply_url
    return None


def _best_admission(observations: list[LaneObservation]) -> AdmissionStatus:
    order = {AdmissionStatus.ADMITTED: 0, AdmissionStatus.PASSED_REVIEW: 1, AdmissionStatus.EXCLUDED: 2}
    return min((obs.admission_status for obs in observations), key=lambda status: order[status])


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
        ordered = sorted(group, key=lambda o: (lane_priority[o.lane], -o.fit, o.lane))
        visible = ordered[0]
        best_fit = max(o.fit for o in group)
        source_lanes = tuple(sorted({o.lane for o in group}, key=lambda lane: (lane_priority[lane], lane)))

        merged_job = visible.job
        strongest_url = _strongest_url(ordered)
        if strongest_url and strongest_url != merged_job.apply_url:
            from dataclasses import replace as _replace

            merged_job = _replace(merged_job, apply_url=strongest_url)

        opportunity = Opportunity(
            stable_job_key=key,
            job=merged_job,
            admission_status=_best_admission(group),
            source_lanes=source_lanes,
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
        key=lambda r: (lane_priority[r.source_lanes[0]], -r.best_fit, r.opportunity.stable_job_key),
    )
