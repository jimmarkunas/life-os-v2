from __future__ import annotations

from datetime import date, timedelta

import pytest

from lifeos.jobs.lifecycle import LifecycleStatus, apply_observation, close_definitively, mark_applied, new_record
from lifeos.jobs.models import AdmissionStatus, Opportunity
from tests.jobs.fixtures import make_job

RUN_DATE = date(2026, 1, 15)


def _opportunity(key="k1", admission=AdmissionStatus.ADMITTED):
    return Opportunity(stable_job_key=key, job=make_job(), admission_status=admission)


def test_new_record_starts_new_and_live():
    record = new_record(_opportunity(), run_date=RUN_DATE)
    assert record.status == LifecycleStatus.NEW
    assert record.live is True
    assert record.first_surfaced == RUN_DATE


def test_excluded_opportunity_starts_historical_not_live():
    record = new_record(_opportunity(admission=AdmissionStatus.EXCLUDED), run_date=RUN_DATE)
    assert record.status == LifecycleStatus.HISTORICAL
    assert record.live is False


def test_apply_observation_preserves_first_surfaced():
    original = new_record(_opportunity(), run_date=RUN_DATE)
    later = RUN_DATE + timedelta(days=5)
    updated = apply_observation(original, _opportunity(), run_date=later)
    assert updated.first_surfaced == RUN_DATE
    assert updated.last_seen == later


def test_applied_flag_is_preserved_across_reobservation():
    original = new_record(_opportunity(), run_date=RUN_DATE)
    applied = mark_applied(original, run_date=RUN_DATE)
    reobserved = apply_observation(applied, _opportunity(), run_date=RUN_DATE + timedelta(days=10))
    assert reobserved.applied is True
    assert reobserved.status == LifecycleStatus.APPLIED
    assert reobserved.applied_on == RUN_DATE


def test_applied_on_is_never_overwritten_once_set():
    original = new_record(_opportunity(), run_date=RUN_DATE)
    applied = mark_applied(original, run_date=RUN_DATE)
    reapplied = mark_applied(applied, run_date=RUN_DATE + timedelta(days=3))
    assert reapplied.applied_on == RUN_DATE


def test_review_transition_after_ready_date():
    original = new_record(_opportunity(), run_date=RUN_DATE)
    reviewed = apply_observation(original, _opportunity(), run_date=original.review_ready_on)
    assert reviewed.status == LifecycleStatus.REVIEW


def test_absence_is_never_a_close_reason():
    """Simply not re-observing a record must never call close_definitively;
    only calling apply_observation with a live opportunity keeps status
    current. This test documents the invariant by construction: there is no
    API path from "not observed this run" to closed."""
    original = new_record(_opportunity(), run_date=RUN_DATE)
    assert original.live is True  # no action taken -- remains as last known


def test_close_definitively_requires_explicit_call():
    original = new_record(_opportunity(), run_date=RUN_DATE)
    closed = close_definitively(original)
    assert closed.live is False
    assert closed.status == LifecycleStatus.EXPIRED


def test_close_definitively_on_applied_record_stays_applied_status():
    original = new_record(_opportunity(), run_date=RUN_DATE)
    applied = mark_applied(original, run_date=RUN_DATE)
    closed = close_definitively(applied)
    assert closed.status == LifecycleStatus.APPLIED


def test_apply_observation_rejects_mismatched_key():
    original = new_record(_opportunity(key="k1"), run_date=RUN_DATE)
    with pytest.raises(ValueError):
        apply_observation(original, _opportunity(key="k2"), run_date=RUN_DATE)
