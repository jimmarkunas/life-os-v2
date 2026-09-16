"""Stable Job identity.

MIGRATE/REFACTOR from v1 `jobs/identity.py`, generalized: v1's identity
priority chain (existing key > company+provider id > canonical apply URL >
normalized company|role|location) is proven and reused verbatim. Sanitized
by removing the hardcoded discovery-provider hostnames v1 baked in directly
(Lensa/jobright.ai email-tracking and intermediary-page detection) -- those
are real, useful heuristics, but they are provider-specific knowledge that
belongs in the Mail/Newsletter adapter's extraction step (where a source can
tell Career "this URL is a tracking wrapper, not a destination"), not hard
in Career's identity module. Career's job here is the deterministic
fallback chain and URL canonicalization; a source is expected to have
already resolved an intermediary/tracking URL to a real destination (or
omitted apply_url) before handing Career a NormalizedCandidate.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lifeos.jobs.models import Job

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


def stable_job_key(job: Job) -> str:
    """Deterministic identity, strongest-available evidence first:

    1. company + provider_job_id (source-native identity, when both present)
    2. canonical apply URL (tracking parameters stripped)
    3. normalized company|role|location

    Raises ValueError when none of the three is derivable -- callers must
    treat that as a REVIEW-DEGRADED candidate, never a silent drop.
    """
    company = (job.company.name or "").strip()
    if company and job.provider_job_id:
        return f"{company}::{job.provider_job_id.strip()}"

    url = canonical_url(job.apply_url)
    if url:
        return f"url:{url}"

    role = job.role or ""
    location = job.location or ""
    if not company or not role or not location:
        raise ValueError(
            "cannot derive stable Job identity without provider_job_id, a canonical "
            "apply URL, or company+role+location"
        )
    return f"{_norm(company)}|{_norm(role)}|{_norm(location)}"
