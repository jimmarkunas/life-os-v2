"""Source-oriented Newsletter models; never canonical Job identity/policy."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping

class ParseState(str, Enum):
    PASS = "PASS"
    DEGRADED = "DEGRADED"

ENRICHMENT_ONLY_ISSUES = frozenset({
    "source-apply-url-missing",
})


def fatal_issue_codes(codes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(code for code in codes if code not in ENRICHMENT_ONLY_ISSUES)

@dataclass(frozen=True, slots=True)
class RoutedNewsletterMessage:
    mailbox: str
    message_id: str
    received_at: datetime
    sender: str
    subject: str
    body_text: str
    headers: Mapping[str, str] = field(default_factory=dict)
    html_text: str = ""
    raw_mime: str = ""

@dataclass(frozen=True, slots=True)
class SourceVacancyObservation:
    evidence_ref: str
    source_provider: str
    source_mailbox: str
    source_message_id: str
    source_subject: str
    company: str | None
    role: str | None
    location_text: str | None
    compensation_text: str | None
    source_apply_url: str | None
    provider_job_id: str | None = None
    provider_score: int | None = None
    issues: tuple[str, ...] = ()
    source_received_at: datetime | None = None

@dataclass(frozen=True, slots=True)
class ParseIssue:
    code: str
    evidence_ref: str

@dataclass(frozen=True, slots=True)
class MessageParseResult:
    message_ref: str
    source_provider: str | None
    state: ParseState
    observations: tuple[SourceVacancyObservation, ...]
    issues: tuple[ParseIssue, ...]
