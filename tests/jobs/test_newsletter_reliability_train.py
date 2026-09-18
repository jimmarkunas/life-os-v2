from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from lifeos.core.runtime import RunContext
from lifeos.jobs.lifecycle import LifecycleRecord, LifecycleStatus
from lifeos.jobs.models import AdmissionStatus, Company, FitAuthority, Job, Opportunity, WorkMode
from lifeos.jobs.newsletter_reliability import (
    MemoizingFetcher,
    drain_newsletter_backlog,
    prepare_newsletter_candidates,
)
from lifeos.jobs.terminal_evidence import FetchResponse
from lifeos.newsletter.models import (
    MessageParseResult,
    ParseState,
    RoutedNewsletterMessage,
    SourceVacancyObservation,
)
from lifeos.newsletter.processor import (
    NewsletterExecutionState,
    NewsletterProcessResult,
    NewsletterTimings,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakeBacklogGmail:
    mailbox = "gmail"

    def __init__(self, ids: list[str], clock: MutableClock) -> None:
        self.ids = list(ids)
        self.clock = clock
        self.hydrated: list[str] = []

    def enumerate_unprocessed_ids(self, _boundary: str):
        return tuple(self.ids)

    def hydrate_messages(self, ids):
        message_id = tuple(ids)[0]
        self.hydrated.append(message_id)
        self.clock.value += 1.0
        return (
            RoutedNewsletterMessage(
                mailbox="gmail",
                message_id=message_id,
                received_at=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
                sender="alerts@example.invalid",
                subject=f"Jobs {message_id}",
                body_text="synthetic",
            ),
        )


class FakeProcessor:
    def __init__(self, clock: MutableClock, observations_per_message: int = 10) -> None:
        self.clock = clock
        self.observations_per_message = observations_per_message

    def process_messages(self, messages):
        self.clock.value += 1.0
        message = tuple(messages)[0]
        observations = tuple(
            SourceVacancyObservation(
                evidence_ref=f"{message.message_id}:{index}",
                source_provider="Synthetic",
                source_mailbox="gmail",
                source_message_id=message.message_id,
                source_subject=message.subject,
                company="Acme",
                role=f"Program Manager {index}",
                location_text="Remote",
                compensation_text=None,
                source_apply_url=None,
                source_received_at=message.received_at,
            )
            for index in range(self.observations_per_message)
        )
        parsed = MessageParseResult(
            f"gmail:{message.message_id}",
            "Synthetic",
            ParseState.PASS,
            observations,
            (),
        )
        return NewsletterProcessResult(
            NewsletterExecutionState.PASS,
            (parsed,),
            (),
            NewsletterTimings(0.0, 0.0, 0.0),
        )


def test_deadline_aware_drain_admits_oldest_prefix_and_stops_before_reserve() -> None:
    clock = MutableClock()
    context = RunContext.start(
        timeout_seconds=20,
        now=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
        monotonic_clock=clock,
    )
    gmail = FakeBacklogGmail(["A", "B", "C", "D"], clock)

    result = drain_newsletter_backlog(
        gmail,
        processor=FakeProcessor(clock),
        boundary_name="J Newsletters",
        context=context,
    )

    assert result.pending_before == ("A", "B", "C", "D")
    assert result.admitted_message_ids == ("A", "B")
    assert gmail.hydrated == ["A", "B"]
    assert result.admitted_observations == 20
    assert result.process_result.state is NewsletterExecutionState.PASS


def test_deadline_aware_drain_resumes_from_canonical_gmail_state_without_cursor() -> None:
    clock = MutableClock()
    gmail = FakeBacklogGmail(["A", "B", "C"], clock)
    first = RunContext.start(
        timeout_seconds=16,
        now=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
        monotonic_clock=clock,
    )
    first_result = drain_newsletter_backlog(
        gmail,
        processor=FakeProcessor(clock),
        boundary_name="J Newsletters",
        context=first,
    )
    assert first_result.admitted_message_ids == ("A",)

    gmail.ids = [item for item in gmail.ids if item not in first_result.admitted_message_ids]
    clock.value = 0.0
    second = RunContext.start(
        timeout_seconds=16,
        now=datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc),
        monotonic_clock=clock,
    )
    second_result = drain_newsletter_backlog(
        gmail,
        processor=FakeProcessor(clock),
        boundary_name="J Newsletters",
        context=second,
    )
    assert second_result.pending_before == ("B", "C")
    assert second_result.admitted_message_ids == ("B",)


class CountingFetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResponse:
        self.calls.append(url)
        return FetchResponse(final_url=url, body="<html>synthetic vacancy evidence</html>")


def test_memoizing_fetcher_resolves_exact_same_url_once_per_run() -> None:
    wrapped = CountingFetcher()
    fetcher = MemoizingFetcher(wrapped)

    first = fetcher.get("https://jobs.example.invalid/jobs/1")
    second = fetcher.get("https://jobs.example.invalid/jobs/1")

    assert first == second
    assert wrapped.calls == ["https://jobs.example.invalid/jobs/1"]


def _complete_record() -> LifecycleRecord:
    job = Job(
        company=Company(name="Acme"),
        role="Senior Program Manager",
        location="Remote",
        work_mode=WorkMode.REMOTE,
        compensation_text="$150,000",
        compensation_minimum=150000,
        posting_date=date(2026, 9, 16),
        apply_url="https://jobs.example.invalid/jobs/1",
        source_lane="US Remote",
        canonical_identity="url:https://jobs.example.invalid/jobs/1",
        description_text="Lead a complex technical program across teams.",
        source_provider="Synthetic",
    )
    opportunity = Opportunity(
        stable_job_key="url:https://jobs.example.invalid/jobs/1",
        job=job,
        admission_status=AdmissionStatus.ADMITTED,
        fit=92,
        fit_authority=FitAuthority.AUTHORITATIVE,
    )
    return LifecycleRecord(
        opportunity=opportunity,
        status=LifecycleStatus.REVIEW,
        applied=False,
        applied_on=None,
        first_surfaced=date(2026, 9, 16),
        review_ready_on=date(2026, 9, 17),
        last_seen=date(2026, 9, 16),
        live=True,
    )


class FakeRepository:
    def __init__(self, record: LifecycleRecord) -> None:
        self.record = record

    def get_many(self, stable_job_keys):
        return {
            self.record.opportunity.stable_job_key: self.record
            for key in stable_job_keys
            if key == self.record.opportunity.stable_job_key
        }

    def get_by_apply_urls(self, urls):
        apply_url = self.record.opportunity.job.apply_url
        return {apply_url: self.record} if apply_url in urls else {}


class FailingAdapter:
    def to_jobs_candidate(self, _observation):
        raise AssertionError("terminal adaptation should not run for complete canonical reuse")


def test_complete_canonical_job_reuses_existing_evidence_without_resolution() -> None:
    record = _complete_record()
    observation = SourceVacancyObservation(
        evidence_ref="gmail:msg-1:0",
        source_provider="Synthetic",
        source_mailbox="gmail",
        source_message_id="msg-1",
        source_subject="Jobs",
        company="Acme",
        role="Senior Program Manager",
        location_text="Remote",
        compensation_text=None,
        source_apply_url="https://jobs.example.invalid/jobs/1",
        source_received_at=datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
    )
    context = RunContext.start(timeout_seconds=45)

    prepared = prepare_newsletter_candidates(
        (observation,),
        adapter=FailingAdapter(),
        repository=FakeRepository(record),
        context=context,
        market="US",
        source_lane="US Remote",
    )

    assert prepared.canonical_reuse_count == 1
    assert len(prepared.candidates) == 1
    candidate = prepared.candidates[0]
    assert candidate.job.canonical_identity == record.opportunity.stable_job_key
    assert candidate.job.description_text == record.opportunity.job.description_text
    assert candidate.fit == 92
    assert candidate.fit_authority is FitAuthority.AUTHORITATIVE
