"""Thin Gmail transport: mailbox acquisition and label/folder mechanics only."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from time import sleep
from typing import Any, Generic, Mapping, Sequence, TypeVar
from urllib.parse import quote, urlencode

from lifeos.core.backlog import consume_bounded_backlog
from lifeos.core.config import RuntimeConfig
from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext
from lifeos.newsletter.models import RoutedNewsletterMessage

from .mailbox import MailMessageFactory, MailboxTransportError

T = TypeVar("T")
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_READ_RETRY = RetryPolicy(
    max_attempts=2,
    backoff_seconds=0.1,
    max_backoff_seconds=1.0,
    retryable_api_reasons=("rateLimitExceeded",),
)
_NO_RETRY = RetryPolicy(max_attempts=1)
MAX_MESSAGE_DETAIL_WORKERS = 8
DEFAULT_MAX_LIST_PAGES = 10
MAX_LIST_PAGES = 20
GMAIL_ACCESS_TOKEN_FIELD = "GMAIL_API_TOKEN"
PROCESSED_LABEL_SUFFIX = "Processed"
BACKLOG_DETAIL_PACING_SECONDS = 0.25
BACKLOG_BATCH_SIZE = 10
BACKLOG_MESSAGE_RUNTIME_RESERVE_SECONDS = 20.0
BACKLOG_PER_MESSAGE_ADMISSION_SECONDS = 2.0
# Headers sufficient for DeterministicMailClassifier's AUTOMATED_JOB_SOURCE
# routing decision (sender/subject plus automation/source-adapter headers).
# That decision does not use body_text, so format=metadata with exactly
# these headers is sufficient for safe Inbox staging classification.
INBOX_METADATA_HEADERS = (
    "From",
    "Subject",
    "List-Unsubscribe",
    "List-ID",
    "Precedence",
    "Auto-Submitted",
    "X-LifeOS-Source-Adapter",
    "X-LifeOS-Job-Source",
)


@dataclass(frozen=True, slots=True)
class GmailNewsletterBacklogSnapshot:
    pending_source_messages: int
    oldest_pending_age_seconds: int | None
# scan_window bounds concurrency (max_workers in-flight requests) but that
# alone does not bound the per-minute call RATE against Gmail's 6,000
# quota-units/min per-user ceiling: fast responses at even modest concurrency
# can sustain a rate well past it. Pace between chunks of concurrent calls so
# the sustained rate stays well under quota regardless of response latency.
SCAN_CHUNK_PACING_SECONDS = 1.0


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
        max_list_pages: int = DEFAULT_MAX_LIST_PAGES,
    ) -> None:
        if not access_token:
            raise ValueError("Gmail access token is required")
        pages = int(max_list_pages)
        if pages < 1 or pages > MAX_LIST_PAGES:
            raise ValueError(f"max_list_pages must be between 1 and {MAX_LIST_PAGES}")
        self._context = context
        self._http = http
        self._token = access_token
        self._factory = message_factory
        self._user_id = user_id
        self._max_workers = max(1, min(int(max_workers), MAX_MESSAGE_DETAIL_WORKERS))
        self._max_list_pages = pages
        self._label_ids: dict[str, str] = {}
        self._label_lock = Lock()

    @classmethod
    def from_config(
        cls,
        *,
        context: RunContext,
        http: HttpClient,
        config: RuntimeConfig,
        message_factory: MailMessageFactory[T],
        access_token_field: str = GMAIL_ACCESS_TOKEN_FIELD,
        user_id: str = "me",
        max_workers: int = 8,
        max_list_pages: int = DEFAULT_MAX_LIST_PAGES,
    ) -> "GmailMailboxTransport[T]":
        """Construct from already-validated runtime configuration."""
        return cls(
            context=context,
            http=http,
            access_token=config.require(access_token_field),
            message_factory=message_factory,
            user_id=user_id,
            max_workers=max_workers,
            max_list_pages=max_list_pages,
        )

    @property
    def mailbox(self) -> str:
        return self.provider

    def scan_window(self, start: datetime, end: datetime) -> tuple[T, ...]:
        return self._scan_window(start, end)

    def scan_inbox_window(self, start: datetime, end: datetime) -> tuple[T, ...]:
        return self._scan_window(start, end, label_id="INBOX")

    def scan_inbox_metadata_window(self, start: datetime, end: datetime) -> tuple[T, ...]:
        """Metadata-first Inbox acquisition for staging classification.

        Fetches Gmail format=metadata (never format=full) for every INBOX
        message in the window, requesting only the headers current
        deterministic routing policy needs. body_text/html_text/raw_mime are
        left at their MailMessage defaults ("") since body content is
        intentionally not acquired here -- full Newsletter bodies are
        hydrated later, from J Newsletters, by the existing Newsletter
        processing path.
        """
        return self._scan_window(start, end, label_id="INBOX", fetch_fn=self._fetch_message_metadata)

    def _scan_window(
        self,
        start: datetime,
        end: datetime,
        *,
        label_id: str | None = None,
        fetch_fn: "Any" = None,
    ) -> tuple[T, ...]:
        _validate_window(start, end)
        ids = self._list_message_ids(start, end, label_id=label_id)
        if not ids:
            return ()

        fetch = fetch_fn or self._fetch_message
        messages: list[T] = []
        failures = 0
        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(ids))) as pool:
            for chunk_start in range(0, len(ids), self._max_workers):
                chunk = ids[chunk_start : chunk_start + self._max_workers]
                futures = {pool.submit(fetch, message_id): message_id for message_id in chunk}
                for future in as_completed(futures):
                    try:
                        messages.append(future.result())
                    except Exception:
                        failures += 1
                if chunk_start + self._max_workers < len(ids):
                    self._pace_scan_chunk()
        if failures:
            raise MailboxTransportError(f"Gmail message detail acquisition incomplete: {failures} failed")
        messages.sort(key=lambda item: (getattr(item, "received_at"), getattr(item, "message_id")))
        return tuple(messages)

    def _pace_scan_chunk(self) -> None:
        self._context.require_time(SCAN_CHUNK_PACING_SECONDS)
        sleep(SCAN_CHUNK_PACING_SECONDS)

    def enumerate_unprocessed_ids(self, boundary_name: str) -> tuple[str, ...]:
        """Enumerate the COMPLETE age-independent Newsletter label backlog,
        oldest-first, with no bounding. Gmail lists the label newest-first;
        this reverses it so callers see genuine canonical order and can
        apply their own bounded selection (see lifeos.core.backlog)."""
        label_id = self._resolve_label_id(boundary_name)
        processed_label = _processed_label_name(boundary_name)
        try:
            self._resolve_label_id(processed_label)
        except MailboxTransportError:
            processed_label = ""
        ids = self._list_message_ids(
            None,
            None,
            label_id=label_id,
            exclude_label_name=processed_label or None,
        )
        return tuple(reversed(ids))

    def newsletter_backlog_snapshot(self, boundary_name: str, *, now: datetime) -> GmailNewsletterBacklogSnapshot:
        if now.tzinfo is None:
            raise ValueError("snapshot timestamp must be timezone-aware")
        ids = self.enumerate_unprocessed_ids(boundary_name)
        if not ids:
            return GmailNewsletterBacklogSnapshot(0, None)
        oldest = self._fetch_message_metadata_fields(ids[0])
        received_at = oldest["received_at"]
        age = max(0, int((now - received_at).total_seconds()))
        return GmailNewsletterBacklogSnapshot(len(ids), age)

    def hydrate_messages(self, message_ids: Sequence[str]) -> tuple[RoutedNewsletterMessage, ...]:
        """Hydrate exactly the given (already-selected) message IDs. Never
        enumerates or bounds on its own -- callers choose which references
        to hydrate."""
        ids = tuple(message_ids)
        if not ids:
            return ()
        messages: list[RoutedNewsletterMessage] = []
        failures: dict[str, Exception] = {}
        for index, message_id in enumerate(ids):
            try:
                messages.append(self._fetch_routed_message(message_id))
            except Exception as exc:
                failures[message_id] = exc
            if index + 1 < len(ids):
                self._pace_backlog_detail_read()
        if failures:
            retry_failures: dict[str, Exception] = {}
            failed_ids = tuple(failures)
            for index, message_id in enumerate(failed_ids):
                try:
                    messages.append(self._fetch_routed_message(message_id))
                except Exception as exc:
                    retry_failures[message_id] = exc
                if index + 1 < len(failed_ids):
                    self._pace_backlog_detail_read()
            if retry_failures:
                raise MailboxTransportError(
                    "Gmail Newsletter message acquisition incomplete: "
                    f"mailbox={self.provider} operation=fetch_unprocessed "
                    f"failed_messages={_format_message_failures(retry_failures)}"
                )
        messages.sort(key=lambda item: (item.received_at, item.message_id))
        return tuple(messages)

    def fetch_unprocessed(
        self, start: datetime, end: datetime, boundary_name: str
    ) -> tuple[RoutedNewsletterMessage, ...]:
        """Fetch one bounded batch of staged Newsletter messages not yet
        accepted. The shared platform mechanic (lifeos.core.backlog) receives
        the COMPLETE current backlog from enumerate_unprocessed_ids and is
        the sole place bounded selection happens; hydrate_messages then
        hydrates only the items it selects. Accepted messages receive the
        processed label, so later executions naturally advance through the
        same durable Gmail queue without another datastore or checkpoint.
        """
        _validate_window(start, end)
        hydrated: list[RoutedNewsletterMessage] = []

        def _hydrate_selected(batch: Sequence[str]) -> dict[str, bool]:
            hydrated.extend(self.hydrate_messages(batch))
            return {message_id: True for message_id in batch}

        def _admit_message(_message_id: str, index: int) -> bool:
            required_seconds = (
                BACKLOG_MESSAGE_RUNTIME_RESERVE_SECONDS
                + BACKLOG_PER_MESSAGE_ADMISSION_SECONDS * (index + 1)
            )
            return self._context.remaining_seconds() > required_seconds

        consume_bounded_backlog(
            enumerate_backlog=lambda: self.enumerate_unprocessed_ids(boundary_name),
            batch_size=None,
            process_batch=_hydrate_selected,
            mark_complete=lambda _message_id: None,
            admit_item=_admit_message,
        )
        return tuple(hydrated)

    def _pace_backlog_detail_read(self) -> None:
        self._context.require_time(BACKLOG_DETAIL_PACING_SECONDS)
        sleep(BACKLOG_DETAIL_PACING_SECONDS)

    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None:
        """Stage confirmed automated job mail out of Inbox immediately."""
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

    def mark_newsletter_processed(self, message_id: str, boundary_name: str) -> None:
        """Mark one staged Newsletter message accepted after canonical read-back."""
        if not message_id:
            raise ValueError("message_id is required")
        if not boundary_name:
            raise ValueError("boundary_name is required")
        processed_id = self._resolve_label_id(
            _processed_label_name(boundary_name), create_if_missing=True
        )
        path = f"{_GMAIL_API}/users/{quote(self._user_id, safe='')}/messages/{quote(message_id, safe='')}/modify"
        self._http.request_json(
            self._context,
            "POST",
            path,
            headers=self._headers(),
            json_body={"addLabelIds": [processed_id], "removeLabelIds": ["UNREAD"]},
            timeout_seconds=10.0,
            retry=_NO_RETRY,
        )

    def newsletter_retention_candidates(self, boundary_name: str) -> tuple[dict[str, Any], ...]:
        label_id = self._resolve_label_id(boundary_name)
        labels_needed = {label_id, self._resolve_label_id(_processed_label_name(boundary_name))}
        ids = self._list_message_ids(None, None, query_terms=(f'label:"{boundary_name}"', f'label:"{_processed_label_name(boundary_name)}"', "older_than:60d", "-in:trash"))
        candidates = []
        for message_id in ids:
            fields = self._fetch_message_metadata_fields(message_id, include_labels=True)
            if labels_needed.issubset(set(fields.get("label_ids", ()))) and "TRASH" not in set(fields.get("label_ids", ())):
                candidates.append(fields)
        return tuple(candidates)

    def trash_newsletter_message(self, message_id: str) -> bool:
        path = f"{_GMAIL_API}/users/{quote(self._user_id, safe='')}/messages/{quote(message_id, safe='')}/trash"
        self._http.request_json(self._context, "POST", path, headers=self._headers(), timeout_seconds=10.0, retry=_NO_RETRY)
        fields = self._fetch_message_metadata_fields(message_id, include_labels=True)
        return fields.get("message_id") == message_id and "TRASH" in set(fields.get("label_ids", ()))

    def _list_message_ids(
        self,
        start: datetime | None,
        end: datetime | None,
        *,
        label_id: str | None = None,
        exclude_label_name: str | None = None,
        query_terms: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        query_parts: list[str] = []
        if start is not None:
            query_parts.append(f"after:{int(start.timestamp())}")
        if end is not None:
            query_parts.append(f"before:{int(end.timestamp())}")
        if exclude_label_name:
            safe_label = exclude_label_name.replace('"', "")
            query_parts.append(f'-label:"{safe_label}"')
        query = " ".join((*query_terms, *query_parts)).strip()
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        ids: list[str] = []
        for _page_number in range(1, self._max_list_pages + 1):
            params = {"q": query, "maxResults": "500"}
            if label_id:
                params["labelIds"] = label_id
            if page_token:
                if page_token in seen_page_tokens:
                    raise MailboxTransportError("Gmail pagination token repeated")
                seen_page_tokens.add(page_token)
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
                return tuple(ids)
            page_token = str(token)
        raise MailboxTransportError("Gmail message listing exceeded pagination limit")

    def _fetch_message(self, message_id: str) -> T:
        fields = self._fetch_message_fields(message_id)
        fields.pop("html_text", None)
        fields.pop("raw_mime", None)
        return self._factory(provider=self.provider, **fields)

    def _fetch_message_metadata(self, message_id: str) -> T:
        fields = self._fetch_message_metadata_fields(message_id)
        return self._factory(provider=self.provider, **fields)

    def fetch_message_metadata(self, message_id: str) -> T:
        return self._fetch_message_metadata(message_id)

    def fetch_message(self, message_id: str) -> T:
        return self._fetch_message(message_id)

    def apply_amazon(self, message_id: str) -> None:
        label_id = self._resolve_label_id("Amazon")
        path = f"{_GMAIL_API}/users/{quote(self._user_id, safe='')}/messages/{quote(message_id, safe='')}/modify"
        self._http.request_json(self._context, "POST", path, headers=self._headers(), json_body={"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]}, timeout_seconds=10.0, retry=_NO_RETRY)

    def read_labels(self, message_id: str) -> frozenset[str]:
        fields = self._fetch_message_metadata_fields(message_id, include_labels=True)
        labels = set(fields.get("label_ids", ()))
        amazon_id = self._resolve_label_id("Amazon")
        if amazon_id in labels:
            labels.remove(amazon_id)
            labels.add("Amazon")
        return frozenset(labels)

    def _fetch_message_metadata_fields(self, message_id: str, *, include_labels: bool = False) -> dict[str, Any]:
        params = [("format", "metadata")] + [("metadataHeaders", name) for name in INBOX_METADATA_HEADERS]
        url = (
            f"{_GMAIL_API}/users/{quote(self._user_id, safe='')}/messages/"
            f"{quote(message_id, safe='')}?{urlencode(params)}"
        )
        payload = self._http.request_json(
            self._context,
            "GET",
            url,
            headers=self._headers(),
            timeout_seconds=10.0,
            retry=_READ_RETRY,
        )
        if not isinstance(payload, dict):
            raise MailboxTransportError("Gmail message metadata response was not an object")
        headers = _gmail_headers(payload)
        internal_date = payload.get("internalDate")
        try:
            received_at = datetime.fromtimestamp(int(str(internal_date)) / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError) as exc:
            raise MailboxTransportError("Gmail message timestamp invalid") from exc
        fields = {
            "message_id": str(payload.get("id") or message_id),
            "received_at": received_at,
            "sender": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "headers": headers,
        }
        if include_labels:
            fields["label_ids"] = tuple(str(label) for label in (payload.get("labelIds") or []))
        return fields

    def _fetch_routed_message(self, message_id: str) -> RoutedNewsletterMessage:
        fields = self._fetch_message_fields(message_id)
        fields["raw_mime"] = self._fetch_raw_message(message_id)
        return RoutedNewsletterMessage(mailbox=self.provider, **fields)

    def _fetch_raw_message(self, message_id: str) -> str:
        url = f"{_GMAIL_API}/users/{quote(self._user_id, safe='')}/messages/{quote(message_id, safe='')}?format=raw"
        payload = self._http.request_json(
            self._context,
            "GET",
            url,
            headers=self._headers(),
            timeout_seconds=10.0,
            retry=_READ_RETRY,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("raw"), str):
            raise MailboxTransportError("Gmail raw Newsletter message response was invalid")
        raw_mime = _decode_base64url(payload["raw"])
        if not raw_mime:
            raise MailboxTransportError("Gmail raw Newsletter message was empty")
        return raw_mime

    def _fetch_message_fields(self, message_id: str) -> dict[str, Any]:
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
        return {
            "message_id": str(payload.get("id") or message_id),
            "received_at": received_at,
            "sender": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            **_gmail_body_parts(payload.get("payload") or {}),
            "headers": headers,
        }

    def _resolve_label_id(self, name: str, *, create_if_missing: bool = False) -> str:
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
            if create_if_missing:
                created = self._http.request_json(
                    self._context,
                    "POST",
                    url,
                    headers=self._headers(),
                    json_body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                    timeout_seconds=10.0,
                    retry=_NO_RETRY,
                )
                if isinstance(created, dict) and created.get("id"):
                    label_id = str(created["id"])
                    self._label_ids[name] = label_id
                    return label_id
        raise MailboxTransportError("Gmail label not found")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}


class GmailInboxMetadataPort:
    """MailboxPort view onto GmailMailboxTransport that routes MailRouter's
    scan through the metadata-first Inbox boundary instead of full-body scan.
    Not a second router/pipeline: classification and routing still flow
    through the one MailRouter / DeterministicMailClassifier /
    route_to_newsletters path -- this only changes which acquisition method
    that path's scan_window call reaches.
    """

    def __init__(self, transport: GmailMailboxTransport) -> None:
        self._transport = transport

    @property
    def provider(self) -> str:
        return self._transport.provider

    def scan_window(self, start: datetime, end: datetime) -> tuple[Any, ...]:
        return self._transport.scan_inbox_metadata_window(start, end)

    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None:
        self._transport.route_to_newsletters(message_id, boundary_name)


def _processed_label_name(boundary_name: str) -> str:
    return f"{boundary_name}/{PROCESSED_LABEL_SUFFIX}"


def _validate_window(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("mail window timestamps must be timezone-aware")
    if end <= start:
        raise ValueError("mail window end must be after start")


def _format_message_failures(failures: Mapping[str, Exception]) -> str:
    details = []
    for message_id, exc in failures.items():
        detail = str(exc).replace("\n", " ").strip()
        if len(detail) > 160:
            detail = f"{detail[:157]}..."
        details.append(f"{message_id}:{exc.__class__.__name__}:{detail}")
    return "[" + ", ".join(details) + "]"


def _gmail_headers(message: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    payload = message.get("payload") or {}
    if isinstance(payload, dict):
        for item in payload.get("headers") or []:
            if isinstance(item, dict) and item.get("name"):
                result[str(item["name"])] = str(item.get("value") or "")
    return result


def _gmail_body_parts(part: Mapping[str, Any]) -> dict[str, str]:
    plain, html_text = _gmail_body_texts(part)
    return {"body_text": plain or html_text, "html_text": html_text}


def _gmail_body_texts(part: Mapping[str, Any]) -> tuple[str, str]:
    mime_type = str(part.get("mimeType") or "")
    body = part.get("body") or {}
    if mime_type == "text/plain" and isinstance(body, dict) and body.get("data"):
        return _decode_base64url(str(body["data"])), ""
    if mime_type == "text/html" and isinstance(body, dict) and body.get("data"):
        return "", _decode_base64url(str(body["data"]))
    pieces: list[str] = []
    html_pieces: list[str] = []
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            text, html_text = _gmail_body_texts(child)
            if text:
                pieces.append(text)
            if html_text:
                html_pieces.append(html_text)
    if isinstance(body, dict) and body.get("data"):
        fallback = _decode_base64url(str(body["data"]))
        return fallback, ""
    return "\n".join(pieces), "\n".join(html_pieces)


def _decode_base64url(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8", errors="replace")
    except (ValueError, UnicodeEncodeError):
        return ""
