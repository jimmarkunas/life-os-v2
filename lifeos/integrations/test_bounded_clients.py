from __future__ import annotations

import unittest
from datetime import datetime, timezone

from lifeos.core.config import ConfigField, RuntimeConfig
from lifeos.core.http import HttpClient, HttpError, HttpErrorKind, HttpResponse, RetryPolicy
from lifeos.core.runtime import RunContext
from lifeos.integrations.gmail import GMAIL_ACCESS_TOKEN_FIELD, GmailMailboxTransport
from lifeos.integrations.mailbox import MailboxTransportError
from lifeos.integrations.notion import (
    NOTION_ACCESS_TOKEN_FIELD,
    NotionIdentityQuery,
    NotionTransport,
    NotionTransportError,
)


from tests.testkit.builders import SyntheticMessage, gmail_mailbox


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 2, tzinfo=timezone.utc)


class RecordingJsonHttp:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def request_json(self, context, method, url, **kwargs):
        self.calls.append((context, method, url, kwargs))
        return self.responder(len(self.calls), method, url, kwargs)


class IntegrationBoundTests(unittest.TestCase):
    def test_gmail_from_config_builds_authenticated_request_without_repr_leak(self) -> None:
        config = RuntimeConfig.load(
            (ConfigField(GMAIL_ACCESS_TOKEN_FIELD),),
            environ={GMAIL_ACCESS_TOKEN_FIELD: "synthetic-gmail-token"},
        )
        http = RecordingJsonHttp(lambda *_: {"messages": []})
        mailbox = GmailMailboxTransport.from_config(
            context=RunContext.start(timeout_seconds=45),
            http=http,
            config=config,
            message_factory=SyntheticMessage,
        )

        self.assertEqual(mailbox.scan_window(START, END), ())
        headers = http.calls[0][3]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer synthetic-gmail-token")
        self.assertNotIn("synthetic-gmail-token", repr(config))

    def test_gmail_pagination_is_hard_bounded(self) -> None:
        def responder(call_number, _method, _url, _kwargs):
            return {"messages": [], "nextPageToken": f"synthetic-cursor-{call_number}"}

        http = RecordingJsonHttp(responder)
        mailbox = gmail_mailbox(http, max_list_pages=2)

        with self.assertRaisesRegex(MailboxTransportError, "pagination limit"):
            mailbox.scan_window(START, END)
        self.assertEqual(len(http.calls), 2)

    def test_notion_from_config_builds_authenticated_request_without_repr_leak(self) -> None:
        config = RuntimeConfig.load(
            (ConfigField(NOTION_ACCESS_TOKEN_FIELD),),
            environ={NOTION_ACCESS_TOKEN_FIELD: "synthetic-notion-token"},
        )
        http = RecordingJsonHttp(lambda *_: {"results": [], "has_more": False})
        notion = NotionTransport.from_config(
            context=RunContext.start(timeout_seconds=45),
            http=http,
            config=config,
        )
        identity = NotionIdentityQuery("Synthetic Key", "rich_text", ("synthetic-key",))

        self.assertEqual(notion.query_data_source("synthetic-source", identity), ())
        headers = http.calls[0][3]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer synthetic-notion-token")
        self.assertNotIn("synthetic-notion-token", repr(config))

    def test_notion_pagination_is_hard_bounded(self) -> None:
        def responder(call_number, _method, _url, _kwargs):
            return {
                "results": [{"id": f"synthetic-page-{call_number}"}],
                "has_more": True,
                "next_cursor": f"synthetic-cursor-{call_number}",
            }

        http = RecordingJsonHttp(responder)
        notion = NotionTransport(
            context=RunContext.start(timeout_seconds=45),
            http=http,
            access_token="synthetic-token",
            max_query_pages=2,
        )
        identity = NotionIdentityQuery("Synthetic Key", "rich_text", ("synthetic-key",))

        with self.assertRaisesRegex(NotionTransportError, "pagination limit"):
            notion.query_data_source("synthetic-source", identity)
        self.assertEqual(len(http.calls), 2)

    def test_gmail_consumes_caller_deadline_before_network(self) -> None:
        class FakeClock:
            value = 0.0

            def __call__(self):
                return self.value

        class Backend:
            calls = 0

            def request(self, method, url, *, headers, body, timeout_seconds):
                self.calls += 1
                return HttpResponse(200, {}, b'{"messages":[]}')

        clock = FakeClock()
        context = RunContext.start(timeout_seconds=1, monotonic_clock=clock)
        clock.value = 1.0
        backend = Backend()
        mailbox = gmail_mailbox(HttpClient(backend), context=context)

        with self.assertRaises(HttpError) as caught:
            mailbox.scan_window(START, END)
        self.assertEqual(caught.exception.kind, HttpErrorKind.DEADLINE)
        self.assertEqual(backend.calls, 0)

    def test_gmail_transient_read_retry_remains_bounded(self) -> None:
        class Backend:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, *, headers, body, timeout_seconds):
                self.calls += 1
                if self.calls == 1:
                    return HttpResponse(429, {"Retry-After": "0"}, b"{}")
                return HttpResponse(200, {}, b'{"messages":[]}')

        backend = Backend()
        mailbox = gmail_mailbox(HttpClient(backend))

        self.assertEqual(mailbox.scan_window(START, END), ())
        self.assertEqual(backend.calls, 2)

if __name__ == "__main__":
    unittest.main()
