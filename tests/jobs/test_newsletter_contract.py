from __future__ import annotations

from lifeos.jobs.lifecycle import new_record
from lifeos.jobs.models import AdmissionStatus, Job, WorkMode
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.repository import InMemoryCareerRepository
from tests.jobs.fixtures import REMOTE_LANE, RUN_DATE, make_candidate, make_job

LANE_PRIORITY = {"Synthetic-Remote": 0}


class CountingRepository(InMemoryCareerRepository):
    def __init__(self) -> None:
        super().__init__()
        self.upsert_count = 0

    def upsert(self, record):
        self.upsert_count += 1
        return super().upsert(record)


def _seed(
    repo: InMemoryCareerRepository,
    key: str,
    *,
    apply_url: str | None = None,
    company_name: str = "Acme Synthetic Co",
    role: str = "Synthetic Engineer",
    location: str | None = "Remote - Synthetic Country",
):
    job = Job(
        stable_job_key=key,
        job=make_job(company_name=company_name, role=role, location=location, apply_url=apply_url),
        admission_status=AdmissionStatus.PASSED_REVIEW,
    )
    return repo.upsert(new_record(job, run_date=RUN_DATE))


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


def test_duplicate_observation_still_contributes_provenance_to_canonical_record():
    """The second same-vacancy observation (same canonical URL, hence same
    identity) is reported DUPLICATE, but its evidence -- here, its provider
    alias -- must still shape the persisted canonical Job."""
    same_url = "https://boards.example/the-real-vacancy"
    first_job = make_job(apply_url=same_url, provider_job_id=None)
    second_job = make_job(apply_url=same_url, provider_job_id="linkedin-alias-1")
    candidates = [
        make_candidate(job=first_job, evidence_ref="ev:first"),
        make_candidate(job=second_job, evidence_ref="ev:second"),
    ]
    repo = InMemoryCareerRepository()
    results = ingest(candidates, lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    by_ref = {r.evidence_ref: r for r in results}
    assert by_ref["ev:first"].disposition == Disposition.CREATED
    assert by_ref["ev:second"].disposition == Disposition.DUPLICATE

    key = by_ref["ev:first"].stable_job_key
    persisted = repo.get_many([key])[key]
    assert persisted.job.aliases == ("Acme Synthetic Co::linkedin-alias-1",)


def test_cross_provider_duplicate_aliases_reach_the_persisted_record():
    provider_a = make_job(
        company_name="Acme Synthetic Co", role="Synthetic Engineer", location="NYC",
        apply_url=None, provider_job_id="linkedin-1",
    )
    provider_b = make_job(
        company_name="Acme Synthetic Co", role="Synthetic Engineer", location="NYC",
        apply_url=None, provider_job_id="lensa-2",
    )
    candidates = [
        make_candidate(job=provider_a, evidence_ref="ev:a"),
        make_candidate(job=provider_b, evidence_ref="ev:b"),
    ]
    repo = InMemoryCareerRepository()
    results = ingest(candidates, lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    key = results[0].stable_job_key
    persisted = repo.get_many([key])[key]
    assert persisted.job.aliases == (
        "Acme Synthetic Co::lensa-2",
        "Acme Synthetic Co::linkedin-1",
    )


def test_only_one_canonical_mutation_per_key_regardless_of_observation_count():
    same_url_job = make_job(apply_url="https://boards.example/one-vacancy")
    candidates = [make_candidate(job=same_url_job, evidence_ref=f"ev:{i}") for i in range(5)]
    repo = InMemoryCareerRepository()
    results = ingest(candidates, lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    keys = {r.stable_job_key for r in results}
    assert len(keys) == 1
    created_or_updated = [r for r in results if r.disposition in (Disposition.CREATED, Disposition.UPDATED)]
    assert len(created_or_updated) == 1
    duplicates = [r for r in results if r.disposition == Disposition.DUPLICATE]
    assert len(duplicates) == 4


def test_exact_index_to_result_correspondence_in_mixed_batch():
    same_url_job = make_job(apply_url="https://boards.example/shared")
    onsite_job = make_job(work_mode=WorkMode.ONSITE, apply_url="https://boards.example/onsite-only")
    unresolvable_job = make_job(provider_job_id=None, apply_url=None, company_name="", role="", location=None)

    candidates = [
        make_candidate(job=unresolvable_job, evidence_ref="idx0-review-degraded"),
        make_candidate(job=same_url_job, evidence_ref="idx1-created"),
        make_candidate(job=onsite_job, evidence_ref="idx2-excluded"),
        make_candidate(job=same_url_job, evidence_ref="idx3-duplicate"),
        make_candidate(job=make_job(apply_url="https://boards.example/independent"), evidence_ref="idx4-created"),
    ]
    repo = InMemoryCareerRepository()
    results = ingest(candidates, lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)

    assert len(results) == len(candidates)
    for i, (candidate, result) in enumerate(zip(candidates, results)):
        assert result.evidence_ref == candidate.evidence_ref, f"index {i} mismatch"

    assert results[0].disposition == Disposition.REVIEW_DEGRADED
    assert results[1].disposition == Disposition.CREATED
    assert results[2].disposition == Disposition.EXCLUDED
    assert results[3].disposition == Disposition.DUPLICATE
    assert results[4].disposition == Disposition.CREATED


def test_idempotent_rerun_of_identical_batch_does_not_duplicate_job():
    candidate = make_candidate(evidence_ref="ev:1")
    repo = InMemoryCareerRepository()
    first = ingest([candidate], lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    second = ingest([candidate], lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    assert first[0].disposition == Disposition.CREATED
    assert second[0].disposition == Disposition.UPDATED
    assert first[0].stable_job_key == second[0].stable_job_key
    assert len(repo.get_many([first[0].stable_job_key])) == 1


def test_existing_fallback_identity_later_canonical_url_updates_same_job():
    repo = InMemoryCareerRepository()
    fallback_job = make_job(
        company_name="Acme Synthetic Co",
        role="Technical Program Manager",
        location="Remote",
        apply_url=None,
    )
    first = ingest(
        [make_candidate(job=fallback_job, fit=None, evidence_ref="ev:first")],
        lane=REMOTE_LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
    )
    assert first[0].disposition == Disposition.CREATED
    assert first[0].stable_job_key == "acme synthetic co|technical program manager|remote"

    enriched_job = make_job(
        company_name="Acme Synthetic Co",
        role="Technical Program Manager",
        location="Remote",
        apply_url="https://greenhouse.io/acme/jobs/123",
    )
    second = ingest(
        [make_candidate(job=enriched_job, evidence_ref="ev:second")],
        lane=REMOTE_LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
    )

    assert second[0].disposition == Disposition.UPDATED
    assert second[0].stable_job_key == first[0].stable_job_key
    assert repo.get_many(["url:https://greenhouse.io/acme/jobs/123"]) == {}
    persisted = repo.get_many([first[0].stable_job_key])[first[0].stable_job_key]
    assert persisted.job.job.apply_url == "https://greenhouse.io/acme/jobs/123"


def test_persisted_apply_url_resolves_future_changed_fallback_to_existing_job():
    repo = InMemoryCareerRepository()
    existing_key = "acme synthetic co|technical program manager|remote"
    _seed(repo, existing_key, apply_url="https://greenhouse.io/acme/jobs/123", role="Technical Program Manager", location="Remote")

    changed_fallback_job = make_job(
        company_name="Acme Inc",
        role="TPM",
        location="Remote US",
        apply_url="https://greenhouse.io/acme/jobs/123",
    )
    result = ingest(
        [make_candidate(job=changed_fallback_job, evidence_ref="ev:url")],
        lane=REMOTE_LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
    )

    assert result[0].disposition == Disposition.UPDATED
    assert result[0].stable_job_key == existing_key
    assert len(repo.get_many([existing_key])) == 1


def test_existing_url_key_row_still_resolves_to_url_key():
    repo = InMemoryCareerRepository()
    url_key = "url:https://greenhouse.io/acme/jobs/123"
    _seed(repo, url_key, apply_url="https://greenhouse.io/acme/jobs/123")

    result = ingest(
        [make_candidate(job=make_job(apply_url="https://greenhouse.io/acme/jobs/123"), evidence_ref="ev:url")],
        lane=REMOTE_LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
    )

    assert result[0].disposition == Disposition.UPDATED
    assert result[0].stable_job_key == url_key


def test_url_and_fallback_matching_same_existing_row_converges_safely():
    repo = InMemoryCareerRepository()
    fallback_key = "acme synthetic co|synthetic engineer|remote - synthetic country"
    _seed(repo, fallback_key, apply_url="https://greenhouse.io/acme/jobs/123")

    result = ingest(
        [make_candidate(job=make_job(apply_url="https://greenhouse.io/acme/jobs/123"), evidence_ref="ev:both")],
        lane=REMOTE_LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
    )

    assert result[0].disposition == Disposition.UPDATED
    assert result[0].stable_job_key == fallback_key


def test_url_and_fallback_matching_different_existing_rows_is_review_degraded_no_write():
    repo = CountingRepository()
    _seed(repo, "url:https://greenhouse.io/acme/jobs/123", apply_url="https://greenhouse.io/acme/jobs/123")
    _seed(repo, "acme synthetic co|synthetic engineer|remote - synthetic country", apply_url=None)
    repo.upsert_count = 0

    result = ingest(
        [make_candidate(job=make_job(apply_url="https://greenhouse.io/acme/jobs/123"), evidence_ref="ev:collision")],
        lane=REMOTE_LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
    )

    assert result[0].disposition == Disposition.REVIEW_DEGRADED
    assert "multiple existing Jobs" in (result[0].detail or "")
    assert repo.upsert_count == 0


def test_no_existing_match_keeps_current_stable_job_key_priority():
    repo = InMemoryCareerRepository()
    url_result = ingest(
        [make_candidate(job=make_job(apply_url="https://greenhouse.io/acme/jobs/123"), evidence_ref="ev:url")],
        lane=REMOTE_LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
    )
    fallback_result = ingest(
        [make_candidate(job=make_job(apply_url=None, role="Technical Program Manager", location="Remote"), fit=None, evidence_ref="ev:fallback")],
        lane=REMOTE_LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
    )

    assert url_result[0].stable_job_key == "url:https://greenhouse.io/acme/jobs/123"
    assert fallback_result[0].stable_job_key == "acme synthetic co|technical program manager|remote"


def test_multiple_observations_converging_to_existing_job_produce_one_mutation():
    repo = CountingRepository()
    existing_key = "acme synthetic co|technical program manager|remote"
    _seed(repo, existing_key, apply_url="https://greenhouse.io/acme/jobs/123", role="Technical Program Manager", location="Remote")
    repo.upsert_count = 0

    candidates = [
        make_candidate(
            job=make_job(company_name="Acme Inc", role="TPM", location="Remote US", apply_url="https://greenhouse.io/acme/jobs/123"),
            evidence_ref="ev:url",
        ),
        make_candidate(
            job=make_job(role="Technical Program Manager", location="Remote", apply_url=None),
            fit=None,
            evidence_ref="ev:fallback",
        ),
    ]
    result = ingest(candidates, lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)

    assert {item.disposition for item in result} == {Disposition.UPDATED, Disposition.DUPLICATE}
    assert {item.stable_job_key for item in result} == {existing_key}
    assert repo.upsert_count == 1


def test_apply_url_lookup_uses_canonical_tracking_normalization():
    repo = InMemoryCareerRepository()
    existing_key = "acme synthetic co|synthetic engineer|remote - synthetic country"
    _seed(repo, existing_key, apply_url="https://greenhouse.io/acme/jobs/123?utm_source=stored")

    result = ingest(
        [make_candidate(job=make_job(apply_url="https://greenhouse.io/acme/jobs/123?utm_source=incoming&ref=x"), evidence_ref="ev:tracked")],
        lane=REMOTE_LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
    )

    assert result[0].disposition == Disposition.UPDATED
    assert result[0].stable_job_key == existing_key
