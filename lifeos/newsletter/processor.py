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

MAX_SOURCE_FETCH_WORKERS = 2
MAX_PARSE_WORKERS = 8

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

def _is_known_non_vacancy_notification(message: RoutedNewsletterMessage) -> bool:
    """Recognize proven LinkedIn Jobs notifications that intentionally contain no vacancy cards.

    Stable sender + subject envelopes are sufficient; Gmail's normalized body
    representation is not guaranteed to preserve explanatory copy. Unknown
    no-card messages still fail closed.
    """
    sender = message.sender.casefold()
    # LinkedIn subjects observed in production use a typographic apostrophe
    # (U+2018/U+2019), not ASCII "'"; normalize before matching so the
    # pattern below actually matches real mail, not just synthetic fixtures.
    subject = message.subject.casefold().strip().replace("‘", "'").replace("’", "'")
    return (
        "linkedin.com" in sender
        and (
            (
                subject.startswith("message people you know at ")
                and subject.endswith(" to learn more")
            )
            or subject.endswith(", looking for a new job?")
            or subject.startswith("we've turned off your job alert for ")
        )
    )

def _parse_message_or_known_empty(message: RoutedNewsletterMessage) -> MessageParseResult:
    if _is_known_non_vacancy_notification(message):
        return MessageParseResult(
            f"{message.mailbox}:{message.message_id}",
            "LinkedIn Jobs",
            ParseState.PASS,
            (),
            (),
        )
    return parse_message(message)

class NewsletterProcessor:
    def __init__(self, *, boundary_name: str = "J Newsletters", max_workers: int = 8) -> None:
        self._boundary_name = boundary_name
        requested = max(1, int(max_workers))
        self._fetch_workers = min(requested, MAX_SOURCE_FETCH_WORKERS)
        self._parse_workers = min(requested, MAX_PARSE_WORKERS)

    def process_window(self, sources: Sequence[NewsletterSourcePort], start: datetime, end: datetime) -> NewsletterProcessResult:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("newsletter window timestamps must be timezone-aware")
        if end <= start:
            raise ValueError("newsletter window end must be after start")
        total_started = perf_counter()
        fetch_started = perf_counter()
        messages: list[RoutedNewsletterMessage] = []
        errors: list[NewsletterError] = []
        if not sources:
            errors.append(NewsletterError("<config>", "fetch", "no-newsletter-sources"))
        if sources:
            with ThreadPoolExecutor(max_workers=min(self._fetch_workers, len(sources))) as pool:
                futures = {
                    pool.submit(lambda source=s: tuple(source.fetch_unprocessed(start, end, self._boundary_name))): s
                    for s in sources
                }
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        batch = future.result()
                    except Exception as exc:
                        errors.append(NewsletterError(source.mailbox, "fetch", _safe_error_detail(exc)))
                        continue
                    for message in batch:
                        if message.mailbox != source.mailbox:
                            errors.append(NewsletterError(source.mailbox, "fetch", "mailbox-mismatch"))
                            continue
                        messages.append(message)
        fetch_seconds = perf_counter() - fetch_started
        messages.sort(key=lambda m: (m.received_at, m.mailbox, m.message_id))
        parse_started = perf_counter()
        parsed: list[MessageParseResult] = []
        if messages:
            with ThreadPoolExecutor(max_workers=min(self._parse_workers, len(messages))) as pool:
                for chunk_start in range(0, len(messages), self._parse_workers):
                    chunk = messages[chunk_start : chunk_start + self._parse_workers]
                    futures = [pool.submit(_parse_message_or_known_empty, message) for message in chunk]
                    for future in futures:
                        parsed.append(future.result())
        parse_seconds = perf_counter() - parse_started
        parsed.sort(key=lambda r: r.message_ref)
        for result in parsed:
            if result.state is not ParseState.DEGRADED:
                continue
            mailbox = result.message_ref.split(":", 1)[0] if ":" in result.message_ref else "<unknown>"
            issue_codes = sorted({issue.code for issue in result.issues})
            detail = f"{result.message_ref}:issues={','.join(issue_codes) if issue_codes else 'message-parse-degraded'}"
            errors.append(NewsletterError(mailbox, "parse", detail))
        state = (
            NewsletterExecutionState.PASS
            if not errors and all(r.state is ParseState.PASS for r in parsed)
            else NewsletterExecutionState.DEGRADED
        )
        return NewsletterProcessResult(
            state,
            tuple(parsed),
            tuple(errors),
            NewsletterTimings(fetch_seconds, parse_seconds, perf_counter() - total_started),
        )


def _safe_error_detail(exc: Exception) -> str:
    detail = str(exc).replace("\n", " ").strip()
    if len(detail) > 240:
        detail = f"{detail[:237]}..."
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__
