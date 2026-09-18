from __future__ import annotations

import base64
import json
import socket
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from lifeos.core.config import ConfigField, RuntimeConfig
from lifeos.core.http import HttpClient, HttpError, HttpErrorKind, HttpResponse
from lifeos.core.runtime import RunContext
from lifeos.integrations.calendar import GoogleCalendarTransport, GoogleCalendarTransportError
from lifeos.integrations.jira import JiraTransport, JiraTransportError


class RecordingBackend:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.outcomes:
            raise AssertionError("unexpected network call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def response(payload, status: int = 200) -> HttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return HttpResponse(status, {}, body)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class JiraTransportTests(unittest.TestCase):
    def test_runtime_config_builds_authenticated_client_without_exposing_secret(self) -> None:
        config = RuntimeConfig.load(
            (
                ConfigField("JIRA_BASE_URL"),
                ConfigField("JIRA_API_TOKEN"),
                ConfigField("JIRA_USER_EMAIL", required=False),
            ),
            environ={
                "JIRA_BASE_URL": "https://jira.example.invalid",
                "JIRA_API_TOKEN": "synthetic-jira-token",
                "JIRA_USER_EMAIL": "synthetic-user@example.invalid",
            },
        )
        backend = RecordingBackend([response({"id": "synthetic-issue", "key": "SYN-1"})])
        transport = JiraTransport.from_config(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(backend),
            config=config,
        )

        issue = transport.get_issue("SYN-1")

        self.assertEqual(issue["key"], "SYN-1")
        self.assertNotIn("synthetic-jira-token", repr(config))
        auth = str(backend.calls[0]["headers"]["Authorization"])
        self.assertTrue(auth.startswith("Basic "))
        decoded = base64.b64decode(auth.removeprefix("Basic ")).decode("utf-8")
        self.assertEqual(decoded, "synthetic-user@example.invalid:synthetic-jira-token")

    def test_search_is_bounded_and_repeated_tokens_fail_closed(self) -> None:
        bounded_backend = RecordingBackend(
            [
                response({"issues": [{"key": "SYN-1"}], "isLast": False, "nextPageToken": "page-2"}),
                response({"issues": [{"key": "SYN-2"}], "isLast": False, "nextPageToken": "page-3"}),
            ]
        )
        bounded = JiraTransport(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(bounded_backend),
            base_url="https://jira.example.invalid",
            access_token="synthetic-token",
            max_search_pages=2,
        )
        with self.assertRaises(JiraTransportError):
            bounded.search_issues("project = SYN")
        self.assertEqual(len(bounded_backend.calls), 2)

        repeated_backend = RecordingBackend(
            [
                response({"issues": [], "isLast": False, "nextPageToken": "repeat"}),
                response({"issues": [], "isLast": False, "nextPageToken": "repeat"}),
            ]
        )
        repeated = JiraTransport(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(repeated_backend),
            base_url="https://jira.example.invalid",
            access_token="synthetic-token",
        )
        with self.assertRaisesRegex(JiraTransportError, "pagination token repeated"):
            repeated.search_issues("project = SYN")
        self.assertEqual(len(repeated_backend.calls), 2)

    def test_exhausted_deadline_prevents_jira_network_work(self) -> None:
        clock = FakeClock()
        context = RunContext.start(timeout_seconds=1, monotonic_clock=clock)
        clock.value = 2
        backend = RecordingBackend([])
        transport = JiraTransport(
            context=context,
            http=HttpClient(backend),
            base_url="https://jira.example.invalid",
            access_token="synthetic-token",
        )
        with self.assertRaises(HttpError) as caught:
            transport.get_issue("SYN-1")
        self.assertEqual(caught.exception.kind, HttpErrorKind.DEADLINE)
        self.assertEqual(backend.calls, [])

    def test_jira_read_retry_is_bounded(self) -> None:
        backend = RecordingBackend(
            [socket.timeout("synthetic transient"), response({"id": "synthetic-issue", "key": "SYN-1"})]
        )
        transport = JiraTransport(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(backend),
            base_url="https://jira.example.invalid",
            access_token="synthetic-token",
        )
        self.assertEqual(transport.get_issue("SYN-1")["key"], "SYN-1")
        self.assertEqual(len(backend.calls), 2)

    def test_jira_create_update_shapes_and_authoritative_read_back(self) -> None:
        backend = RecordingBackend(
            [
                response({"id": "synthetic-id", "key": "SYN-1"}, status=201),
                response({"id": "synthetic-id", "key": "SYN-1", "fields": {"summary": "Created"}}),
                response(None, status=204),
                response({"id": "synthetic-id", "key": "SYN-1", "fields": {"summary": "Updated"}}),
            ]
        )
        transport = JiraTransport(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(backend),
            base_url="https://jira.example.invalid",
            access_token="synthetic-token",
        )

        created = transport.create_issue({"summary": "Created", "project": {"key": "SYN"}})
        updated = transport.update_issue("SYN-1", {"summary": "Updated"})

        self.assertEqual(created["fields"]["summary"], "Created")
        self.assertEqual(updated["fields"]["summary"], "Updated")
        self.assertEqual([call["method"] for call in backend.calls], ["POST", "GET", "PUT", "GET"])
        create_body = json.loads(bytes(backend.calls[0]["body"]).decode("utf-8"))
        update_body = json.loads(bytes(backend.calls[2]["body"]).decode("utf-8"))
        self.assertEqual(create_body["fields"]["summary"], "Created")
        self.assertEqual(update_body, {"fields": {"summary": "Updated"}})

    def test_jira_api_error_does_not_expose_token_or_private_issue_content(self) -> None:
        credential_value = "synthetic" + "-private-token"
        backend = RecordingBackend([response({"error": "private synthetic issue content"}, status=400)])
        transport = JiraTransport(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(backend),
            base_url="https://jira.example.invalid",
            access_token=credential_value,
        )
        with self.assertRaises(HttpError) as caught:
            transport.get_issue("SYN-PRIVATE")
        text = str(caught.exception)
        self.assertNotIn(credential_value, text)
        self.assertNotIn("private synthetic issue content", text)
        self.assertNotIn("jira.example.invalid", text)


class GoogleCalendarTransportTests(unittest.TestCase):
    def test_runtime_config_builds_bearer_client_without_exposing_secret(self) -> None:
        config = RuntimeConfig.load(
            (
                ConfigField("GOOGLE_CALENDAR_API_TOKEN"),
                ConfigField("GOOGLE_CALENDAR_ID", required=False),
            ),
            environ={
                "GOOGLE_CALENDAR_API_TOKEN": "synthetic-calendar-token",
                "GOOGLE_CALENDAR_ID": "synthetic-calendar",
            },
        )
        backend = RecordingBackend([response({"id": "synthetic-event"})])
        transport = GoogleCalendarTransport.from_config(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(backend),
            config=config,
        )

        self.assertEqual(transport.get_event("synthetic-event")["id"], "synthetic-event")
        self.assertEqual(
            backend.calls[0]["headers"]["Authorization"],
            "Bearer synthetic-calendar-token",
        )

    def test_event_listing_has_explicit_window_and_bounded_pagination(self) -> None:
        backend = RecordingBackend(
            [
                response({"items": [{"id": "event-1"}], "nextPageToken": "page-2"}),
                response({"items": [{"id": "event-2"}], "nextPageToken": "page-3"}),
            ]
        )
        transport = GoogleCalendarTransport(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(backend),
            access_token="synthetic-token",
            max_event_pages=2,
        )
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with self.assertRaises(GoogleCalendarTransportError):
            transport.list_events(start, end)
        self.assertEqual(len(backend.calls), 2)
        query = parse_qs(urlsplit(str(backend.calls[0]["url"])).query)
        self.assertEqual(query["timeMin"], ["2026-01-01T00:00:00Z"])
        self.assertEqual(query["timeMax"], ["2026-01-02T00:00:00Z"])

    def test_calendar_repeated_page_token_fails_closed(self) -> None:
        backend = RecordingBackend(
            [
                response({"items": [], "nextPageToken": "repeat"}),
                response({"items": [], "nextPageToken": "repeat"}),
            ]
        )
        transport = GoogleCalendarTransport(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(backend),
            access_token="synthetic-token",
        )
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with self.assertRaisesRegex(GoogleCalendarTransportError, "pagination token repeated"):
            transport.list_events(start, end)
        self.assertEqual(len(backend.calls), 2)

    def test_exhausted_deadline_prevents_calendar_network_work(self) -> None:
        clock = FakeClock()
        context = RunContext.start(timeout_seconds=1, monotonic_clock=clock)
        clock.value = 2
        backend = RecordingBackend([])
        transport = GoogleCalendarTransport(
            context=context,
            http=HttpClient(backend),
            access_token="synthetic-token",
        )
        with self.assertRaises(HttpError) as caught:
            transport.get_event("synthetic-event")
        self.assertEqual(caught.exception.kind, HttpErrorKind.DEADLINE)
        self.assertEqual(backend.calls, [])

    def test_calendar_read_retry_is_bounded(self) -> None:
        backend = RecordingBackend(
            [socket.timeout("synthetic transient"), response({"id": "synthetic-event"})]
        )
        transport = GoogleCalendarTransport(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(backend),
            access_token="synthetic-token",
        )
        self.assertEqual(transport.get_event("synthetic-event")["id"], "synthetic-event")
        self.assertEqual(len(backend.calls), 2)

    def test_calendar_create_update_shapes_and_authoritative_read_back(self) -> None:
        backend = RecordingBackend(
            [
                response({"id": "synthetic-event"}, status=201),
                response({"id": "synthetic-event", "summary": "Created"}),
                response({"id": "synthetic-event", "summary": "Updated"}),
                response({"id": "synthetic-event", "summary": "Updated"}),
            ]
        )
        transport = GoogleCalendarTransport(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(backend),
            access_token="synthetic-token",
        )
        event = {
            "summary": "Created",
            "start": {"dateTime": "2026-01-01T10:00:00Z"},
            "end": {"dateTime": "2026-01-01T11:00:00Z"},
        }

        created = transport.create_event(event)
        updated = transport.update_event("synthetic-event", {"summary": "Updated"})

        self.assertEqual(created["summary"], "Created")
        self.assertEqual(updated["summary"], "Updated")
        self.assertEqual([call["method"] for call in backend.calls], ["POST", "GET", "PATCH", "GET"])
        create_body = json.loads(bytes(backend.calls[0]["body"]).decode("utf-8"))
        update_body = json.loads(bytes(backend.calls[2]["body"]).decode("utf-8"))
        self.assertEqual(create_body["summary"], "Created")
        self.assertEqual(update_body, {"summary": "Updated"})

    def test_calendar_api_error_does_not_expose_token_or_event_content(self) -> None:
        credential_value = "synthetic" + "-private-calendar-token"
        backend = RecordingBackend([response({"error": "private synthetic event content"}, status=403)])
        transport = GoogleCalendarTransport(
            context=RunContext.start(timeout_seconds=45),
            http=HttpClient(backend),
            access_token=credential_value,
        )
        with self.assertRaises(HttpError) as caught:
            transport.get_event("synthetic-private-event")
        text = str(caught.exception)
        self.assertNotIn(credential_value, text)
        self.assertNotIn("private synthetic event content", text)
        self.assertNotIn("synthetic-private-event", text)


if __name__ == "__main__":
    unittest.main()
