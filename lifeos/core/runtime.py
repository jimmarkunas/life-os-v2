"""Bounded execution primitives shared by LIFE OS v2 domains."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from time import monotonic
from typing import Callable, Generic, Iterator, Mapping, TypeVar
from uuid import uuid4

T = TypeVar("T")

DEFAULT_RUNTIME_SECONDS = 45.0
MAX_RUNTIME_SECONDS = 300.0
DEFAULT_HTTP_CONCURRENCY = 8
MAX_HTTP_CONCURRENCY = 8


class DeadlineExceeded(TimeoutError):
    """Raised when a RunContext has no usable time budget remaining."""


class ExecutionStatus(str, Enum):
    PASS = "PASS"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ExecutionResult(Generic[T]):
    """Small terminal result shape; intentionally not a workflow/state machine."""

    status: ExecutionStatus
    value: T | None = None
    code: str = "ok"
    detail: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)

    @classmethod
    def passed(cls, value: T | None = None, *, code: str = "ok", **detail: object) -> "ExecutionResult[T]":
        return cls(status=ExecutionStatus.PASS, value=value, code=code, detail=_safe_detail(detail))

    @classmethod
    def degraded(cls, *, code: str, value: T | None = None, **detail: object) -> "ExecutionResult[T]":
        return cls(status=ExecutionStatus.DEGRADED, value=value, code=code, detail=_safe_detail(detail))

    @classmethod
    def failed(cls, *, code: str, **detail: object) -> "ExecutionResult[T]":
        return cls(status=ExecutionStatus.FAILED, value=None, code=code, detail=_safe_detail(detail))


def _safe_detail(detail: Mapping[str, object]) -> dict[str, str | int | float | bool | None]:
    allowed = (str, int, float, bool, type(None))
    return {key: value if isinstance(value, allowed) else type(value).__name__ for key, value in detail.items()}


@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: str
    started_at: datetime
    deadline: datetime
    _timeout_seconds: float = field(repr=False)
    _started_monotonic: float = field(repr=False)
    _clock: Callable[[], float] = field(repr=False, compare=False)
    _http_permits: threading.Semaphore = field(repr=False, compare=False)

    @classmethod
    def start(
        cls,
        *,
        timeout_seconds: float = DEFAULT_RUNTIME_SECONDS,
        run_id: str | None = None,
        now: datetime | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        http_concurrency: int = DEFAULT_HTTP_CONCURRENCY,
    ) -> "RunContext":
        timeout = float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        if timeout > MAX_RUNTIME_SECONDS:
            raise ValueError(f"timeout_seconds exceeds platform maximum of {int(MAX_RUNTIME_SECONDS)}")
        requested_http_concurrency = int(http_concurrency)
        if requested_http_concurrency < 1:
            raise ValueError("http_concurrency must be at least 1")
        effective_http_concurrency = min(requested_http_concurrency, MAX_HTTP_CONCURRENCY)
        started_at = now or datetime.now(timezone.utc)
        if started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        return cls(
            run_id=run_id or uuid4().hex,
            started_at=started_at,
            deadline=started_at + timedelta(seconds=timeout),
            _timeout_seconds=timeout,
            _started_monotonic=monotonic_clock(),
            _clock=monotonic_clock,
            _http_permits=threading.Semaphore(effective_http_concurrency),
        )

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self._started_monotonic)

    def remaining_seconds(self) -> float:
        return max(0.0, self._timeout_seconds - self.elapsed_seconds())

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0.0

    def require_time(self, minimum_seconds: float = 0.0) -> float:
        minimum = max(0.0, float(minimum_seconds))
        remaining = self.remaining_seconds()
        if remaining <= minimum:
            raise DeadlineExceeded("execution deadline exhausted")
        return remaining

    def bounded_timeout(self, requested_seconds: float) -> float:
        requested = float(requested_seconds)
        if requested <= 0:
            raise ValueError("requested timeout must be positive")
        remaining = self.require_time()
        return min(requested, remaining)

    @contextmanager
    def http_permit(self) -> Iterator[None]:
        """Bound active HTTP calls to the execution-wide ceiling without outliving the deadline."""
        wait_seconds = self.require_time()
        acquired = self._http_permits.acquire(timeout=wait_seconds)
        if not acquired:
            raise DeadlineExceeded("execution deadline exhausted waiting for an HTTP concurrency permit")
        try:
            yield
        finally:
            self._http_permits.release()
