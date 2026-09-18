from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from lifeos.core.http import HttpError, HttpErrorKind
from lifeos.jobs.newsletter_contract import Disposition, IngestResult
from lifeos.mail.models import MailMessage
from lifeos.newsletter.models import MessageParseResult, ParseState, RoutedNewsletterMessage, SourceVacancyObservation
from scripts import smoke_us_remote_live_1x1 as smoke


@dataclass
class FakeWebResult:
    observations: tuple[SourceVacancyObservation, ...]


class FakeAcquirer:
    def __init__(self, *, context, http, fallback_fetcher=None) -> None:
        pass

    def acquire(self, *args, **kwargs) -> FakeWebResult:
        return FakeWebResult((observation("web-1", source_message_id="web"),))


class FakeGmail:
    def __init__(self, *, context, http, access_token, message_factory) -> None:
        now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        self.messages = [
            MailMessage(
                provider="gmail",
                message_id="synthetic-non-inbox-job",
                received_at=now,
                sender="alerts@example.invalid",
                subject="Daily job alert: archived role",
                body_text="Synthetic role",
                headers={"List-Unsubscribe": "<https://example.invalid/unsub>"},
            ),
            MailMessage(
                provider="gmail",
                message_id="synthetic-human",
                received_at=now,
                sender="person@example.invalid",
                subject="Lunch?",
                body_text="Human mail in Inbox",
                headers={},
            ),
            MailMessage(
                provider="gmail",
                message_id="synthetic-inbox-job",
                received_at=now,
                sender="alerts@example.invalid",
                subject="Daily job alert: new roles",
                body_text="Synthetic role",
                headers={"List-Unsubscribe": "<https://example.invalid/unsub>"},
            ),
            MailMessage(
                provider="gmail",
                message_id="synthetic-unrelated",
                received_at=now,
                sender="friend@example.invalid",
                subject="Hello",
                body_text="Not a job alert",
                headers={},
            ),
        ]
        self.labels = {
            "synthetic-non-inbox-job": {"J Newsletters"},
            "synthetic-human": {"INBOX"},
            "synthetic-inbox-job": {"INBOX"},
            "synthetic-unrelated": {"INBOX"},
        }
        self.inbox_scanned = False
        self.staged: list[str] = []
        self.processed: list[str] = []

    def scan_inbox_window(self, start, end):
        self.inbox_scanned = True
        return tuple(message for message in self.messages if "INBOX" in self.labels.get(message.message_id, set()))

    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None:
        self.staged.append(message_id)
        self.labels.setdefault(message_id, set()).discard("INBOX")
        self.labels.setdefault(message_id, set()).add(boundary_name)

    def hydrate_messages(self, message_ids) -> tuple[RoutedNewsletterMessage, ...]:
        return tuple(self._routed_message(message_id) for message_id in message_ids)

    def _routed_message(self, message_id: str) -> RoutedNewsletterMessage:
        if message_id not in self.staged:
            raise AssertionError("message was parsed before staging")
        return RoutedNewsletterMessage(
            mailbox="gmail",
            message_id=message_id,
            received_at=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            sender="alerts@example.invalid",
            subject="Daily job alert: new roles",
            body_text="Senior Product Manager at ExampleCo https://example.invalid/job",
        )

    def mark_newsletter_processed(self, message_id: str, boundary_name: str) -> None:
        if message_id not in self.staged:
            raise AssertionError("unstaged message marked processed")
        self.labels.setdefault(message_id, set()).add(f"{boundary_name}/Processed")
        self.processed.append(message_id)


def observation(evidence_ref: str, *, source_message_id: str = "synthetic-inbox-job") -> SourceVacancyObservation:
    return SourceVacancyObservation(
        evidence_ref=evidence_ref,
        source_provider="Synthetic",
        source_mailbox="gmail",
        source_message_id=source_message_id,
        source_subject="Daily job alert: new roles",
        company="ExampleCo",
        role="Senior Product Manager",
        location_text="Remote",
        compensation_text=None,
        source_apply_url="https://example.invalid/job",
        source_received_at=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
    )


def parsed_message(message: RoutedNewsletterMessage) -> MessageParseResult:
    return MessageParseResult(
        message_ref=f"gmail:{message.message_id}",
        source_provider="Synthetic",
        state=ParseState.PASS,
        observations=(observation("newsletter-1", source_message_id=message.message_id),),
        issues=(),
    )


def parsed_degraded(message: RoutedNewsletterMessage) -> MessageParseResult:
    return MessageParseResult(
        message_ref=f"gmail:{message.message_id}",
        source_provider="Synthetic",
        state=ParseState.DEGRADED,
        observations=(),
        issues=(),
    )


def install_common(monkeypatch):
    fake_gmail_box: dict[str, FakeGmail] = {}

    def gmail_factory(**kwargs):
        fake = FakeGmail(**kwargs)
        fake_gmail_box["gmail"] = fake
        return fake

    monkeypatch.setattr(smoke, "_require_env", lambda: {
        "NOTION_API_TOKEN": "synthetic-token",
        "GMAIL_OAUTH_CLIENT_ID": "synthetic-client",
        "GMAIL_OAUTH_CLIENT_SECRET": "synthetic-secret",
        "GMAIL_OAUTH_REFRESH_TOKEN": "synthetic-refresh",
        "NOTION_JOB_LEDGER_DATA_SOURCE_ID": "synthetic-data-source",
    })
    monkeypatch.setattr(smoke, "load_registry", lambda: {})
    monkeypatch.setattr(smoke, "browser_evidence", lambda: None)
    monkeypatch.setattr(smoke, "fallback_fetcher", lambda context, evidence: None)
    monkeypatch.setattr(smoke, "_load_private_policy_from_notion", lambda *args, **kwargs: (object(), {}, object(), "US", "Newsletter"))
    monkeypatch.setattr(smoke, "_exchange_gmail_access_token", lambda *args, **kwargs: "synthetic-access")
    monkeypatch.setattr(smoke, "NotionTransport", lambda **kwargs: object())
    monkeypatch.setattr(smoke, "GmailMailboxTransport", gmail_factory)
    monkeypatch.setattr(smoke.prod, "USRemoteAcquirer", FakeAcquirer)
    monkeypatch.setattr(smoke, "NotionCareerRepository", lambda **kwargs: object())
    monkeypatch.setattr(smoke, "NotionCareerRepositoryConfig", lambda **kwargs: object())
    monkeypatch.setattr(smoke, "HttpClientFetcher", lambda **kwargs: object())
    monkeypatch.setattr(smoke, "NewsletterJobsAdapter", lambda config: object())
    monkeypatch.setattr(smoke, "NewsletterAdapterConfig", lambda **kwargs: object())
    monkeypatch.setattr(smoke, "partition_observations", lambda observations, **kwargs: (list(observations), []))
    monkeypatch.setattr(smoke, "_adapt_all", lambda observations, **kwargs: list(observations))
    return fake_gmail_box


def test_live_1x1_stages_one_fresh_inbox_message_before_processing(monkeypatch, capsys) -> None:
    fake_gmail_box = install_common(monkeypatch)
    monkeypatch.setattr(smoke, "parse_message", parsed_message)
    monkeypatch.setattr(
        smoke,
        "ingest",
        lambda candidates, **kwargs: [
            IngestResult(candidate.evidence_ref, Disposition.CREATED, f"synthetic-key-{index}")
            for index, candidate in enumerate(candidates)
        ],
    )

    assert smoke.main() == 0

    output = json.loads(capsys.readouterr().out)
    gmail = fake_gmail_box["gmail"]
    assert output["status"] == "PASS"
    assert output["newsletter_staged_from_inbox"] is True
    assert output["newsletter_processed"] is True
    assert output["web_candidates"] == 1
    assert gmail.inbox_scanned is True
    assert gmail.staged == ["synthetic-inbox-job"]
    assert gmail.processed == ["synthetic-inbox-job"]
    assert gmail.labels["synthetic-non-inbox-job"] == {"J Newsletters"}
    assert gmail.labels["synthetic-inbox-job"] == {"J Newsletters", "J Newsletters/Processed"}
    assert gmail.labels["synthetic-human"] == {"INBOX"}
    assert gmail.labels["synthetic-unrelated"] == {"INBOX"}


def test_live_1x1_parse_failure_does_not_mark_processed(monkeypatch, capsys) -> None:
    fake_gmail_box = install_common(monkeypatch)
    monkeypatch.setattr(smoke, "parse_message", parsed_degraded)

    assert smoke.main() == 1

    output = json.loads(capsys.readouterr().out)
    gmail = fake_gmail_box["gmail"]
    assert output["status"] == "DEGRADED"
    assert output["reason"] == "selected-newsletter-parse-failed"
    assert output["phase"] == "newsletter-parse"
    assert gmail.staged == ["synthetic-inbox-job"]
    assert gmail.processed == []


def test_live_1x1_no_current_candidate_reports_sanitized_diagnostic(monkeypatch, capsys) -> None:
    fake_gmail_box = install_common(monkeypatch)

    class NoCandidateGmail(FakeGmail):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.labels["synthetic-inbox-job"] = {"J Newsletters"}
            self.messages = [
                message for message in self.messages if message.message_id != "synthetic-inbox-job"
            ]

    def gmail_factory(**kwargs):
        fake = NoCandidateGmail(**kwargs)
        fake_gmail_box["gmail"] = fake
        return fake

    monkeypatch.setattr(smoke, "GmailMailboxTransport", gmail_factory)

    assert smoke.main() == 1

    output = json.loads(capsys.readouterr().out)
    gmail = fake_gmail_box["gmail"]
    assert output["status"] == "DEGRADED"
    assert output["reason"] == "no-current-newsletter-candidate"
    assert output["phase"] == "newsletter-inbox-acquire"
    assert gmail.inbox_scanned is True
    assert gmail.staged == []
    assert gmail.processed == []


def test_live_1x1_inbox_acquisition_http_failure_is_sanitized(monkeypatch, capsys) -> None:
    fake_gmail_box = install_common(monkeypatch)

    class FailingInboxGmail(FakeGmail):
        def scan_inbox_window(self, start, end):
            raise HttpError(HttpErrorKind.HTTP_STATUS, status_code=503, api_message="synthetic-token gmail.googleapis.com synthetic-inbox-job")

    def gmail_factory(**kwargs):
        fake = FailingInboxGmail(**kwargs)
        fake_gmail_box["gmail"] = fake
        return fake

    monkeypatch.setattr(smoke, "GmailMailboxTransport", gmail_factory)

    assert smoke.main() == 1

    raw_output = capsys.readouterr().out
    output = json.loads(raw_output)
    gmail = fake_gmail_box["gmail"]
    assert output["status"] == "DEGRADED"
    assert output["reason"] == "HttpError"
    assert output["phase"] == "newsletter-inbox-acquire"
    assert output["http_kind"] == "http_status"
    assert output["http_status"] == 503
    assert gmail.staged == []
    assert gmail.processed == []
    assert "gmail.googleapis.com" not in raw_output
    assert "synthetic-token" not in raw_output
    assert "synthetic-inbox-job" not in raw_output


def test_live_1x1_accounting_failure_does_not_mark_processed(monkeypatch, capsys) -> None:
    fake_gmail_box = install_common(monkeypatch)
    monkeypatch.setattr(smoke, "parse_message", parsed_message)
    monkeypatch.setattr(
        smoke,
        "ingest",
        lambda candidates, **kwargs: [
            IngestResult(candidate.evidence_ref, Disposition.REVIEW_DEGRADED, None, "synthetic accounting failure")
            for candidate in candidates
        ],
    )

    assert smoke.main() == 1

    output = json.loads(capsys.readouterr().out)
    gmail = fake_gmail_box["gmail"]
    assert output["status"] == "DEGRADED"
    assert output["newsletter_processed"] is False
    assert gmail.staged == ["synthetic-inbox-job"]
    assert gmail.processed == []
