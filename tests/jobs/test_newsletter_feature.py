from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from lifeos.core.runtime import ExecutionStatus, RunContext
from lifeos.jobs.fit_scoring import FitProfile, RoleFamily
from lifeos.jobs.models import WorkMode
from lifeos.jobs.newsletter_adapter import NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition
from lifeos.jobs.newsletter_feature import run_newsletter_feature
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.repository import InMemoryCareerRepository, ReadBackMismatch
from lifeos.jobs.terminal_evidence import FetchResponse
from lifeos.newsletter.models import MessageParseResult, ParseIssue, ParseState, SourceVacancyObservation
from lifeos.newsletter.processor import NewsletterExecutionState, NewsletterProcessResult, NewsletterTimings

RUN_DATE = date(2026, 1, 15)
LANE = LaneConfig(
    name="Synthetic-Newsletter",
    market="Synthetic-US",
    fit_floor=50,
    target_review_floor=None,
    work_mode_policy="any",
    compensation_floor=None,
    freshness_gate=False,
    freshness_max_days=None,
)
LANE_PRIORITY = {"Synthetic-Newsletter": 0, "Strict": 0}
FAKE_PROFILE = FitProfile(
    model_version="test-1",
    role_families=(RoleFamily(patterns=(r"\bsynthetic engineer\b",), base_score=70, label="Synthetic Engineer"),),
    default_role_base=10,
    default_role_label="weak",
)
JOBPOSTING_HTML = """
<html><script type="application/ld+json">
{"@type": "JobPosting", "description": "Synthetic engineer role.", "datePosted": "2026-01-10"}
</script></html>
"""
TIMINGS = NewsletterTimings(fetch_seconds=0.1, parse_seconds=0.1, total_seconds=0.2)


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchResponse]):
        self._responses = responses

    def get(self, url: str) -> FetchResponse:
        if url not in self._responses:
            raise RuntimeError(f"no fixture for {url}")
        return self._responses[url]


def _observation(**overrides) -> SourceVacancyObservation:
    base = dict(
        evidence_ref="ev:1",
        source_provider="synthetic-provider",
        source_mailbox="INBOX",
        source_message_id="msg-1",
        source_subject="Synthetic jobs",
        company="Acme Synthetic Co",
        role="Synthetic Engineer",
        location_text="Remote - Synthetic Country",
        compensation_text="$100,000 - $120,000",
        source_apply_url="https://greenhouse.io/acme/jobs/1",
        provider_job_id=None,
        provider_score=88,
    )
    base.update(overrides)
    return SourceVacancyObservation(**base)


def _process_result(observations: tuple[SourceVacancyObservation, ...], *, state: NewsletterExecutionState = NewsletterExecutionState.PASS) -> NewsletterProcessResult:
    message = MessageParseResult(message_ref="msg-1", source_provider="synthetic-provider", state=ParseState(state.value), observations=observations, issues=())
    return NewsletterProcessResult(state=state, messages=(message,), errors=(), timings=TIMINGS)


def _context() -> RunContext:
    return RunContext.start(timeout_seconds=45.0, now=datetime.now(timezone.utc))


def _adapter(fetcher, source_lane="Newsletter") -> NewsletterJobsAdapter:
    return NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=fetcher, fit_profile=FAKE_PROFILE, market="Synthetic-US", source_lane=source_lane))


# 1. one Newsletter / one qualifying vacancy ---------------------------------


def test_single_qualifying_vacancy_created_and_cleanup_safe():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/1": FetchResponse(final_url="https://greenhouse.io/acme/jobs/1", body=JOBPOSTING_HTML)})
    repo = InMemoryCareerRepository()
    process_result = _process_result((_observation(),))
    result = run_newsletter_feature(process_result, adapter=_adapter(fetcher), lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE, context=_context())
    assert result.cleanup_safe is True
    assert result.execution.status == ExecutionStatus.PASS
    assert len(result.ingest_results) == 1
    assert result.ingest_results[0].disposition == Disposition.CREATED


# 2. one Newsletter / many vacancies ------------------------------------------


def test_many_vacancies_all_accounted_for():
    fetcher = FakeFetcher(
        {
            f"https://greenhouse.io/acme/jobs/{i}": FetchResponse(final_url=f"https://greenhouse.io/acme/jobs/{i}", body=JOBPOSTING_HTML)
            for i in range(1, 6)
        }
    )
    observations = tuple(_observation(evidence_ref=f"ev:{i}", source_apply_url=f"https://greenhouse.io/acme/jobs/{i}") for i in range(1, 6))
    repo = InMemoryCareerRepository()
    process_result = _process_result(observations)
    result = run_newsletter_feature(process_result, adapter=_adapter(fetcher), lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE, context=_context())
    assert len(result.ingest_results) == 5
    assert all(r.disposition == Disposition.CREATED for r in result.ingest_results)
    assert result.cleanup_safe is True


# 3. duplicate vacancy from two source observations -> one canonical mutation


def test_duplicate_source_observations_produce_one_canonical_mutation():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/1": FetchResponse(final_url="https://greenhouse.io/acme/jobs/1", body=JOBPOSTING_HTML)})
    observations = (
        _observation(evidence_ref="ev:a", source_apply_url="https://greenhouse.io/acme/jobs/1?utm_source=x"),
        _observation(evidence_ref="ev:b", source_apply_url="https://greenhouse.io/acme/jobs/1?utm_source=y"),
    )
    fetcher = FakeFetcher(
        {
            # Direct employer/ATS URLs canonicalize (tracking params stripped)
            # with no fetch; the one subsequent fetch is always against the
            # already-canonicalized destination.
            "https://greenhouse.io/acme/jobs/1": FetchResponse(final_url="https://greenhouse.io/acme/jobs/1", body=JOBPOSTING_HTML),
        }
    )
    repo = InMemoryCareerRepository()
    process_result = _process_result(observations)
    result = run_newsletter_feature(process_result, adapter=_adapter(fetcher), lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE, context=_context())
    dispositions = {r.disposition for r in result.ingest_results}
    assert dispositions == {Disposition.CREATED, Disposition.DUPLICATE}
    keys = {r.stable_job_key for r in result.ingest_results}
    assert len(keys) == 1
    assert result.cleanup_safe is True


# 4. existing Job -> update, not a duplicate row ------------------------------


def test_reobserving_existing_job_across_runs_updates_not_duplicates():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/1": FetchResponse(final_url="https://greenhouse.io/acme/jobs/1", body=JOBPOSTING_HTML)})
    repo = InMemoryCareerRepository()
    first = run_newsletter_feature(_process_result((_observation(),)), adapter=_adapter(fetcher), lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE, context=_context())
    second = run_newsletter_feature(_process_result((_observation(evidence_ref="ev:2"),)), adapter=_adapter(fetcher), lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE, context=_context())
    assert first.ingest_results[0].disposition == Disposition.CREATED
    assert second.ingest_results[0].disposition == Disposition.UPDATED
    assert first.ingest_results[0].stable_job_key == second.ingest_results[0].stable_job_key
    assert len(repo.get_many([first.ingest_results[0].stable_job_key])) == 1


# 5. stale/excluded vacancy ---------------------------------------------------


def test_excluded_vacancy_still_cleanup_safe():
    strict_lane = LaneConfig(
        name="Strict",
        market="Synthetic-US",
        fit_floor=999,  # unreachable -- guarantees exclusion
        target_review_floor=None,
        work_mode_policy="any",
        compensation_floor=None,
        freshness_gate=False,
        freshness_max_days=None,
    )
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/1": FetchResponse(final_url="https://greenhouse.io/acme/jobs/1", body=JOBPOSTING_HTML)})
    repo = InMemoryCareerRepository()
    result = run_newsletter_feature(_process_result((_observation(),)), adapter=_adapter(fetcher), lane=strict_lane, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE, context=_context())
    assert result.ingest_results[0].disposition == Disposition.EXCLUDED
    assert result.cleanup_safe is True  # EXCLUDED is a valid terminal disposition, not a blocker


# 6. unresolved final employer URL -> REVIEW-DEGRADED / no cleanup -----------


def test_unresolved_employer_url_is_review_degraded_and_blocks_cleanup():
    fetcher = FakeFetcher({})  # nothing resolves
    repo = InMemoryCareerRepository()
    result = run_newsletter_feature(
        _process_result((_observation(source_apply_url="https://linkedin.com/jobs/view/1"),)),
        adapter=_adapter(fetcher), lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE, context=_context(),
    )
    assert result.ingest_results[0].disposition == Disposition.REVIEW_DEGRADED
    assert result.cleanup_safe is False
    assert result.execution.status == ExecutionStatus.DEGRADED


# 7. read-back failure -> REVIEW-DEGRADED / no cleanup ------------------------


class AlwaysMismatchRepository:
    def get_many(self, stable_job_keys):
        return {}

    def upsert(self, record):
        raise ReadBackMismatch("synthetic forced mismatch")


def test_read_back_failure_is_review_degraded_and_blocks_cleanup():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/1": FetchResponse(final_url="https://greenhouse.io/acme/jobs/1", body=JOBPOSTING_HTML)})
    result = run_newsletter_feature(
        _process_result((_observation(),)),
        adapter=_adapter(fetcher), lane=LANE, lane_priority=LANE_PRIORITY, repository=AlwaysMismatchRepository(), run_date=RUN_DATE, context=_context(),
    )
    assert result.ingest_results[0].disposition == Disposition.REVIEW_DEGRADED
    assert result.cleanup_safe is False


# 8. idempotent replay ---------------------------------------------------------


def test_idempotent_replay_of_identical_batch():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/1": FetchResponse(final_url="https://greenhouse.io/acme/jobs/1", body=JOBPOSTING_HTML)})
    repo = InMemoryCareerRepository()
    observation = _observation()
    first = run_newsletter_feature(_process_result((observation,)), adapter=_adapter(fetcher), lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE, context=_context())
    second = run_newsletter_feature(_process_result((observation,)), adapter=_adapter(fetcher), lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE, context=_context())
    assert first.cleanup_safe is True
    assert second.cleanup_safe is True
    assert first.ingest_results[0].disposition == Disposition.CREATED
    assert second.ingest_results[0].disposition == Disposition.UPDATED
    assert len(repo.get_many([first.ingest_results[0].stable_job_key])) == 1


# 9. private Fit profile remains runtime-only ---------------------------------


def test_no_default_fit_profile_shipped_in_public_code():
    """fit_scoring.py and newsletter_adapter.py must never define a
    ready-to-use real-looking FitProfile constant -- every caller (tests
    included) must construct/inject their own."""
    import lifeos.jobs.fit_scoring as fit_scoring_module
    import lifeos.jobs.newsletter_adapter as adapter_module

    for module in (fit_scoring_module, adapter_module):
        for name in dir(module):
            if name.startswith("_"):
                continue
            value = getattr(module, name)
            assert not isinstance(value, FitProfile), f"{module.__name__}.{name} is an unexpected default FitProfile"


# 10. no full Job Ledger scan --------------------------------------------------


def test_feature_only_looks_up_keys_it_actually_touched():
    class RecordingRepository(InMemoryCareerRepository):
        def __init__(self):
            super().__init__()
            self.get_many_calls: list[list[str]] = []

        def get_many(self, stable_job_keys):
            self.get_many_calls.append(list(stable_job_keys))
            return super().get_many(stable_job_keys)

    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/1": FetchResponse(final_url="https://greenhouse.io/acme/jobs/1", body=JOBPOSTING_HTML)})
    repo = RecordingRepository()
    run_newsletter_feature(_process_result((_observation(),)), adapter=_adapter(fetcher), lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE, context=_context())
    assert len(repo.get_many_calls) == 1
    assert len(repo.get_many_calls[0]) == 1  # exactly the one observed key, never "all keys"
