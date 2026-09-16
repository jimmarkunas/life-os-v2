"""Whole-mailbox scan and conservative routing into the Newsletter boundary."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from time import perf_counter
from typing import Iterable, Protocol, Sequence
from .classifier import DeterministicMailClassifier
from .models import Classification, MailClass, MailMessage, MailRef

class MailboxPort(Protocol):
    @property
    def provider(self) -> str: ...
    def scan_window(self, start: datetime, end: datetime) -> Iterable[MailMessage]: ...
    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None: ...

class MailExecutionState(str, Enum):
    PASS = "PASS"
    DEGRADED = "DEGRADED"

@dataclass(frozen=True, slots=True)
class ProviderScan:
    provider: str
    complete: bool
    message_count: int
    detail: str | None = None

@dataclass(frozen=True, slots=True)
class RouteRecord:
    ref: MailRef
    classification: Classification
    routed: bool

@dataclass(frozen=True, slots=True)
class RoutingError:
    ref: MailRef
    operation: str
    detail: str

@dataclass(frozen=True, slots=True)
class RoutingTimings:
    scan_seconds: float
    classify_seconds: float
    route_seconds: float
    total_seconds: float

@dataclass(frozen=True, slots=True)
class MailRouteResult:
    state: MailExecutionState
    provider_scans: tuple[ProviderScan, ...]
    records: tuple[RouteRecord, ...]
    errors: tuple[RoutingError, ...]
    timings: RoutingTimings
    def count(self, mail_class: MailClass) -> int:
        return sum(1 for r in self.records if r.classification.mail_class is mail_class)
    @property
    def scanned_count(self) -> int:
        return len(self.records)
    @property
    def checkpoint_safe(self) -> bool:
        return self.state is MailExecutionState.PASS and all(scan.complete for scan in self.provider_scans)

class MailRouter:
    def __init__(self, classifier: DeterministicMailClassifier | None = None, *, newsletter_boundary: str = "J Newsletters", max_workers: int = 8) -> None:
        self._classifier = classifier or DeterministicMailClassifier()
        self._newsletter_boundary = newsletter_boundary
        self._max_workers = max(1, max_workers)

    def route_window(self, mailboxes: Sequence[MailboxPort], start: datetime, end: datetime) -> MailRouteResult:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("mail window timestamps must be timezone-aware")
        if end <= start:
            raise ValueError("mail window end must be after start")
        total_started = perf_counter()
        if not mailboxes:
            error = RoutingError(MailRef("<config>", "<scan>"), "scan", "no-mailbox-providers")
            return MailRouteResult(
                state=MailExecutionState.DEGRADED,
                provider_scans=(),
                records=(),
                errors=(error,),
                timings=RoutingTimings(0.0, 0.0, 0.0, perf_counter() - total_started),
            )
        messages, provider_scans, scan_errors, scan_seconds = self._scan_all(mailboxes, start, end)
        classify_started = perf_counter()
        classified = tuple((m, self._classifier.classify(m)) for m in messages)
        classify_seconds = perf_counter() - classify_started
        route_started = perf_counter()
        routed_refs, route_errors = self._route_confirmed(mailboxes, classified)
        route_seconds = perf_counter() - route_started
        errors = tuple((*scan_errors, *route_errors))
        records = tuple(RouteRecord(m.ref, c, m.ref in routed_refs) for m, c in classified)
        state = MailExecutionState.PASS if not errors and all(s.complete for s in provider_scans) else MailExecutionState.DEGRADED
        return MailRouteResult(
            state=state,
            provider_scans=provider_scans,
            records=records,
            errors=errors,
            timings=RoutingTimings(scan_seconds, classify_seconds, route_seconds, perf_counter() - total_started),
        )

    def _scan_all(self, mailboxes: Sequence[MailboxPort], start: datetime, end: datetime):
        started = perf_counter()
        messages: list[MailMessage] = []
        errors: list[RoutingError] = []
        scans: list[ProviderScan] = []
        if not mailboxes:
            return (), (), (), perf_counter() - started
        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(mailboxes))) as pool:
            futures = {pool.submit(lambda box=box: tuple(box.scan_window(start, end))): box for box in mailboxes}
            for future in as_completed(futures):
                box = futures[future]
                try:
                    batch = future.result()
                except Exception as exc:
                    detail = type(exc).__name__
                    scans.append(ProviderScan(box.provider, False, 0, detail))
                    errors.append(RoutingError(MailRef(box.provider, "<scan>"), "scan", detail))
                    continue
                accepted = 0
                complete = True
                for message in batch:
                    if message.provider != box.provider:
                        complete = False
                        errors.append(RoutingError(message.ref, "scan", "provider-mismatch"))
                        continue
                    accepted += 1
                    messages.append(message)
                scans.append(ProviderScan(box.provider, complete, accepted, None if complete else "provider-mismatch"))
        messages.sort(key=lambda x: (x.received_at, x.provider, x.message_id))
        scans.sort(key=lambda x: x.provider)
        return tuple(messages), tuple(scans), tuple(errors), perf_counter() - started

    def _route_confirmed(self, mailboxes: Sequence[MailboxPort], classified):
        by_provider = {box.provider: box for box in mailboxes}
        candidates = [m for m, c in classified if c.mail_class is MailClass.AUTOMATED_JOB_SOURCE]
        if not candidates:
            return frozenset(), ()
        routed: set[MailRef] = set()
        errors: list[RoutingError] = []
        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(candidates))) as pool:
            futures = {}
            for message in candidates:
                box = by_provider.get(message.provider)
                if box is None:
                    errors.append(RoutingError(message.ref, "route", "provider-port-missing"))
                    continue
                futures[pool.submit(box.route_to_newsletters, message.message_id, self._newsletter_boundary)] = message
            for future in as_completed(futures):
                message = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    errors.append(RoutingError(message.ref, "route", type(exc).__name__))
                else:
                    routed.add(message.ref)
        return frozenset(routed), tuple(errors)
