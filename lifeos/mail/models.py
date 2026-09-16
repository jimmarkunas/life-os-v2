"""Runtime-only mail domain models.

These types describe private provider data in memory. They are schemas only; callers
must not serialize mailbox census payloads, production message bodies, addresses,
or provider identifiers into Git.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping


class MailClass(str, Enum):
    AUTOMATED_JOB_SOURCE = "automated_job_source"
    HUMAN_HIRING = "human_hiring"
    UNRELATED = "unrelated"


@dataclass(frozen=True, slots=True)
class MailRef:
    provider: str
    message_id: str


@dataclass(frozen=True, slots=True)
class MailMessage:
    provider: str
    message_id: str
    received_at: datetime
    sender: str
    subject: str
    body_text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    html_text: str = ""
    raw_mime: str = ""

    @property
    def ref(self) -> MailRef:
        return MailRef(provider=self.provider, message_id=self.message_id)


@dataclass(frozen=True, slots=True)
class Classification:
    mail_class: MailClass
    reasons: tuple[str, ...]
