"""Deadline-aware bounded HTTP mechanics with safe normalized failures."""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from enum import Enum
from time import sleep
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError as UrlHTTPError, URLError
from urllib.request import Request, urlopen

from .runtime import DeadlineExceeded, RunContext

MAX_HTTP_ATTEMPTS = 3


class HttpErrorKind(str, Enum):
    DEADLINE = "deadline"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    HTTP_STATUS = "http_status"
    INVALID_RESPONSE = "invalid_response"


class HttpError(RuntimeError):
    """Normalized failure that deliberately omits URL, headers, tokens, and bodies."""

    def __init__(
        self,
        kind: HttpErrorKind,
        *,
        status_code: int | None = None,
        attempts: int = 1,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.attempts = attempts
        self.retry_after_seconds = retry_after_seconds
        parts = [kind.value]
        if status_code is not None:
            parts.append(f"status={status_code}")
        parts.append(f"attempts={attempts}")
        super().__init__("http failure: " + " ".join(parts))


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: float = 0.25
    max_backoff_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_attempts > MAX_HTTP_ATTEMPTS:
            raise ValueError(f"max_attempts must be between 1 and {MAX_HTTP_ATTEMPTS}")
        if self.backoff_seconds < 0 or self.max_backoff_seconds < 0:
            raise ValueError("retry backoff must be non-negative")


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str = ""
    """The actual URL after following redirects. Empty when a backend
    cannot report it (e.g. an error path); callers needing redirect-aware
    resolution should treat empty as "same as requested"."""

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8")) if self.body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(HttpErrorKind.INVALID_RESPONSE) from exc


class HttpBackend(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse: ...


class UrllibHttpBackend:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        request = Request(url=url, data=body, headers=dict(headers), method=method.upper())
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status_code=int(response.status),
                    headers={str(k): str(v) for k, v in response.headers.items()},
                    body=response.read(),
                    final_url=str(response.geturl() or url),
                )
        except UrlHTTPError as exc:
            return HttpResponse(
                status_code=int(exc.code),
                headers={str(k): str(v) for k, v in exc.headers.items()} if exc.headers else {},
                body=exc.read(),
            )


class HttpClient:
    def __init__(self, backend: HttpBackend | None = None) -> None:
        self._backend = backend or UrllibHttpBackend()

    def request(
        self,
        context: RunContext,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        json_body: Any | None = None,
        timeout_seconds: float = 10.0,
        retry: RetryPolicy = RetryPolicy(),
        expected_statuses: Sequence[int] | range = range(200, 300),
    ) -> HttpResponse:
        if body is not None and json_body is not None:
            raise ValueError("provide body or json_body, not both")
        request_headers = dict(headers or {})
        request_body = body
        if json_body is not None:
            request_body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")

        last_error: HttpError | None = None
        for attempt in range(1, retry.max_attempts + 1):
            try:
                per_attempt_timeout = context.bounded_timeout(timeout_seconds)
            except DeadlineExceeded as exc:
                raise HttpError(HttpErrorKind.DEADLINE, attempts=attempt) from exc

            try:
                response = self._backend.request(
                    method,
                    url,
                    headers=request_headers,
                    body=request_body,
                    timeout_seconds=per_attempt_timeout,
                )
            except (TimeoutError, socket.timeout) as exc:
                last_error = HttpError(HttpErrorKind.TIMEOUT, attempts=attempt)
                if attempt >= retry.max_attempts:
                    raise last_error from exc
                self._backoff(context, retry, attempt)
                continue
            except URLError as exc:
                if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
                    last_error = HttpError(HttpErrorKind.TIMEOUT, attempts=attempt)
                else:
                    last_error = HttpError(HttpErrorKind.NETWORK, attempts=attempt)
                if attempt >= retry.max_attempts:
                    raise last_error from exc
                self._backoff(context, retry, attempt)
                continue
            except OSError as exc:
                last_error = HttpError(HttpErrorKind.NETWORK, attempts=attempt)
                if attempt >= retry.max_attempts:
                    raise last_error from exc
                self._backoff(context, retry, attempt)
                continue

            if response.status_code in expected_statuses:
                return response

            retry_after = _retry_after_seconds(response.headers)
            kind = HttpErrorKind.RATE_LIMIT if response.status_code == 429 else HttpErrorKind.HTTP_STATUS
            last_error = HttpError(
                kind,
                status_code=response.status_code,
                attempts=attempt,
                retry_after_seconds=retry_after,
            )
            retryable = response.status_code in {408, 429} or 500 <= response.status_code <= 599
            if not retryable or attempt >= retry.max_attempts:
                raise last_error
            self._backoff(context, retry, attempt, retry_after_seconds=retry_after)

        raise last_error or HttpError(HttpErrorKind.NETWORK)

    def request_json(self, context: RunContext, method: str, url: str, **kwargs: Any) -> Any:
        response = self.request(context, method, url, **kwargs)
        try:
            return response.json()
        except HttpError as exc:
            raise HttpError(HttpErrorKind.INVALID_RESPONSE, status_code=response.status_code) from exc

    @staticmethod
    def _backoff(
        context: RunContext,
        retry: RetryPolicy,
        attempt: int,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        requested = retry_after_seconds if retry_after_seconds is not None else retry.backoff_seconds * (2 ** (attempt - 1))
        requested = min(max(0.0, requested), retry.max_backoff_seconds)
        if requested <= 0:
            context.require_time()
            return
        remaining = context.require_time()
        if requested >= remaining:
            raise HttpError(HttpErrorKind.DEADLINE, attempts=attempt)
        sleep(requested)


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    value = None
    for key, candidate in headers.items():
        if key.casefold() == "retry-after":
            value = candidate
            break
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None
