from __future__ import annotations

from lifeos.jobs.models import WorkMode
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.repository import InMemoryCareerRepository
from tests.jobs.fixtures import REMOTE_LANE, RUN_DATE, make_candidate, make_job

LANE_PRIORITY = {"Synthetic-Remote": 0}


def test_every_candidate_receives_exactly_one_disposition():
    candidates = [
        make_candidate(evidence_ref="ev:1"),
        make_candidate(job=make_job(apply_url="https://boards.example/other"), evidence_ref="ev:2"),
    ]
    repo = InMemoryCareerRepository()
    results = ingest(candidates, lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    assert len(results) == len(candidates)
    assert {r.evidence_ref for r in results} == {"ev:1", "ev:2"}


def test_new_candidate_is_created():
    candidates = [make_candidate(evidence_ref="ev:1")]
    repo = InMemoryCareerRepository()
    results = ingest(candidates, lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    assert results[0].disposition == Disposition.CREATED


def test_reingesting_same_candidate_is_updated_not_duplicated_across_runs():
    candidate = make_candidate(evidence_ref="ev:1")
    repo = InMemoryCareerRepository()
    ingest([candidate], lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    results = ingest(
        [make_candidate(evidence_ref="ev:2")],
        lane=REMOTE_LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
    )
    assert results[0].disposition == Disposition.UPDATED
    assert results[0].stable_job_key == "url:https://synthetic-boards.example/jobs/12345"


def test_within_batch_duplicate_marked_duplicate():
    same_url_job = make_job(apply_url="https://boards.example/dup")
    candidates = [
        make_candidate(job=same_url_job, evidence_ref="ev:1"),
        make_candidate(job=same_url_job, evidence_ref="ev:2"),
    ]
    repo = InMemoryCareerRepository()
    results = ingest(candidates, lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    dispositions = {r.evidence_ref: r.disposition for r in results}
    assert dispositions["ev:1"] == Disposition.CREATED
    assert dispositions["ev:2"] == Disposition.DUPLICATE


def test_excluded_candidate_is_terminal_not_review():
    job = make_job(work_mode=WorkMode.ONSITE)
    candidates = [make_candidate(job=job, evidence_ref="ev:1")]
    repo = InMemoryCareerRepository()
    results = ingest(candidates, lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    assert results[0].disposition == Disposition.EXCLUDED


def test_unresolvable_identity_is_review_degraded_never_dropped():
    job = make_job(provider_job_id=None, apply_url=None, company_name="", role="", location=None)
    candidates = [make_candidate(job=job, evidence_ref="ev:1")]
    repo = InMemoryCareerRepository()
    results = ingest(candidates, lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    assert len(results) == 1
    assert results[0].disposition == Disposition.REVIEW_DEGRADED
    assert results[0].stable_job_key is None


def test_idempotent_rerun_of_identical_batch_does_not_duplicate_opportunity():
    """Same normalized input processed twice must converge on one canonical
    Opportunity: first run creates, second run updates the same key -- never
    a second row for the same vacancy."""
    candidate = make_candidate(evidence_ref="ev:1")
    repo = InMemoryCareerRepository()
    first = ingest([candidate], lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    second = ingest([candidate], lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    assert first[0].disposition == Disposition.CREATED
    assert second[0].disposition == Disposition.UPDATED
    assert first[0].stable_job_key == second[0].stable_job_key
    assert len(repo.get_many([first[0].stable_job_key])) == 1
