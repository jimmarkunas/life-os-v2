from __future__ import annotations

import pytest

from lifeos.jobs.dedupe import LaneObservation, reconcile
from lifeos.jobs.models import AdmissionStatus
from tests.jobs.fixtures import make_job

LANE_PRIORITY = {"Lane-A": 0, "Lane-B": 1, "Lane-C": 1}


def _obs(key, lane, fit, admission=AdmissionStatus.ADMITTED, apply_url=None):
    job = make_job(apply_url=apply_url)
    return LaneObservation(stable_job_key=key, lane=lane, job=job, fit=fit, admission_status=admission)


def test_single_observation_passes_through():
    result = reconcile([_obs("k1", "Lane-A", 90)], lane_priority=LANE_PRIORITY)
    assert len(result) == 1
    assert result[0].opportunity.stable_job_key == "k1"
    assert result[0].source_lanes == ("Lane-A",)


def test_higher_priority_lane_wins_visibility():
    observations = [
        _obs("k1", "Lane-B", 95),
        _obs("k1", "Lane-A", 80),
    ]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert len(result) == 1
    assert result[0].opportunity.source_lanes[0] == "Lane-A"
    assert result[0].source_lanes == ("Lane-A", "Lane-B")


def test_best_fit_tracked_even_when_not_visible_lane():
    observations = [_obs("k1", "Lane-A", 80), _obs("k1", "Lane-B", 95)]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert result[0].best_fit == 95


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
    assert result[0].opportunity.job.apply_url == "https://boards.example/real"


def test_admitted_in_any_lane_beats_excluded_in_another():
    """If one lane's own policy admits this Job and another lane's policy
    excludes it, the canonical Opportunity is admitted -- a stricter lane's
    exclusion must not suppress a real admission proven elsewhere."""
    observations = [
        _obs("k1", "Lane-A", 90, admission=AdmissionStatus.ADMITTED),
        _obs("k1", "Lane-B", 80, admission=AdmissionStatus.EXCLUDED),
    ]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert result[0].opportunity.admission_status == AdmissionStatus.ADMITTED


def test_unconfigured_lane_raises():
    with pytest.raises(ValueError):
        reconcile([_obs("k1", "Unknown-Lane", 90)], lane_priority=LANE_PRIORITY)


def test_distinct_keys_never_merge():
    observations = [_obs("k1", "Lane-A", 90), _obs("k2", "Lane-A", 90)]
    result = reconcile(observations, lane_priority=LANE_PRIORITY)
    assert {r.opportunity.stable_job_key for r in result} == {"k1", "k2"}
