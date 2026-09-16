from __future__ import annotations

import threading

from lifeos.core.runtime import RunContext
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


def test_duplicate_observation_still_contributes_provenance_to_canonical_record():
    """BLOCKER 2 regression: the second same-vacancy observation (same
    canonical URL, hence same identity) is reported DUPLICATE, but its
    evidence -- here, its provider alias -- must still shape the persisted
    canonical Opportunity, never be silently discarded."""
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
    # The duplicate's alias reached the canonical record despite its own
    # observation being reported DUPLICATE -- evidence was merged, not dropped.
    assert persisted.opportunity.aliases == ("Acme Synthetic Co::linkedin-alias-1",)


def test_cross_provider_duplicate_aliases_reach_the_persisted_record():
    """Two different discovery providers, same vacancy (via matching
    company+role+location), different provider_job_id -- both must
    contribute their alias to the one persisted canonical record."""
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
    assert persisted.opportunity.aliases == (
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
    """BLOCKER 3: results[i] must correspond to candidates[i] for every i,
    across every disposition type in one batch, regardless of internal
    grouping/reconciliation order."""
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


# --- PACKAGE C: serial persistence deadline guard ---------------------------


class NonOverlappingUpsertRepository(InMemoryCareerRepository):
    """Regression guard: asserts upsert() is never re-entered while another
    upsert() call for this same repository instance is still in progress.
    A future accidental parallelization of canonical writes would trip the
    assertion inside upsert() itself, failing the test."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.upsert_calls = 0

    def upsert(self, record):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            assert self._active <= 1, "canonical upsert() calls overlapped"
        self.upsert_calls += 1
        try:
            return super().upsert(record)
        finally:
            with self._lock:
                self._active -= 1


def _distinct_candidates(n: int) -> list:
    return [
        make_candidate(
            job=make_job(apply_url=f"https://boards.example/synthetic-{i}"),
            evidence_ref=f"ev:{i}",
        )
        for i in range(n)
    ]


def test_canonical_upserts_never_overlap():
    repo = NonOverlappingUpsertRepository()
    candidates = _distinct_candidates(25)
    results = ingest(candidates, lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    assert all(r.disposition == Disposition.CREATED for r in results)
    assert repo.upsert_calls == 25
    assert repo.max_active == 1


class ClockAdvancingRepository(InMemoryCareerRepository):
    """Every upsert() call consumes a fixed amount of the shared fake clock,
    simulating persistence that takes real time against RunContext's own
    deadline -- deterministic, no sleeping, no real time dependency."""

    def __init__(self, clock, seconds_per_upsert: float) -> None:
        super().__init__()
        self._clock = clock
        self._seconds_per_upsert = seconds_per_upsert
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.upsert_calls = 0

    def upsert(self, record):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            assert self._active <= 1, "canonical upsert() calls overlapped"
        self.upsert_calls += 1
        self._clock.value += self._seconds_per_upsert
        try:
            return super().upsert(record)
        finally:
            with self._lock:
                self._active -= 1


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_deadline_exhaustion_stops_further_canonical_mutations_mid_batch():
    clock = _FakeClock()
    # 10 seconds of budget, each upsert "costs" 3 seconds of clock time --
    # only the first 3 or 4 upserts fit before the deadline is exhausted.
    context = RunContext.start(timeout_seconds=10.0, monotonic_clock=clock)
    repo = ClockAdvancingRepository(clock, seconds_per_upsert=3.0)
    candidates = _distinct_candidates(10)

    results = ingest(
        candidates,
        lane=REMOTE_LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
        context=context,
    )

    # Every original input still receives exactly one terminal disposition.
    assert len(results) == len(candidates)
    assert all(r is not None for r in results)

    created = [r for r in results if r.disposition == Disposition.CREATED]
    degraded = [r for r in results if r.disposition == Disposition.REVIEW_DEGRADED]

    # The first allowed mutations executed...
    assert len(created) == repo.upsert_calls
    assert repo.upsert_calls >= 1
    # ...and once the deadline was exhausted, no further upsert() began.
    assert repo.upsert_calls < len(candidates)
    # Every still-unprocessed observation is REVIEW_DEGRADED, not dropped.
    assert len(created) + len(degraded) == len(candidates)
    assert all("deadline" in (r.detail or "") for r in degraded)
    # Canonical mutation concurrency never exceeded 1.
    assert repo.max_active == 1
    # Already-verified writes are preserved (readable back from the repo).
    for r in created:
        assert repo.get_many([r.stable_job_key])


def test_ingest_without_context_is_unaffected_by_deadline_guard():
    """Backward compatibility: omitting context preserves prior behavior --
    no deadline check is performed."""
    repo = InMemoryCareerRepository()
    results = ingest(
        _distinct_candidates(5), lane=REMOTE_LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE
    )
    assert all(r.disposition == Disposition.CREATED for r in results)
