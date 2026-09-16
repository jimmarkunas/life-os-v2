"""Thin Google Calendar transport for bounded event reads and mutations only."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from lifeos.core.config import RuntimeConfig
from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext

_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
_READ_RETRY = RetryPolicy(max_attempts=2, backoff_seconds=0.1, max_backoff_seconds=1.0)
_NO_RETRY = RetryPolicy(max_attempts=1)
GOOGLE_CALENDAR_ACCESS_TOKEN_FIELD = "GOOGLE_CALENDAR_API_TOKEN"
GOOGLE_CALENDAR_ID_FIELD = "GOOGLE_CALENDAR_ID"
DEFAULT_CALENDAR_ID = "primary"
DEFAULT_MAX_EVENT_PAGES = 10
MAX_EVENT_PAGES = 20
MAX_EVENT_PAGE_SIZE = 250


class GoogleCalendarTransportError(RuntimeError):
    """Safe Calendar transport error; scheduling/business policy belongs to callers."""


class GoogleCalendarTransport:
    def __init__(
        self,
        *,
        context: RunContext,
        http: HttpClient,
        access_token: str,
        calendar_id: str = DEFAULT_CALENDAR_ID,
        max_event_pages: int = DEFAULT_MAX_EVENT_PAGES,
    ) -> None:
        if not access_token:
            raise ValueError("Google Calendar access token is required")
        normalized_calendar = str(calendar_id).strip()
        if not normalized_calendar:
            raise ValueError("calendar_id is required")
        pages = int(max_event_pages)
        if pages < 1 or pages > MAX_EVENT_PAGES:
            raise ValueError(f"max_event_pages must be between 1 and {MAX_EVENT_PAGES}")
        self._context = context
        self._http = http
        self._token = access_token
        self._calendar_id = normalized_calendar
        self._max_event_pages = pages

    @classmethod
    def from_config(
        cls,
        *,
        context: RunContext,
        http: HttpClient,
        config: RuntimeConfig,
        access_token_field: str = GOOGLE_CALENDAR_ACCESS_TOKEN_FIELD,
        calendar_id_field: str = GOOGLE_CALENDAR_ID_FIELD,
        max_event_pages: int = DEFAULT_MAX_EVENT_PAGES,
    ) -> "GoogleCalendarTransport":
        """Construct from already-validated private runtime configuration."""
        return cls(
            context=context,
            http=http,
            access_token=config.require(access_token_field),
            calendar_id=config.optional(calendar_id_field) or DEFAULT_CALENDAR_ID,
            max_event_pages=max_event_pages,
        )

    def list_events(
        self,
        start: datetime,
        end: datetime,
        *,
        page_size: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        _validate_window(start, end)
        size = max(1, min(int(page_size), MAX_EVENT_PAGE_SIZE))
        token: str | None = None
        seen_tokens: set[str] = set()
        events: list[dict[str, Any]] = []

        for _ in range(self._max_event_pages):
            params = {
                "timeMin": _rfc3339(start),
                "timeMax": _rfc3339(end),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": str(size),
            }
            if token:
                params["pageToken"] = token
            payload = self._http.request_json(
                self._context,
                "GET",
                f"{self._events_url()}?{urlencode(params)}",
                headers=self._headers(),
                timeout_seconds=10.0,
                retry=_READ_RETRY,
            )
            page = _require_object(payload, "Google Calendar events response")
            for item in page.get("items") or []:
                if isinstance(item, dict):
                    events.append(item)
            next_token = page.get("nextPageToken")
            if not next_token:
                return tuple(events)
            token = str(next_token)
            if token in seen_tokens:
                raise GoogleCalendarTransportError("Google Calendar pagination token repeated")
            seen_tokens.add(token)

        raise GoogleCalendarTransportError("Google Calendar event listing exceeded configured page limit")

    def get_event(self, event_id: str) -> dict[str, Any]:
        event_ref = _required_ref(event_id, "event_id")
        payload = self._http.request_json(
            self._context,
            "GET",
            f"{self._events_url()}/{quote(event_ref, safe='')}",
            headers=self._headers(),
            timeout_seconds=10.0,
            retry=_READ_RETRY,
        )
        return _require_object(payload, "Google Calendar event response")

    def create_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        if not event:
            raise ValueError("event payload is required")
        payload = self._http.request_json(
            self._context,
            "POST",
            self._events_url(),
            headers=self._headers(),
            json_body=dict(event),
            timeout_seconds=10.0,
            retry=_NO_RETRY,
            expected_statuses=(200, 201),
        )
        created = _require_object(payload, "Google Calendar create response")
        event_id = created.get("id")
        if not event_id:
            raise GoogleCalendarTransportError("Google Calendar create response missing event identity")
        return self.get_event(str(event_id))

    def update_event(self, event_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        event_ref = _required_ref(event_id, "event_id")
        if not changes:
            raise ValueError("event changes are required")
        self._http.request_json(
            self._context,
            "PATCH",
            f"{self._events_url()}/{quote(event_ref, safe='')}",
            headers=self._headers(),
            json_body=dict(changes),
            timeout_seconds=10.0,
            retry=_NO_RETRY,
            expected_statuses=(200,),
        )
        return self.get_event(event_ref)

    def _events_url(self) -> str:
        return f"{_CALENDAR_API}/calendars/{quote(self._calendar_id, safe='')}/events"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }


def _validate_window(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("calendar window timestamps must be timezone-aware")
    if end <= start:
        raise ValueError("calendar window end must be after start")


def _rfc3339(value: datetime) -> str:
    text = value.isoformat()
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def _required_ref(value: str, label: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _require_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise GoogleCalendarTransportError(f"{label} was not an object")
    return payload
