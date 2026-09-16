from datetime import date

from lifeos.core.runtime import ExecutionStatus, RunContext
from lifeos.jobs.fit_scoring import FitProfile, RoleFamily
from lifeos.jobs.newsletter_adapter import NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition
from lifeos.jobs.newsletter_feature import run_newsletter_feature
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.repository import InMemoryCareerRepository
from lifeos.jobs.terminal_evidence import FetchResponse
from lifeos.newsletter.models import MessageParseResult, ParseState, SourceVacancyObservation
from lifeos.newsletter.processor import NewsletterExecutionState, NewsletterProcessResult, NewsletterTimings


class EmptyFetcher:
    def get(self, url: str) -> FetchResponse:
        raise RuntimeError("synthetic unresolved terminal evidence")


def test_supported_lensa_vacancy_stays_successfully_accounted_when_terminal_evidence_is_unavailable():
    profile = FitProfile(
        model_version="synthetic",
        role_families=(RoleFamily(patterns=(r"\\bproject manager\\b",), base_score=80, label="Project Manager"),),
        default_role_base=10,
        default_role_label="weak",
    )
    lane = LaneConfig(
        name="US Remote",
        market="US",
        fit_floor=78,
        target_review_floor=68,
        work_mode_policy="remote_only",
        compensation_floor=80000,
        freshness_gate=True,
        freshness_max_days=7,
    )
    observation = SourceVacancyObservation(
        evidence_ref="gmail:synthetic:card:1",
        source_provider="Lensa",
        source_mailbox="gmail",
        source_message_id="synthetic-message",
        source_subject="Synthetic Lensa alert",
        company="Synthetic Power LLC",
        role="Remote Project Manager",
        location_text="Remote",
        compensation_text="$90K-$110K / yr.",
        source_apply_url="https://email.synthetic.lensa.com/c/synthetic",
        provider_job_id=None,
        provider_score=None,
    )
    message = MessageParseResult(
        message_ref="gmail:synthetic-message",
        source_provider="Lensa",
        state=ParseState.PASS,
        observations=(observation,),
        issues=(),
    )
    process = NewsletterProcessResult(
        state=NewsletterExecutionState.PASS,
        messages=(message,),
        errors=(),
        timings=NewsletterTimings(0.0, 0.0, 0.0),
    )
    result = run_newsletter_feature(
        process,
        adapter=NewsletterJobsAdapter(
            NewsletterAdapterConfig(
                fetcher=EmptyFetcher(),
                fit_profile=profile,
                market="US",
                source_lane="US Remote",
            )
        ),
        lane=lane,
        lane_priority={"US Remote": 0},
        repository=InMemoryCareerRepository(),
        run_date=date(2026, 9, 16),
        context=RunContext.start(timeout_seconds=30),
    )

    assert result.execution.status == ExecutionStatus.PASS
    assert result.cleanup_safe is True
    assert len(result.ingest_results) == 1
    assert result.ingest_results[0].disposition == Disposition.CREATED
    assert result.ingest_results[0].stable_job_key is not None
