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
    """Domain-facing port implemented by provider integrations owned by Agent 1."""

    @property
    def provider(self) -> str: ...

    def scan_window(self, start: datetime, end: datetime) -> Iterable[MailMessage]: ...

    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None: ...


class MailExecutionState(str, Enum):
    PASS = "PASS"
    DEGRADED = "DEGRADED"


# Worker-pool ceilings: configuration above these is silently clamped, never
# raised, so a later misconfiguration cannot explode concurrency.
MAX_PROVIDER_SCAN_WORKERS = 2  # provider-level mailbox scanning
MAX_MESSAGE_WORKERS = 8  # ordinary per-message processing


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
        return sum(1 for record in self.records if record.classification.mail_class is mail_class)

    @property
    def scanned_count(self) -> int:
        return len(self.records)

    @property
    def checkpoint_safe(self) -> bool:
        return self.state is MailExecutionState.PASS and all(
            scan.complete for scan in self.provider_scans
        )


class MailRouter:
    """One bounded execution: scan all configured mailbox ports, classify, then route."""

    def __init__(
        self,
        classifier: DeterministicMailClassifier | None = None,
        *,
        newsletter_boundary: str = "J Newsletters",
        max_workers: int = 8,
    ) -> None:
        self._classifier = classifier or DeterministicMailClassifier()
        self._newsletter_boundary = newsletter_boundary
        requested = max(1, max_workers)
        self._scan_workers = min(requested, MAX_PROVIDER_SCAN_WORKERS)
        self._message_workers = min(requested, MAX_MESSAGE_WORKERS)

    def route_window(
        self,
        mailboxes: Sequence[MailboxPort],
        start: datetime,
        end: datetime,
    ) -> MailRouteResult:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("mail window timestamps must be timezone-aware")
        if end <= start:
            raise ValueError("mail window end must be after start")

        total_started = perf_counter()
        if not mailboxes:
            error = RoutingError(
                ref=MailRef(provider="<config>", message_id="<scan>"),
                operation="scan",
                detail="no-mailbox-providers",
            )
            return MailRouteResult(
                state=MailExecutionState.DEGRADED,
                provider_scans=(),
                records=(),
                errors=(error,),
                timings=RoutingTimings(0.0, 0.0, 0.0, perf_counter() - total_started),
            )

        messages, provider_scans, scan_errors, scan_seconds = self._scan_all(
            mailboxes, start, end
        )

        classify_started = perf_counter()
        classified = tuple((message, self._classifier.classify(message)) for message in messages)
        classify_seconds = perf_counter() - classify_started

        route_started = perf_counter()
        routed_refs, route_errors = self._route_confirmed(mailboxes, classified)
        route_seconds = perf_counter() - route_started

        records = tuple(
            RouteRecord(
                ref=message.ref,
                classification=classification,
                routed=message.ref in routed_refs,
            )
            for message, classification in classified
        )
        errors = tuple((*scan_errors, *route_errors))
        state = (
            MailExecutionState.PASS
            if not errors and all(scan.complete for scan in provider_scans)
            else MailExecutionState.DEGRADED
        )
        total_seconds = perf_counter() - total_started
        return MailRouteResult(
            state=state,
            provider_scans=provider_scans,
            records=records,
            errors=errors,
            timings=RoutingTimings(
                scan_seconds=scan_seconds,
                classify_seconds=classify_seconds,
                route_seconds=route_seconds,
                total_seconds=total_seconds,
            ),
        )

    def _scan_all(
        self,
        mailboxes: Sequence[MailboxPort],
        start: datetime,
        end: datetime,
    ) -> tuple[
        tuple[MailMessage, ...],
        tuple[ProviderScan, ...],
        tuple[RoutingError, ...],
        float,
    ]:
        started = perf_counter()
        messages: list[MailMessage] = []
        scans: list[ProviderScan] = []
        errors: list[RoutingError] = []

        with ThreadPoolExecutor(max_workers=min(self._scan_workers, len(mailboxes))) as pool:
            future_to_mailbox = {
                pool.submit(lambda box=mailbox: tuple(box.scan_window(start, end))): mailbox
                for mailbox in mailboxes
            }
            for future in as_completed(future_to_mailbox):
                mailbox = future_to_mailbox[future]
                try:
                    batch = future.result()
                except Exception as exc:
                    detail = type(exc).__name__
                    scans.append(
                        ProviderScan(
                            provider=mailbox.provider,
                            complete=False,
                            message_count=0,
                            detail=detail,
                        )
                    )
                    errors.append(
                        RoutingError(
                            ref=MailRef(provider=mailbox.provider, message_id="<scan>"),
                            operation="scan",
                            detail=detail,
                        )
                    )
                    continue

                accepted = 0
                complete = True
                for message in batch:
                    if message.provider != mailbox.provider:
                        complete = False
                        errors.append(
                            RoutingError(
                                ref=message.ref,
                                operation="scan",
                                detail="provider-mismatch",
                            )
                        )
                        continue
                    accepted += 1
                    messages.append(message)
                scans.append(
                    ProviderScan(
                        provider=mailbox.provider,
                        complete=complete,
                        message_count=accepted,
                        detail=None if complete else "provider-mismatch",
                    )
                )

        messages.sort(key=lambda item: (item.received_at, item.provider, item.message_id))
        scans.sort(key=lambda item: item.provider)
        return tuple(messages), tuple(scans), tuple(errors), perf_counter() - started

    def _route_confirmed(
        self,
        mailboxes: Sequence[MailboxPort],
        classified: tuple[tuple[MailMessage, Classification], ...],
    ) -> tuple[frozenset[MailRef], tuple[RoutingError, ...]]:
        mailbox_by_provider = {mailbox.provider: mailbox for mailbox in mailboxes}
        candidates = [
            message
            for message, classification in classified
            if classification.mail_class is MailClass.AUTOMATED_JOB_SOURCE
        ]
        if not candidates:
            return frozenset(), ()

        routable: list[MailMessage] = []
        routed: set[MailRef] = set()
        errors: list[RoutingError] = []
        for message in candidates:
            mailbox = mailbox_by_provider.get(message.provider)
            if mailbox is None:
                errors.append(
                    RoutingError(ref=message.ref, operation="route", detail="provider-port-missing")
                )
                continue
            routable.append(message)

        # Bounded in-flight futures: never queue more than one worker's
        # worth of pending routes per chunk, regardless of how many
        # confirmed messages this window produced.
        with ThreadPoolExecutor(max_workers=min(self._message_workers, len(routable) or 1)) as pool:
            for chunk_start in range(0, len(routable), self._message_workers):
                chunk = routable[chunk_start : chunk_start + self._message_workers]
                future_to_message = {
                    pool.submit(
                        mailbox_by_provider[message.provider].route_to_newsletters,
                        message.message_id,
                        self._newsletter_boundary,
                    ): message
                    for message in chunk
                }
                for future in as_completed(future_to_message):
                    message = future_to_message[future]
                    try:
                        future.result()
                    except Exception as exc:
                        errors.append(
                            RoutingError(ref=message.ref, operation="route", detail=type(exc).__name__)
                        )
                    else:
                        routed.add(message.ref)

        return frozenset(routed), tuple(errors)
