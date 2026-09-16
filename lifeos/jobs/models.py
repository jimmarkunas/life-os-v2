"""Shared Career/Jobs domain model.

Wave 1 scope: Company, Job, and the normalized candidate a discovery source
produces before it reaches identity/dedupe/qualification. Opportunity,
Application, Interview, Offer, and Employment are declared now as the shape
future waves extend, but carry no persistence/lifecycle logic yet.

This module owns *shape* only. Identity, qualification, and lifecycle policy
live in their own modules (identity.py, qualification.py, lifecycle.py) so a
future adapter cannot fork business rules by editing a dataclass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class WorkMode(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class FreshnessStatus(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNRESOLVED = "unresolved"
    CLOSED = "closed"


class AdmissionStatus(str, Enum):
    ADMITTED = "admitted"
    PASSED_REVIEW = "passed_review"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class Company:
    """A hiring organization. Identity is the normalized display name until a
    stronger canonical company identity (e.g. a resolved domain) is proven
    necessary by a real second consumer."""

    name: str
    domain: str | None = None


@dataclass(frozen=True)
class Job:
    """A single vacancy as understood by exactly one discovery source
    observation. Not yet deduplicated across sources -- see identity.py /
    dedupe.py for the canonical-Job convergence step."""

    company: Company
    role: str
    location: str | None
    work_mode: WorkMode
    compensation_text: str | None
    compensation_minimum: int | None
    posting_date: date | None
    apply_url: str | None
    source_lane: str
    provider_job_id: str | None = None


@dataclass(frozen=True)
class NormalizedCandidate:
    """One discovery source's normalized vacancy observation, ready for
    identity resolution. This is the Newsletter contract's input shape:
    Mail/Newsletter extracts facts into this shape; Career owns everything
    downstream of it."""

    job: Job
    fit: int | None
    market: str
    freshness_status: FreshnessStatus
    evidence_ref: str
    """Opaque reference to the extraction evidence (e.g. message id). Career
    never inspects this; it exists for traceability in the terminal
    disposition (see newsletter_contract.py)."""


@dataclass(frozen=True)
class Opportunity:
    """The canonical, deduplicated Job after cross-source convergence. One
    Opportunity may be backed by multiple source observations (see
    dedupe.py's ReconciledOpportunity, which this will become in a later
    wave once persistence lands)."""

    stable_job_key: str
    job: Job
    admission_status: AdmissionStatus
    source_lanes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Application:
    """Declared for future-wave shape stability. No lifecycle logic here yet."""

    opportunity_key: str
    applied_on: date | None
    applied: bool = False


@dataclass(frozen=True)
class Interview:
    """Declared for future-wave shape stability."""

    opportunity_key: str
    round_label: str
    scheduled_on: date | None


@dataclass(frozen=True)
class OfferDecision:
    """Declared for future-wave shape stability."""

    opportunity_key: str
    outcome: str | None
    decided_on: date | None


@dataclass(frozen=True)
class Employment:
    """Declared for future-wave shape stability."""

    opportunity_key: str
    started_on: date | None
