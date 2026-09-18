from __future__ import annotations

import pytest

from lifeos.jobs.dedupe import LaneObservation, reconcile
from lifeos.jobs.models import AdmissionStatus
from tests.jobs.fixtures import make_job

LANE_PRIORITY = {"Lane-A": 0, "Lane-B": 1, "Lane-C": 1}


def _obs(key, lane, fit, admission=AdmissionStatus.ADMITTED, apply_url=None, provider_job_id=None):
    job = make_job(apply_url=apply_url, provider_job_id=provider_job_id)
    return LaneObservation(stable_job_key=key, lane=lane, job_observation=job, fit=fit, admission_status=admission)


def test_single_observation_passes_through():
    result = reconcile([_obs("k1", "Lane-A", 90)], lane_priority=LANE_PRIORITY)
    assert len(result) == 1
    assert result[0].job.stable_job_key == "k1"
    assert result[0].source_lanes == ("Lane-A",)


def test_higher_priority_lane_wins_visibility():
    observations = [
        _obs("k1", "Lane-B", 95),
        _obs("k1", "Lane-A", 80),
    ]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert len(result) == 1
    assert result[0].job.source_lanes[0] == "Lane-A"
    assert result[0].source_lanes == ("Lane-A", "Lane-B")


def test_best_fit_tracked_even_when_not_visible_lane():
    observations = [_obs("k1", "Lane-A", 80), _obs("k1", "Lane-B", 95)]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert result[0].best_fit == 95


def test_unresolved_fit_does_not_outrank_real_score():
    observations = [_obs("k1", "Lane-A", None), _obs("k1", "Lane-B", 95)]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert result[0].best_fit == 95
    assert result[0].job.fit == 95


def test_unresolved_fit_stays_unresolved_when_no_score_exists():
    result = reconcile([_obs("k1", "Lane-A", None)], lane_priority=LANE_PRIORITY)
    assert result[0].best_fit is None
    assert result[0].job.fit is None


def test_observation_count_reflects_all_sources():
    observations = [_obs("k1", "Lane-A", 80), _obs("k1", "Lane-B", 85), _obs("k1", "Lane-C", 90)]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert result[0].observation_count == 3


def test_strongest_apply_url_preferred():
    observations = [
        _obs("k1", "Lane-A", 90, apply_url=None),
        _obs("k1", "Lane-B", 80, apply_url="https://boards.example/real"),
    ]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert result[0].job.job.apply_url == "https://boards.example/real"


def test_admitted_in_any_lane_beats_excluded_in_another():
    """If one lane's own policy admits this Job and another lane's policy
    excludes it, the canonical Job is admitted -- a stricter lane's
    exclusion must not suppress a real admission proven elsewhere."""
    observations = [
        _obs("k1", "Lane-A", 90, admission=AdmissionStatus.ADMITTED),
        _obs("k1", "Lane-B", 80, admission=AdmissionStatus.EXCLUDED),
    ]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert result[0].job.admission_status == AdmissionStatus.ADMITTED


def test_unconfigured_lane_raises():
    with pytest.raises(ValueError):
        reconcile([_obs("k1", "Unknown-Lane", 90)], lane_priority=LANE_PRIORITY)


def test_distinct_keys_never_merge():
    observations = [_obs("k1", "Lane-A", 90), _obs("k2", "Lane-A", 90)]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert {r.job.stable_job_key for r in result} == {"k1", "k2"}


# --- Provider alias preservation across convergence -------------------------


def test_aliases_from_all_converged_providers_are_preserved():
    observations = [
        _obs("k1", "Lane-A", 90, provider_job_id="linkedin-1"),
        _obs("k1", "Lane-B", 85, provider_job_id="lensa-2"),
    ]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert result[0].job.aliases == (
        "Acme Synthetic Co::lensa-2",
        "Acme Synthetic Co::linkedin-1",
    )


def test_duplicate_provider_alias_deduplicated():
    observations = [
        _obs("k1", "Lane-A", 90, provider_job_id="same-id"),
        _obs("k1", "Lane-B", 85, provider_job_id="same-id"),
    ]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert result[0].job.aliases == ("Acme Synthetic Co::same-id",)
