from __future__ import annotations

from datetime import datetime, timezone

import lifeos.integrations.gmail as gmail_module
from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import BACKLOG_DETAIL_PACING_SECONDS, GmailMailboxTransport
from lifeos.integrations.test_gmail_serial_backlog import SerialDetailProbeHttp


def test_fetch_unprocessed_paces_each_serial_detail_read(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(gmail_module, "sleep", sleeps.append)
    http = SerialDetailProbeHttp()
    mailbox = GmailMailboxTransport(
        context=RunContext.start(timeout_seconds=45),
        http=http,
        access_token="synthetic-token",
        message_factory=lambda **kwargs: kwargs,
        max_workers=8,
    )

    messages = mailbox.fetch_unprocessed(
        datetime(2026, 9, 15, tzinfo=timezone.utc),
        datetime(2026, 9, 16, tzinfo=timezone.utc),
        "J Newsletters",
    )

    assert len(messages) == 4
    assert http.max_active_detail_requests == 1
    assert sleeps == [BACKLOG_DETAIL_PACING_SECONDS] * 3
