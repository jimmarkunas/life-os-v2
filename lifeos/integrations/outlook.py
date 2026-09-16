"""Thin Microsoft Graph mail transport: acquisition and folder move mechanics only."""
from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any, Generic, TypeVar
from urllib.parse import quote, urlencode

from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext

from .mailbox import MailMessageFactory, MailboxTransportError

T = TypeVar("T")
_GRAPH_API = "https://graph.microsoft.com/v1.0"
_READ_RETRY = RetryPolicy(max_attempts=2, backoff_seconds=0.1, max_backoff_seconds=1.0)
_NO_RETRY = RetryPolicy(max_attempts=1)


class OutlookMailboxTransport(Generic[T]):
    provider = "outlook"

    def __init__(
        self,
        *,
        context: RunContext,
        http: HttpClient,
        access_token: str,
        message_factory: MailMessageFactory[T],
    ) -> None:
        if not access_token:
            raise ValueError("Outlook access token is required")
        self._context = context
        self._http = http
        self._token = access_token
        self._factory = message_factory
        self._folder_ids: dict[str, str] = {}
        self._folder_lock = Lock()

    def scan_window(self, start: datetime, end: datetime) -> tuple[T, ...]:
        _validate_window(start, end)
        filter_value = (
            f"receivedDateTime ge {_iso_utc(start)} and "
            f"receivedDateTime lt {_iso_utc(end)}"
        )
        params = {
            "$filter": filter_value,
            "$select": "id,receivedDateTime,from,subject,body,internetMessageHeaders",
            "$orderby": "receivedDateTime asc",
            "$top": "100",
        }
        url: str | None = f"{_GRAPH_API}/me/messages?{urlencode(params)}"
        messages: list[T] = []
        while url:
            payload = self._http.request_json(
                self._context,
                "GET",
                url,
                headers=self._headers(),
                timeout_seconds=10.0,
                retry=_READ_RETRY,
            )
            if not isinstance(payload, dict):
                raise MailboxTransportError("Outlook messages response was not an object")
            for item in payload.get("value") or []:
                if isinstance(item, dict):
                    messages.append(self._to_message(item))
            next_link = payload.get("@odata.nextLink")
            url = str(next_link) if next_link else None
        messages.sort(key=lambda item: (getattr(item, "received_at"), getattr(item, "message_id")))
        return tuple(messages)

    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None:
        if not message_id:
            raise ValueError("message_id is required")
        if not boundary_name:
            raise ValueError("boundary_name is required")
        folder_id = self._resolve_folder_id(boundary_name)
        url = f"{_GRAPH_API}/me/messages/{quote(message_id, safe='')}/move"
        self._http.request_json(
            self._context,
            "POST",
            url,
            headers=self._headers(),
            json_body={"destinationId": folder_id},
            timeout_seconds=10.0,
            retry=_NO_RETRY,
        )

    def _to_message(self, item: dict[str, Any]) -> T:
        message_id = str(item.get("id") or "")
        if not message_id:
            raise MailboxTransportError("Outlook message missing provider id")
        received_at = _parse_graph_datetime(item.get("receivedDateTime"))
        sender = ""
        from_value = item.get("from")
        if isinstance(from_value, dict):
            email = from_value.get("emailAddress")
            if isinstance(email, dict):
                sender = str(email.get("address") or "")
        headers = {
            str(header.get("name")): str(header.get("value") or "")
            for header in (item.get("internetMessageHeaders") or [])
            if isinstance(header, dict) and header.get("name")
        }
        body = item.get("body")
        body_text = str(body.get("content") or "") if isinstance(body, dict) else ""
        return self._factory(
            provider=self.provider,
            message_id=message_id,
            received_at=received_at,
            sender=sender,
            subject=str(item.get("subject") or ""),
            body_text=body_text,
            headers=headers,
        )

    def _resolve_folder_id(self, name: str) -> str:
        with self._folder_lock:
            cached = self._folder_ids.get(name)
            if cached:
                return cached
            params = {
                "$filter": f"displayName eq '{name.replace(chr(39), chr(39) * 2)}'",
                "$select": "id,displayName",
                "$top": "10",
            }
            url = f"{_GRAPH_API}/me/mailFolders?{urlencode(params)}"
            payload = self._http.request_json(
                self._context,
                "GET",
                url,
                headers=self._headers(),
                timeout_seconds=10.0,
                retry=_READ_RETRY,
            )
            if not isinstance(payload, dict):
                raise MailboxTransportError("Outlook folders response was not an object")
            for folder in payload.get("value") or []:
                if isinstance(folder, dict) and folder.get("displayName") == name and folder.get("id"):
                    folder_id = str(folder["id"])
                    self._folder_ids[name] = folder_id
                    return folder_id
        raise MailboxTransportError("Outlook newsletter folder not found")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}


def _validate_window(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("mail window timestamps must be timezone-aware")
    if end <= start:
        raise ValueError("mail window end must be after start")


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_graph_datetime(value: Any) -> datetime:
    if not value:
        raise MailboxTransportError("Outlook message timestamp missing")
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MailboxTransportError("Outlook message timestamp invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
