from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

import lifeos.integrations.gmail as gmail_module
from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import SCAN_CHUNK_PACING_SECONDS, GmailMailboxTransport
from lifeos.integrations.test_gmail_serial_backlog import SerialDetailProbeHttp


@dataclass(frozen=True)
class _SyntheticMessage:
    provider: str
    message_id: str
    received_at: datetime
    sender: str
    subject: str
    body_text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


def test_scan_window_paces_between_concurrent_chunks(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(gmail_module, "sleep", sleeps.append)
    http = SerialDetailProbeHttp()
    mailbox = GmailMailboxTransport(
        context=RunContext.start(timeout_seconds=45),
        http=http,
        access_token="synthetic-token",
        message_factory=_SyntheticMessage,
        max_workers=2,
    )

    messages = mailbox.scan_window(
        datetime(2026, 9, 15, tzinfo=timezone.utc),
        datetime(2026, 9, 16, tzinfo=timezone.utc),
    )

    # SerialDetailProbeHttp always returns 4 message IDs; max_workers=2 means
    # two chunks of two concurrent calls each, so exactly one pace between them.
    assert len(messages) == 4
    assert sleeps == [SCAN_CHUNK_PACING_SECONDS]


def test_scan_window_single_chunk_does_not_pace(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(gmail_module, "sleep", sleeps.append)
    http = SerialDetailProbeHttp()
    mailbox = GmailMailboxTransport(
        context=RunContext.start(timeout_seconds=45),
        http=http,
        access_token="synthetic-token",
        message_factory=_SyntheticMessage,
        max_workers=8,
    )

    messages = mailbox.scan_window(
        datetime(2026, 9, 15, tzinfo=timezone.utc),
        datetime(2026, 9, 16, tzinfo=timezone.utc),
    )

    assert len(messages) == 4
    assert sleeps == []
