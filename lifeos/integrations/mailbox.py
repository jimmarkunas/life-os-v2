"""Provider-neutral mailbox transport contracts consumed by the Mail domain."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping, Protocol, TypeVar

T = TypeVar("T")


class MailMessageFactory(Protocol[T]):
    def __call__(
        self,
        *,
        provider: str,
        message_id: str,
        received_at: datetime,
        sender: str,
        subject: str,
        body_text: str,
        headers: Mapping[str, str],
    ) -> T: ...


class MailboxTransport(Protocol[T]):
    @property
    def provider(self) -> str: ...

    def scan_window(self, start: datetime, end: datetime) -> Iterable[T]: ...

    def route_to_newsletters(self, message_id: str, boundary_name: str) -> None: ...


class MailboxTransportError(RuntimeError):
    """Safe provider error that does not include production message data."""
