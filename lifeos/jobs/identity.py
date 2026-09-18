"""Stable Job identity.

MIGRATE/REFACTOR from v1 `jobs/identity.py`, corrected. v1's chain put
company+provider_job_id ahead of the canonical URL. That is wrong for
cross-source convergence: two discovery providers (e.g. LinkedIn and Lensa)
assign different IDs to the identical vacancy, so keying on provider ID
first can produce two canonical Jobs for one real vacancy -- exactly the
duplication bug this correction removes.

Corrected priority, strongest cross-source trust first:

1. an already-resolved canonical identity, when a caller supplies one
   (JobObservation.canonical_identity) -- authoritative, skips everything else.
2. the canonical employer/ATS vacancy URL (tracking parameters stripped).
   A resolved final URL is the strongest source-independent signal two
   different providers can agree on for the same vacancy.
3. normalized company|role|location -- deterministic fallback when no URL
   is available.

provider_job_id is never part of primary identity. It is preserved as
provenance/alias evidence only -- see provider_alias() below -- so a
duplicate-provider-ID case can still be traced without letting a bare ID
fork identity for what is otherwise the same canonical vacancy.

Sanitized by removing the hardcoded discovery-provider hostnames v1 baked
in directly (Lensa/jobright.ai email-tracking and intermediary-page
detection); that host-classification/resolution-policy logic now lives in
terminal_evidence.py, which Jobs owns per the platform ownership boundary
and which is expected to hand this module an already-resolved,
canonicalizable apply_url before stable_job_key() runs.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lifeos.jobs.models import JobObservation

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"source", "src", "ref", "referrer", "tracking", "trk", "gh_src"}


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def canonical_url(value: str | None) -> str | None:
    """Strip tracking query parameters and normalize scheme/host/path casing
    and trailing slash. Does not attempt provider-specific tracking-wrapper
    detection -- that is source-adapter responsibility (see module docstring)."""
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value.strip()
    if not parts.scheme or not parts.netloc:
        return value.strip()
    kept = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith(TRACKING_QUERY_PREFIXES)
        and key.casefold() not in TRACKING_QUERY_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, urlencode(kept), ""))


def stable_job_key(job: JobObservation) -> str:
    """Deterministic identity, strongest cross-source evidence first:

    1. JobObservation.canonical_identity, when a caller supplies one (authoritative)
    2. canonical apply URL (tracking parameters stripped)
    3. normalized company|role|location

    provider_job_id is deliberately NOT part of this chain -- see module
    docstring and provider_alias() -- so two providers observing the same
    vacancy under different IDs still converge on one key whenever a URL
    or company+role+location match.

    Raises ValueError when none of the three is derivable -- callers must
    treat that as a REVIEW-DEGRADED candidate, never a silent drop.
    """
    if job.canonical_identity and job.canonical_identity.strip():
        return job.canonical_identity.strip()

    url = canonical_url(job.apply_url)
    if url:
        return f"url:{url}"

    company = (job.company.name or "").strip()
    role = job.role or ""
    location = job.location or ""
    if not company or not role or not location:
        raise ValueError(
            "cannot derive stable Job identity without canonical_identity, a canonical "
            "apply URL, or company+role+location"
        )
    return f"{_norm(company)}|{_norm(role)}|{_norm(location)}"


@dataclass(frozen=True)
class IdentityEvidence:
    """Every primary identity signal derivable for one incoming observation."""

    stable_job_keys: tuple[str, ...]
    canonical_apply_urls: tuple[str, ...]


class IdentityCollision(ValueError):
    """Raised when one observation points at multiple existing canonical Jobs."""


def derive_identity_evidence(job: JobObservation) -> IdentityEvidence:
    """Return all valid identity evidence for bounded existing-record lookup.

    The final fallback key remains exactly the same company|role|location
    expression used by stable_job_key(); provider IDs are intentionally
    excluded because they are provenance only.
    """
    keys: list[str] = []
    urls: list[str] = []

    if job.canonical_identity and job.canonical_identity.strip():
        keys.append(job.canonical_identity.strip())

    url = canonical_url(job.apply_url)
    if url:
        keys.append(f"url:{url}")
        urls.append(url)

    company = (job.company.name or "").strip()
    role = job.role or ""
    location = job.location or ""
    if company and role and location:
        keys.append(f"{_norm(company)}|{_norm(role)}|{_norm(location)}")

    return IdentityEvidence(
        stable_job_keys=tuple(dict.fromkeys(keys)),
        canonical_apply_urls=tuple(dict.fromkeys(urls)),
    )


def resolve_existing_identity(
    evidence: IdentityEvidence,
    *,
    records_by_stable_key: dict[str, object],
    records_by_apply_url: dict[str, object],
) -> str | None:
    """Resolve which existing canonical Stable Job Key owns this evidence.

    Returns None when no existing record matches. Raises IdentityCollision
    when different evidence paths point at different existing canonical Jobs.
    Records are duck-typed to JobLedgerRecord to avoid making identity.py
    depend on lifecycle/repository modules.
    """
    matches: set[str] = set()
    for key in evidence.stable_job_keys:
        record = records_by_stable_key.get(key)
        if record is not None:
            matches.add(record.job.stable_job_key)
    for url in evidence.canonical_apply_urls:
        record = records_by_apply_url.get(url)
        if record is not None:
            matches.add(record.job.stable_job_key)

    if len(matches) > 1:
        raise IdentityCollision(f"identity evidence matched multiple existing Jobs: {', '.join(sorted(matches))}")
    return next(iter(matches), None)


def provider_alias(job: JobObservation) -> str | None:
    """Return a provenance/alias string for this observation's source-native
    identity (company + provider_job_id), or None when unavailable.

    Never used as primary identity -- see stable_job_key() -- but preserved
    so cross-provider provenance (e.g. "this canonical Job was also seen as
    LinkedIn posting 12345") is not silently lost during convergence."""
    company = (job.company.name or "").strip()
    if company and job.provider_job_id and job.provider_job_id.strip():
        return f"{company}::{job.provider_job_id.strip()}"
    return None
