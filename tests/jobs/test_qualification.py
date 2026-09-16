from __future__ import annotations

from datetime import date, timedelta

from lifeos.jobs.models import AdmissionStatus, FreshnessStatus, WorkMode
from lifeos.jobs.qualification import qualify
from tests.jobs.fixtures import ANY_MODE_LANE, REMOTE_LANE, RUN_DATE, make_candidate, make_job


def test_admitted_when_all_gates_pass():
    candidate = make_candidate(fit=90)
    result = qualify(candidate, lane=REMOTE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.ADMITTED
    assert result.review_reason is None


def test_low_fit_excluded_by_default():
    candidate = make_candidate(fit=50)
    result = qualify(candidate, lane=REMOTE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.EXCLUDED


def test_target_bucket_review_band_passes_review_not_excluded():
    candidate = make_candidate(fit=70, market="Synthetic-Other")
    result = qualify(candidate, lane=ANY_MODE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.PASSED_REVIEW


def test_unresolved_fit_passes_review_never_excluded():
    candidate = make_candidate(fit=None)
    result = qualify(candidate, lane=REMOTE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.PASSED_REVIEW
    assert result.review_reason == "Fit unresolved"


def test_market_mismatch_excluded():
    candidate = make_candidate(market="Wrong-Market")
    result = qualify(candidate, lane=REMOTE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.EXCLUDED


def test_remote_only_lane_excludes_onsite():
    job = make_job(work_mode=WorkMode.ONSITE)
    candidate = make_candidate(job=job)
    result = qualify(candidate, lane=REMOTE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.EXCLUDED


def test_remote_only_lane_reviews_unknown_work_mode():
    job = make_job(work_mode=WorkMode.UNKNOWN)
    candidate = make_candidate(job=job)
    result = qualify(candidate, lane=REMOTE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.PASSED_REVIEW


def test_compensation_below_floor_excluded():
    job = make_job(compensation_minimum=50_000)
    candidate = make_candidate(job=job)
    result = qualify(candidate, lane=REMOTE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.EXCLUDED


def test_missing_compensation_is_allowed():
    job = make_job(compensation_minimum=None)
    candidate = make_candidate(job=job, fit=90)
    result = qualify(candidate, lane=REMOTE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.ADMITTED


def test_stale_posting_excluded_when_freshness_gated():
    job = make_job(posting_date=RUN_DATE - timedelta(days=30))
    candidate = make_candidate(job=job)
    result = qualify(candidate, lane=REMOTE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.EXCLUDED


def test_missing_posting_date_reviews_when_freshness_gated():
    job = make_job(posting_date=None)
    candidate = make_candidate(job=job)
    result = qualify(candidate, lane=REMOTE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.PASSED_REVIEW


def test_closed_freshness_always_excluded():
    candidate = make_candidate(freshness_status=FreshnessStatus.CLOSED)
    result = qualify(candidate, lane=REMOTE_LANE, run_date=RUN_DATE)
    assert result.admission_status == AdmissionStatus.EXCLUDED
    assert result.freshness_status == FreshnessStatus.CLOSED
