from __future__ import annotations

import base64
import threading
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

from lifeos.core.runtime import RunContext
from lifeos.integrations.outlook import OutlookMailboxTransport
from tests.testkit.builders import gmail_mailbox, SyntheticMessage


class FakeHttp:
    def __init__(self) -> None:
        self.calls = []
        self._lock = threading.Lock()

    def request_json(self, context, method, url, **kwargs):
        with self._lock:
            self.calls.append((method, url, kwargs))
        if "gmail.googleapis.com" in url:
            return self._gmail(method, url, kwargs)
        if "graph.microsoft.com" in url:
            return self._outlook(method, url, kwargs)
        raise AssertionError("unexpected URL")

    def _gmail(self, method, url, kwargs):
        if method == "GET" and "/messages?" in url:
            query = parse_qs(urlparse(url).query)
            if query.get("labelIds") == ["INBOX"]:
                decoded_query = unquote(query.get("q", [""])[0])
                assert "after:1767225600" in decoded_query
                assert "before:1767312000" in decoded_query
                return {"messages": [{"id": "msg-b"}, {"id": "msg-a"}]}
            return {"messages": [{"id": "msg-b"}, {"id": "msg-a"}]}
        if method == "GET" and "/messages/msg-a?format=full" in url:
            return self._gmail_message("msg-a", 1000, "A")
        if method == "GET" and "/messages/msg-b?format=full" in url:
            return self._gmail_message("msg-b", 2000, "B")
        if method == "GET" and url.endswith("/labels"):
            return {"labels": [{"id": "label-news", "name": "J Newsletters"}]}
        if method == "POST" and url.endswith("/messages/msg-a/modify"):
            self.assert_payload(kwargs, {"addLabelIds": ["label-news"], "removeLabelIds": ["INBOX"]})
            return {"id": "msg-a"}
        raise AssertionError(f"unexpected Gmail call {method}")

    @staticmethod
    def _gmail_message(message_id, internal_date, subject):
        body = base64.urlsafe_b64encode(b"synthetic job alert").decode("ascii").rstrip("=")
        return {
            "id": message_id,
            "internalDate": str(internal_date),
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "alerts@example.invalid"},
                    {"name": "Subject", "value": subject},
                    {"name": "List-Unsubscribe", "value": "synthetic"},
                ],
                "body": {"data": body},
            },
        }

    def _outlook(self, method, url, kwargs):
        if method == "GET" and "/me/messages?" in url:
            return {
                "value": [
                    {
                        "id": "outlook-msg",
                        "receivedDateTime": "2026-01-01T12:00:00Z",
                        "from": {"emailAddress": {"address": "alerts@example.invalid"}},
                        "subject": "Synthetic alert",
                        "body": {"contentType": "text", "content": "synthetic body"},
                        "internetMessageHeaders": [{"name": "Precedence", "value": "bulk"}],
                    }
                ]
            }
        if method == "GET" and "/me/mailFolders?" in url:
            return {"value": [{"id": "folder-news", "displayName": "J Newsletters"}]}
        if method == "POST" and url.endswith("/messages/outlook-msg/move"):
            self.assert_payload(kwargs, {"destinationId": "folder-news"})
            return {"id": "outlook-msg"}
        raise AssertionError(f"unexpected Outlook call {method}")

    @staticmethod
    def assert_payload(kwargs, expected):
        if kwargs.get("json_body") != expected:
            raise AssertionError((kwargs.get("json_body"), expected))


class MailTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = RunContext.start(timeout_seconds=45)
        self.http = FakeHttp()
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.end = datetime(2026, 1, 2, tzinfo=timezone.utc)

    def test_gmail_implements_agent2_port_shape(self) -> None:
        mailbox = gmail_mailbox(self.http, context=self.context, max_workers=2)
        messages = mailbox.scan_window(self.start, self.end)
        self.assertEqual(mailbox.provider, "gmail")
        self.assertEqual([m.message_id for m in messages], ["msg-a", "msg-b"])
        self.assertEqual(messages[0].body_text, "synthetic job alert")
        self.assertFalse(any("format=raw" in call[1] for call in self.http.calls))
        mailbox.route_to_newsletters("msg-a", "J Newsletters")

    def test_gmail_scan_inbox_window_uses_inbox_label_and_existing_hydration(self) -> None:
        mailbox = gmail_mailbox(self.http, context=self.context, max_workers=2)

        messages = mailbox.scan_inbox_window(self.start, self.end)

        self.assertEqual([m.message_id for m in messages], ["msg-a", "msg-b"])
        self.assertEqual(messages[0].body_text, "synthetic job alert")
        list_calls = [call for call in self.http.calls if call[0] == "GET" and "/messages?" in call[1]]
        self.assertEqual(len(list_calls), 1)
        params = parse_qs(urlparse(list_calls[0][1]).query)
        self.assertEqual(params["labelIds"], ["INBOX"])
        decoded_query = unquote(params["q"][0])
        self.assertIn("after:1767225600", decoded_query)
        self.assertIn("before:1767312000", decoded_query)

        self.http.calls.clear()
        mailbox.scan_window(self.start, self.end)
        list_call = next(call for call in self.http.calls if call[0] == "GET" and "/messages?" in call[1])
        self.assertNotIn("labelIds", parse_qs(urlparse(list_call[1]).query))

    def test_outlook_implements_agent2_port_shape(self) -> None:
        mailbox = OutlookMailboxTransport(
            context=self.context,
            http=self.http,
            access_token="synthetic-token",
            message_factory=SyntheticMessage,
        )
        messages = mailbox.scan_window(self.start, self.end)
        self.assertEqual(mailbox.provider, "outlook")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].sender, "alerts@example.invalid")
        mailbox.route_to_newsletters("outlook-msg", "J Newsletters")


from lifeos.integrations.notion import NotionIdentityQuery, NotionTransport


class FakeNotionHttp:
    def __init__(self) -> None:
        self.calls = []
        self.query_count = 0

    def request_json(self, context, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "POST" and "/data_sources/" in url and url.endswith("/query"):
            self.query_count += 1
            if self.query_count == 1:
                return {"results": [{"id": "page-1"}], "has_more": True, "next_cursor": "cursor-2"}
            return {"results": [{"id": "page-2"}], "has_more": False, "next_cursor": None}
        if method == "POST" and url.endswith("/pages"):
            return {"id": "created-page", "properties": kwargs["json_body"]["properties"]}
        if method == "PATCH" and "/pages/" in url:
            return {"id": "updated-page", "properties": kwargs["json_body"]["properties"]}
        if method == "GET" and "/pages/" in url:
            return {"id": "updated-page", "properties": {"Synthetic Key": {"rich_text": []}}}
        raise AssertionError((method, url))


class NotionTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.http = FakeNotionHttp()
        self.transport = NotionTransport(
            context=RunContext.start(timeout_seconds=45),
            http=self.http,
            access_token="synthetic-token",
        )

    def test_query_requires_bounded_identity_values(self) -> None:
        identity = NotionIdentityQuery(
            property_name="Synthetic Key",
            property_type="rich_text",
            values=("key-1", "key-2"),
        )
        rows = self.transport.query_data_source("synthetic-data-source", identity)
        self.assertEqual([row["id"] for row in rows], ["page-1", "page-2"])
        first_body = self.http.calls[0][2]["json_body"]
        self.assertIn("filter", first_body)
        self.assertEqual(len(first_body["filter"]["or"]), 2)
        with self.assertRaises(ValueError):
            NotionIdentityQuery("Synthetic Key", "rich_text", ())
        with self.assertRaises(ValueError):
            NotionIdentityQuery("Synthetic Key", "rich_text", tuple(str(i) for i in range(51)))

    def test_create_update_and_authoritative_reread_are_transport_only(self) -> None:
        created = self.transport.create_page("synthetic-data-source", {"Synthetic Key": {"rich_text": []}})
        self.assertEqual(created["id"], "created-page")
        updated = self.transport.update_page("created-page", {"Synthetic Key": {"rich_text": []}})
        self.assertEqual(updated["id"], "updated-page")
        reread = self.transport.get_page("updated-page")
        self.assertEqual(reread["id"], "updated-page")

    def test_write_calls_use_bounded_transport_retry(self) -> None:
        self.transport.create_page("synthetic-data-source", {})
        call = self.http.calls[-1]
        self.assertEqual(call[2]["retry"].max_attempts, 2)
