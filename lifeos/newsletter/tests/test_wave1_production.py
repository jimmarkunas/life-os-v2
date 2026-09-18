from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from lifeos.core.runtime import ExecutionStatus, RunContext
from lifeos.jobs.fit_scoring import FitProfile, RoleFamily
from lifeos.jobs.newsletter_adapter import NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition
from lifeos.jobs.newsletter_feature import run_newsletter_feature
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.repository import InMemoryCareerRepository
from lifeos.jobs.terminal_evidence import FetchResponse
from lifeos.mail.models import MailClass, MailMessage
from lifeos.mail.router import MailRouter
from lifeos.newsletter.models import ParseState, RoutedNewsletterMessage
from lifeos.newsletter.parsers import parse_message
from lifeos.newsletter.processor import NewsletterProcessor


RUN_AT = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
RUN_DATE = date(2026, 1, 15)
BOUNDARY = "J Newsletters"

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
LANE_PRIORITY = {"Synthetic-Newsletter": 0}
FIT_PROFILE = FitProfile(
    model_version="wave1-test",
    role_families=(RoleFamily(patterns=(r"\bsynthetic engineer\b", r"\btechnical program manager\b"), base_score=70, label="match"),),
    default_role_base=10,
    default_role_label="weak",
)


def _jobposting(role: str, *, date_posted: str = "2026-01-10") -> str:
    return f"""
    <html>
      <head><meta name="description" content="{role} synthetic posting"></head>
      <body>
        <script type="application/ld+json">
        {{"@type": "JobPosting", "description": "{role} full synthetic description", "datePosted": "{date_posted}"}}
        </script>
      </body>
    </html>
    """


class Fetcher:
    def __init__(self) -> None:
        self.responses = {
            "https://email.lensa.com/ls/click/card-1": FetchResponse(
                final_url="https://email.lensa.com/ls/click/card-1",
                body='<a href="https://greenhouse.io/synthetic/jobs/1">Apply</a>',
            ),
            "https://email.lensa.com/f/a/card-3": FetchResponse(
                final_url="https://email.lensa.com/f/a/card-3",
                body='<a href="https://greenhouse.io/synthetic/jobs/3">Apply</a>',
            ),
            "https://jobright.ai/jobs/info/job-2": FetchResponse(
                final_url="https://jobright.ai/jobs/info/job-2",
                body='<a href="https://lever.co/synthetic/2">Apply now</a>',
            ),
            "https://www.linkedin.com/jobs/view/999/": FetchResponse(
                final_url="https://www.linkedin.com/jobs/view/999/",
                body='<a href="https://greenhouse.io/synthetic/jobs/1?utm_source=linkedin">Apply</a>',
            ),
            "https://greenhouse.io/synthetic/jobs/1": FetchResponse(
                final_url="https://greenhouse.io/synthetic/jobs/1",
                body=_jobposting("Synthetic Engineer"),
            ),
            "https://greenhouse.io/synthetic/jobs/3": FetchResponse(
                final_url="https://greenhouse.io/synthetic/jobs/3",
                body=_jobposting("Marketing Manager", date_posted="Posted 1 day ago"),
            ),
            "https://lever.co/synthetic/2": FetchResponse(
                final_url="https://lever.co/synthetic/2",
                body=_jobposting("Technical Program Manager"),
            ),
        }

    def get(self, url: str) -> FetchResponse:
        if url not in self.responses:
            raise AssertionError(f"missing synthetic fetch fixture for {url}")
        return self.responses[url]


class FakeMailbox:
    provider = "gmail"
    mailbox = "gmail"

    def __init__(self, messages: list[MailMessage]) -> None:
        self.messages = messages
        self.routed: list[str] = []

    def scan_window(self, start: datetime, end: datetime):
        return [message for message in self.messages if start <= message.received_at < end]

    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None:
        assert boundary_name == BOUNDARY
        self.routed.append(message_id)

    def fetch_unprocessed(self, start: datetime, end: datetime, boundary_name: str):
        assert boundary_name == BOUNDARY
        out = []
        for message in self.messages:
            if message.message_id not in self.routed or not (start <= message.received_at < end):
                continue
            out.append(
                RoutedNewsletterMessage(
                    mailbox=message.provider,
                    message_id=message.message_id,
                    received_at=message.received_at,
                    sender=message.sender,
                    subject=message.subject,
                    body_text=message.body_text,
                    headers=message.headers,
                    html_text=message.html_text,
                )
            )
        return out


class FakeNewsletterSource:
    mailbox = "gmail"

    def __init__(self, messages: list[RoutedNewsletterMessage]) -> None:
        self.messages = messages

    def fetch_unprocessed(self, start: datetime, end: datetime, boundary_name: str):
        assert boundary_name == BOUNDARY
        return [message for message in self.messages if start <= message.received_at < end]


def _mail(message_id: str, subject: str, *, sender: str, body: str = "", html: str = "", headers=None, minute: int = 0) -> MailMessage:
    return MailMessage(
        provider="gmail",
        message_id=message_id,
        received_at=RUN_AT + timedelta(minutes=minute),
        sender=sender,
        subject=subject,
        body_text=body,
        html_text=html,
        headers=headers or {},
    )


def _lensa_html() -> str:
    return """
    <html><body>
      <a href="https://email.lensa.com/ls/click/card-1">
        <div>Synthetic Labs</div><div>Synthetic Engineer</div><div>$120K - $150K / yr Remote</div>
      </a>
      <a href="https://email.lensa.com/ls/click/card-1">
        <div>Synthetic Labs</div><div>Synthetic Engineer</div><div>$120K - $150K / yr Remote</div>
      </a>
      <a href="https://email.lensa.com/f/a/card-3">
        <div>Synthetic Ops</div><div>Marketing Manager</div><div>$90K - $110K / yr Remote</div>
      </a>
      <a href="https://email.lensa.com/c/settings">settings</a>
      Gig Jobs
      <a href="https://email.lensa.com/f/a/gig">Gig Jobs</a>
      unsubscribe
    </body></html>
    """


def _jobright_html() -> str:
    return """
    <html><body>
      <a href="https://jobright.ai/jobs/info/job-2">
        <div>Acme Synthetic Co</div><div>92%</div><div>Technical Program Manager</div>
        <div>Remote - Synthetic Country</div><div>$130K - $150K / yr</div>
      </a>
      View more opportunities
      unsubscribe
    </body></html>
    """


def _linkedin_body() -> str:
    return """
    Technical Program Manager
    Synthetic Labs
    Remote - Synthetic Country
    View job: https://www.linkedin.com/jobs/view/999/
    unsubscribe
    """


def _lensa_no_apply_url_body() -> str:
    return """
    Synthetic Labs
    Synthetic Engineer
    $120K - $150K / yr Remote
    Gig Jobs
    unsubscribe
    """


def _jobright_malformed_identity_body() -> str:
    return """
    [Malformed card](https://jobright.ai/jobs/info/malformed)
    View more opportunities
    unsubscribe
    """


def test_lensa_variants_duplicate_rendering_controls_and_non_target_roles() -> None:
    result = parse_message(
        RoutedNewsletterMessage(
            mailbox="gmail",
            **{"message_" + "id": "synthetic-lensa"},
            received_at=RUN_AT,
            sender="alerts@lensa.example.invalid",
            subject="Lensa job alert",
            body_text="",
            html_text=_lensa_html(),
        )
    )

    assert result.state is ParseState.PASS
    assert len(result.observations) == 2
    assert [obs.role for obs in result.observations] == ["Synthetic Engineer", "Marketing Manager"]
    assert [obs.source_apply_url for obs in result.observations] == [
        "https://email.lensa.com/ls/click/card-1",
        "https://email.lensa.com/f/a/card-3",
    ]


def test_lensa_positional_recovery_mismatch_preserves_missing_url_issue_without_degrading() -> None:
    html = """
    <a href="https://email.lensa.com/f/a/one">Apply $100K</a>
    <a href="https://email.lensa.com/f/a/two">Apply $120K</a>
    Synthetic Labs
    Synthetic Engineer
    $100K Remote
    Gig Jobs unsubscribe
    """
    result = parse_message(
        RoutedNewsletterMessage(
            mailbox="gmail",
            **{"message_" + "id": "synthetic-lensa-mismatch"},
            received_at=RUN_AT,
            sender="alerts@lensa.example.invalid",
            subject="Lensa job alert",
            body_text=html,
        )
    )

    assert result.state is ParseState.PASS
    assert len(result.observations) == 1
    assert result.observations[0].source_apply_url is None
    assert "source-apply-url-missing" in result.observations[0].issues
    assert [issue.code for issue in result.issues] == ["source-apply-url-missing"]


def test_jobright_and_linkedin_complete_primary_cards() -> None:
    jobright = parse_message(
        RoutedNewsletterMessage("gmail", "synthetic-jobright", RUN_AT, "alerts@jobright.example.invalid", "Jobright daily jobs", "", html_text=_jobright_html())
    )
    linkedin = parse_message(
        RoutedNewsletterMessage("gmail", "synthetic-linkedin", RUN_AT, "jobs@linkedin.example.invalid", "LinkedIn jobs for you", _linkedin_body())
    )

    assert jobright.state is ParseState.PASS
    assert len(jobright.observations) == 1
    assert jobright.observations[0].provider_score == 92
    assert jobright.observations[0].role == "Technical Program Manager"
    assert linkedin.state is ParseState.PASS
    assert linkedin.observations[0].provider_job_id == "999"


def _run_feature(process_result, repo: InMemoryCareerRepository):
    adapter = NewsletterJobsAdapter(
        NewsletterAdapterConfig(
            fetcher=Fetcher(),
            fit_profile=FIT_PROFILE,
            market="Synthetic-US",
            source_lane="Newsletter",
        )
    )
    return run_newsletter_feature(
        process_result,
        adapter=adapter,
        lane=LANE,
        lane_priority=LANE_PRIORITY,
        repository=repo,
        run_date=RUN_DATE,
        context=RunContext.start(timeout_seconds=45.0, now=RUN_AT),
    )


def test_complete_wave1_mailbox_e2e_and_idempotent_replay() -> None:
    mailbox = FakeMailbox(
        [
            _mail("synthetic-human", "Recruiter follow-up", sender="person@example.invalid", body="Would you like to discuss this role?", minute=1),
            _mail("synthetic-other", "Receipt", sender="receipt@example.invalid", body="Your receipt", minute=2),
            _mail("synthetic-lensa", "Job alert from Lensa", sender="alerts@lensa.example.invalid", html=_lensa_html(), headers={"List-Unsubscribe": "<https://example.invalid/unsub>"}, minute=3),
            _mail("synthetic-jobright", "Jobright daily jobs", sender="alerts@jobright.example.invalid", html=_jobright_html(), headers={"List-Unsubscribe": "<https://example.invalid/unsub>"}, minute=4),
            _mail("synthetic-linkedin", "LinkedIn jobs for you", sender="jobs@linkedin.example.invalid", body=_linkedin_body(), headers={"List-Unsubscribe": "<https://example.invalid/unsub>"}, minute=5),
        ]
    )
    start = RUN_AT
    end = RUN_AT + timedelta(hours=1)

    mail_result = MailRouter(newsletter_boundary=BOUNDARY).route_window([mailbox], start, end)
    assert mail_result.count(MailClass.HUMAN_HIRING) == 1
    assert mail_result.count(MailClass.UNRELATED) == 1
    assert mail_result.count(MailClass.AUTOMATED_JOB_SOURCE) == 3

    process_result = NewsletterProcessor(boundary_name=BOUNDARY).process_window([mailbox], start, end)
    assert process_result.state.value == "PASS"
    assert len(process_result.observations) == 4

    repo = InMemoryCareerRepository()
    first = _run_feature(process_result, repo)
    second = _run_feature(process_result, repo)

    first_counts = {d: sum(1 for result in first.ingest_results if result.disposition == d) for d in Disposition}
    second_counts = {d: sum(1 for result in second.ingest_results if result.disposition == d) for d in Disposition}

    assert first.execution.status is ExecutionStatus.PASS
    assert first.cleanup_safe is True
    assert len(first.ingest_results) == 4
    assert first_counts[Disposition.CREATED] == 2
    assert first_counts[Disposition.DUPLICATE] == 1
    assert first_counts[Disposition.EXCLUDED] == 1
    assert first_counts[Disposition.REVIEW_DEGRADED] == 0

    assert second.execution.status is ExecutionStatus.PASS
    assert second.cleanup_safe is True
    assert second_counts[Disposition.CREATED] == 0
    assert second_counts[Disposition.UPDATED] == 2
    assert second_counts[Disposition.DUPLICATE] == 1
    assert second_counts[Disposition.EXCLUDED] == 1
    assert second_counts[Disposition.REVIEW_DEGRADED] == 0
    assert {r.stable_job_key for r in first.ingest_results if r.stable_job_key} == {
        r.stable_job_key for r in second.ingest_results if r.stable_job_key
    }


def test_missing_source_apply_url_is_enrichment_only_through_real_parser_boundary() -> None:
    message = RoutedNewsletterMessage(
        mailbox="gmail",
        message_id="synthetic-lensa-no-url",
        received_at=RUN_AT,
        sender="alerts@lensa.example.invalid",
        subject="Lensa job alert",
        body_text=_lensa_no_apply_url_body(),
    )
    process_result = NewsletterProcessor(boundary_name=BOUNDARY).process_window(
        [FakeNewsletterSource([message])],
        RUN_AT,
        RUN_AT + timedelta(hours=1),
    )

    assert process_result.state.value == "PASS"
    assert len(process_result.messages) == 1
    parsed = process_result.messages[0]
    assert parsed.state is ParseState.PASS
    assert len(parsed.observations) == 1
    assert parsed.issues[0].code == "source-apply-url-missing"
    assert parsed.observations[0].issues == ("source-apply-url-missing",)

    repo = InMemoryCareerRepository()
    result = _run_feature(process_result, repo)

    assert result.cleanup_safe is True
    assert result.execution.status is ExecutionStatus.PASS
    assert result.ingest_results[0].disposition == Disposition.CREATED
    assert result.ingest_results[0].stable_job_key is not None
    persisted = repo.get_many([result.ingest_results[0].stable_job_key])[result.ingest_results[0].stable_job_key]
    assert persisted.job.admission_status.value == "passed_review"
    assert persisted.job.job.apply_url is None
    assert persisted.job.fit is None
    assert all(item.disposition is not Disposition.REVIEW_DEGRADED for item in result.ingest_results)


def test_identity_critical_parser_issue_still_fails_closed_through_real_parser_boundary() -> None:
    message = RoutedNewsletterMessage(
        mailbox="gmail",
        message_id="synthetic-jobright-malformed",
        received_at=RUN_AT,
        sender="alerts@jobright.example.invalid",
        subject="Jobright daily jobs",
        body_text=_jobright_malformed_identity_body(),
    )
    process_result = NewsletterProcessor(boundary_name=BOUNDARY).process_window(
        [FakeNewsletterSource([message])],
        RUN_AT,
        RUN_AT + timedelta(hours=1),
    )

    assert process_result.state.value == "DEGRADED"
    assert process_result.messages[0].state is ParseState.DEGRADED
    assert "unresolved-card-shape" in process_result.observations[0].issues

    result = _run_feature(process_result, InMemoryCareerRepository())
    assert result.ingest_results[0].disposition == Disposition.REVIEW_DEGRADED
    assert result.cleanup_safe is False
