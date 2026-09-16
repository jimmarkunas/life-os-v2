"""Fetch and parse routed J Newsletters mail; no source cleanup or Jobs policy."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from time import perf_counter
from typing import Iterable, Protocol, Sequence
from .models import MessageParseResult, ParseState, RoutedNewsletterMessage, SourceVacancyObservation
from .parsers import parse_message

class NewsletterSourcePort(Protocol):
    @property
    def mailbox(self) -> str: ...
    def fetch_unprocessed(self, start: datetime, end: datetime, boundary_name: str) -> Iterable[RoutedNewsletterMessage]: ...

class NewsletterExecutionState(str, Enum):
    PASS = "PASS"
    DEGRADED = "DEGRADED"

@dataclass(frozen=True, slots=True)
class NewsletterError:
    mailbox: str
    operation: str
    detail: str

@dataclass(frozen=True, slots=True)
class NewsletterTimings:
    fetch_seconds: float
    parse_seconds: float
    total_seconds: float

@dataclass(frozen=True, slots=True)
class NewsletterProcessResult:
    state: NewsletterExecutionState
    messages: tuple[MessageParseResult, ...]
    errors: tuple[NewsletterError, ...]
    timings: NewsletterTimings
    @property
    def observations(self) -> tuple[SourceVacancyObservation, ...]:
        return tuple(obs for result in self.messages for obs in result.observations)
    @property
    def cleanup_safe(self) -> bool:
        return False

class NewsletterProcessor:
    def __init__(self, *, boundary_name: str = "J Newsletters", max_workers: int = 8) -> None:
        self._boundary_name = boundary_name; self._max_workers = max(1, max_workers)
    def process_window(self, sources: Sequence[NewsletterSourcePort], start: datetime, end: datetime) -> NewsletterProcessResult:
        if start.tzinfo is None or end.tzinfo is None: raise ValueError("newsletter window timestamps must be timezone-aware")
        if end <= start: raise ValueError("newsletter window end must be after start")
        total_started = perf_counter(); fetch_started = perf_counter(); messages: list[RoutedNewsletterMessage] = []; errors: list[NewsletterError] = []
        if sources:
            with ThreadPoolExecutor(max_workers=min(self._max_workers, len(sources))) as pool:
                futures = {pool.submit(lambda source=s: tuple(source.fetch_unprocessed(start,end,self._boundary_name))): s for s in sources}
                for future in as_completed(futures):
                    source = futures[future]
                    try: batch = future.result()
                    except Exception as exc:
                        errors.append(NewsletterError(source.mailbox,"fetch",type(exc).__name__)); continue
                    for message in batch:
                        if message.mailbox != source.mailbox:
                            errors.append(NewsletterError(source.mailbox,"fetch","mailbox-mismatch")); continue
                        messages.append(message)
        fetch_seconds = perf_counter() - fetch_started
        messages.sort(key=lambda m:(m.received_at,m.mailbox,m.message_id))
        parse_started = perf_counter(); parsed: list[MessageParseResult] = []
        if messages:
            with ThreadPoolExecutor(max_workers=min(self._max_workers, len(messages))) as pool:
                futures = [pool.submit(parse_message,m) for m in messages]
                for future in futures: parsed.append(future.result())
        parse_seconds = perf_counter() - parse_started
        parsed.sort(key=lambda r:r.message_ref)
        state = NewsletterExecutionState.PASS if not errors and all(r.state is ParseState.PASS for r in parsed) else NewsletterExecutionState.DEGRADED
        return NewsletterProcessResult(state, tuple(parsed), tuple(errors), NewsletterTimings(fetch_seconds,parse_seconds,perf_counter()-total_started))
