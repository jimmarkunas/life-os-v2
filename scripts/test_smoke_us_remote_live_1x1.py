from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

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
        self.inbox = [
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
        self.staged: list[str] = []
        self.processed: list[str] = []

    def scan_window(self, start, end):
        return tuple(self.inbox)

    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None:
        self.staged.append(message_id)
        self.inbox = [message for message in self.inbox if message.message_id != message_id]

    def _fetch_routed_message(self, message_id: str) -> RoutedNewsletterMessage:
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

    monkeypatch.setattr(smoke.prod, "_require_env", lambda: {
        "NOTION_API_TOKEN": "synthetic-token",
        "GMAIL_OAUTH_CLIENT_ID": "synthetic-client",
        "GMAIL_OAUTH_CLIENT_SECRET": "synthetic-secret",
        "GMAIL_OAUTH_REFRESH_TOKEN": "synthetic-refresh",
        "NOTION_JOB_LEDGER_DATA_SOURCE_ID": "synthetic-data-source",
    })
    monkeypatch.setattr(smoke.prod, "_load_registry", lambda: {})
    monkeypatch.setattr(smoke.prod, "_browser_evidence", lambda: None)
    monkeypatch.setattr(smoke.prod, "_fallback_fetcher", lambda context, evidence: None)
    monkeypatch.setattr(smoke.prod, "_load_private_policy_from_notion", lambda *args, **kwargs: (object(), {}, object(), "US", "Newsletter"))
    monkeypatch.setattr(smoke.prod, "_exchange_gmail_access_token", lambda *args, **kwargs: "synthetic-access")
    monkeypatch.setattr(smoke, "NotionTransport", lambda **kwargs: object())
    monkeypatch.setattr(smoke, "GmailMailboxTransport", gmail_factory)
    monkeypatch.setattr(smoke.prod, "USRemoteAcquirer", FakeAcquirer)
    monkeypatch.setattr(smoke, "NotionCareerRepository", lambda **kwargs: object())
    monkeypatch.setattr(smoke, "NotionCareerRepositoryConfig", lambda **kwargs: object())
    monkeypatch.setattr(smoke, "HttpClientFetcher", lambda **kwargs: object())
    monkeypatch.setattr(smoke, "NewsletterJobsAdapter", lambda config: object())
    monkeypatch.setattr(smoke, "NewsletterAdapterConfig", lambda **kwargs: object())
    monkeypatch.setattr(smoke.prod, "_partition_observations", lambda observations, **kwargs: (list(observations), []))
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
    assert gmail.staged == ["synthetic-inbox-job"]
    assert gmail.processed == ["synthetic-inbox-job"]
    assert [message.message_id for message in gmail.inbox] == ["synthetic-unrelated"]


def test_live_1x1_parse_failure_does_not_mark_processed(monkeypatch, capsys) -> None:
    fake_gmail_box = install_common(monkeypatch)
    monkeypatch.setattr(smoke, "parse_message", parsed_degraded)

    assert smoke.main() == 1

    output = json.loads(capsys.readouterr().out)
    gmail = fake_gmail_box["gmail"]
    assert output["status"] == "DEGRADED"
    assert output["reason"] == "selected-newsletter-parse-failed"
    assert gmail.staged == ["synthetic-inbox-job"]
    assert gmail.processed == []


def test_live_1x1_no_current_candidate_reports_sanitized_diagnostic(monkeypatch, capsys) -> None:
    fake_gmail_box = install_common(monkeypatch)

    class NoCandidateGmail(FakeGmail):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.inbox = [
                MailMessage(
                    provider="gmail",
                    message_id="synthetic-unrelated",
                    received_at=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
                    sender="friend@example.invalid",
                    subject="Hello",
                    body_text="Not a job alert",
                    headers={},
                )
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
    assert gmail.staged == []
    assert gmail.processed == []
