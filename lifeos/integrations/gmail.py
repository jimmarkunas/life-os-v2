"""Thin Gmail transport: mailbox acquisition and label/folder mechanics only."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Generic, Mapping, TypeVar
from urllib.parse import quote, urlencode

from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext

from .mailbox import MailMessageFactory, MailboxTransportError

T = TypeVar("T")
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_READ_RETRY = RetryPolicy(max_attempts=2, backoff_seconds=0.1, max_backoff_seconds=1.0)
_NO_RETRY = RetryPolicy(max_attempts=1)


class GmailMailboxTransport(Generic[T]):
    provider = "gmail"

    def __init__(
        self,
        *,
        context: RunContext,
        http: HttpClient,
        access_token: str,
        message_factory: MailMessageFactory[T],
        user_id: str = "me",
        max_workers: int = 8,
    ) -> None:
        if not access_token:
            raise ValueError("Gmail access token is required")
        self._context = context
        self._http = http
        self._token = access_token
        self._factory = message_factory
        self._user_id = user_id
        self._max_workers = max(1, min(int(max_workers), 16))
        self._label_ids: dict[str, str] = {}
        self._label_lock = Lock()

    def scan_window(self, start: datetime, end: datetime) -> tuple[T, ...]:
        _validate_window(start, end)
        ids = self._list_message_ids(start, end)
        if not ids:
            return ()

        messages: list[T] = []
        failures = 0
        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(ids))) as pool:
            futures = {pool.submit(self._fetch_message, message_id): message_id for message_id in ids}
            for future in as_completed(futures):
                try:
                    messages.append(future.result())
                except Exception:
                    failures += 1
        if failures:
            raise MailboxTransportError(f"Gmail message detail acquisition incomplete: {failures} failed")
        messages.sort(key=lambda item: (getattr(item, "received_at"), getattr(item, "message_id")))
        return tuple(messages)

    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None:
        if not message_id:
            raise ValueError("message_id is required")
        if not boundary_name:
            raise ValueError("boundary_name is required")
        label_id = self._resolve_label_id(boundary_name)
        path = f"{_GMAIL_API}/users/{quote(self._user_id, safe='')}/messages/{quote(message_id, safe='')}/modify"
        self._http.request_json(
            self._context,
            "POST",
            path,
            headers=self._headers(),
            json_body={"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]},
            timeout_seconds=10.0,
            retry=_NO_RETRY,
        )

    def _list_message_ids(self, start: datetime, end: datetime) -> tuple[str, ...]:
        query = f"after:{int(start.timestamp())} before:{int(end.timestamp())}"
        page_token: str | None = None
        ids: list[str] = []
        while True:
            params = {"q": query, "maxResults": "500"}
            if page_token:
                params["pageToken"] = page_token
            url = f"{_GMAIL_API}/users/{quote(self._user_id, safe='')}/messages?{urlencode(params)}"
            payload = self._http.request_json(
                self._context,
                "GET",
                url,
                headers=self._headers(),
                timeout_seconds=10.0,
                retry=_READ_RETRY,
            )
            if not isinstance(payload, dict):
                raise MailboxTransportError("Gmail list response was not an object")
            for item in payload.get("messages") or []:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
            token = payload.get("nextPageToken")
            if not token:
                break
            page_token = str(token)
        return tuple(ids)

    def _fetch_message(self, message_id: str) -> T:
        url = f"{_GMAIL_API}/users/{quote(self._user_id, safe='')}/messages/{quote(message_id, safe='')}?format=full"
        payload = self._http.request_json(
            self._context,
            "GET",
            url,
            headers=self._headers(),
            timeout_seconds=10.0,
            retry=_READ_RETRY,
        )
        if not isinstance(payload, dict):
            raise MailboxTransportError("Gmail message response was not an object")
        headers = _gmail_headers(payload)
        internal_date = payload.get("internalDate")
        try:
            received_at = datetime.fromtimestamp(int(str(internal_date)) / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError) as exc:
            raise MailboxTransportError("Gmail message timestamp invalid") from exc
        return self._factory(
            provider=self.provider,
            message_id=str(payload.get("id") or message_id),
            received_at=received_at,
            sender=headers.get("From", ""),
            subject=headers.get("Subject", ""),
            body_text=_gmail_body_text(payload.get("payload") or {}),
            headers=headers,
        )

    def _resolve_label_id(self, name: str) -> str:
        with self._label_lock:
            cached = self._label_ids.get(name)
            if cached:
                return cached
            url = f"{_GMAIL_API}/users/{quote(self._user_id, safe='')}/labels"
            payload = self._http.request_json(
                self._context,
                "GET",
                url,
                headers=self._headers(),
                timeout_seconds=10.0,
                retry=_READ_RETRY,
            )
            if not isinstance(payload, dict):
                raise MailboxTransportError("Gmail labels response was not an object")
            for label in payload.get("labels") or []:
                if isinstance(label, dict) and label.get("name") == name and label.get("id"):
                    label_id = str(label["id"])
                    self._label_ids[name] = label_id
                    return label_id
        raise MailboxTransportError("Gmail newsletter label not found")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}


def _validate_window(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("mail window timestamps must be timezone-aware")
    if end <= start:
        raise ValueError("mail window end must be after start")


def _gmail_headers(message: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    payload = message.get("payload") or {}
    if isinstance(payload, dict):
        for item in payload.get("headers") or []:
            if isinstance(item, dict) and item.get("name"):
                result[str(item["name"])] = str(item.get("value") or "")
    return result


def _gmail_body_text(part: Mapping[str, Any]) -> str:
    mime_type = str(part.get("mimeType") or "")
    body = part.get("body") or {}
    if mime_type == "text/plain" and isinstance(body, dict) and body.get("data"):
        return _decode_base64url(str(body["data"]))
    pieces: list[str] = []
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            text = _gmail_body_text(child)
            if text:
                pieces.append(text)
    if pieces:
        return "\n".join(pieces)
    if isinstance(body, dict) and body.get("data"):
        return _decode_base64url(str(body["data"]))
    return ""


def _decode_base64url(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8", errors="replace")
    except (ValueError, UnicodeEncodeError):
        return ""
