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


class FitAuthority(str, Enum):
    AUTHORITATIVE = "authoritative"
    NON_AUTHORITATIVE = "non_authoritative"


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
    """Source-native identity (e.g. a LinkedIn/Lensa posting ID). Evidence
    and provenance only -- never primary cross-source identity, since two
    providers assign different IDs to the identical vacancy. See
    identity.provider_alias()."""
    canonical_identity: str | None = None
    """An already-resolved canonical Job identity, supplied by a caller that
    knows it (e.g. a prior reconciliation, or an explicit human-confirmed
    merge). When present this is authoritative and skips URL/company-role-
    location derivation entirely."""
    description_text: str | None = None
    """Terminal employer/ATS description text, when resolved. Evidence
    input for Fit scoring; never provider-supplied marketing copy."""
    provider_score: int | None = None
    """Provider-supplied match score, if any. Evidence only -- see
    fit_scoring.py's module docstring -- never contributes to LIFE OS Fit."""
    source_provider: str | None = None
    """Source-provider display name observed for this Job. Provenance only."""


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
    unresolved_reason: str | None = None
    """Set by a Jobs-owned adapter (e.g. newsletter_adapter.py) when
    required evidence -- most commonly final employer/ATS resolution --
    could not be established. When set, ingest() routes this candidate
    straight to REVIEW_DEGRADED before attempting identity/qualification;
    it is never silently dropped or force-admitted."""
    fit_authority: FitAuthority = FitAuthority.NON_AUTHORITATIVE
    """Whether this candidate's LIFE OS Fit came from authoritative Jobs
    evidence. Provider percentages and weak source snippets are never
    authoritative."""
    source_types: tuple[str, ...] = field(default_factory=tuple)
    """Acquisition provenance labels observed for this candidate, such as
    provider name and mailbox alert type. These are not source-lane names."""


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
    aliases: tuple[str, ...] = field(default_factory=tuple)
    """Provider-native identity strings (see identity.provider_alias) seen
    across every observation that converged onto this canonical Job.
    Provenance only -- never used to re-derive stable_job_key."""
    fit: int | None = None
    """Authoritative LIFE OS Fit for this canonical Job (see
    fit_scoring.py). Never a provider-supplied score -- see
    Job.provider_score for that, kept strictly separate."""
    fit_authority: FitAuthority = FitAuthority.NON_AUTHORITATIVE
    source_providers: tuple[str, ...] = field(default_factory=tuple)
    source_types: tuple[str, ...] = field(default_factory=tuple)


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
