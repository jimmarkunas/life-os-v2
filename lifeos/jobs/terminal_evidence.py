"""Terminal vacancy evidence: canonical URL resolution and Posting Date
interpretation.

MIGRATE/REFACTOR from v1 `jobs/vacancy_normalization.py`. The proven
algorithmic core is reused: relative/absolute Posting Date parsing bounded
to explicit vacancy-date evidence (never a source receipt timestamp
standing in for an unproven Posting Date), bounded multi-hop intermediary
traversal with loop detection, downstream-link ranking (ATS host markers >
job-path hints), and schema.org/JobPosting extraction.

Per the platform ownership boundary, Jobs owns what counts as final
employer/ATS evidence and canonical URL resolution *policy*; Platform Core
owns HTTP mechanics (timeouts, retry, deadline budget). This module
therefore never calls urlopen/Request directly. It accepts an injected
`Fetcher` and performs at most one fetch per hop, bounded by
MAX_INTERMEDIARY_HOPS -- retry/backoff is the injected fetcher's
responsibility, not this module's, which is why v1's internal
MAX_RESOLVE_ATTEMPTS retry-with-sleep loop is retired rather than ported.

Retired from v1 (not ported): urllib transport and its retry/backoff loop
(Platform Core's domain once its HTTP interface lands); all
orchestration/runtime globals, checkpoint/recovery machinery, and any
Jim-specific data paths -- none of that logic existed in
vacancy_normalization.py's algorithmic core to begin with, but is called
out here per the harvest boundary.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from lifeos.jobs.identity import canonical_url

# --- Posting Date interpretation -------------------------------------------

_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_RELATIVE_AGO = re.compile(r"\b(\d+)\s+(minute|minutes|hour|hours|day|days)\s+ago\b", re.I)
_TODAY = re.compile(r"\btoday\b", re.I)
_YESTERDAY = re.compile(r"\byesterday\b", re.I)


def parse_posting_date(value: Any, *, reference_time: datetime | None = None) -> str | None:
    """Interpret vacancy-evidence text as an ISO Posting Date, or None.

    `reference_time` anchors relative evidence only (e.g. an email's
    received time). It is never itself returned as a Posting Date when the
    text carries no vacancy date/age evidence -- unsupported evidence
    resolves to None, never to the reference clock.
    """
    text = str(value or "").strip()
    if not text:
        return None

    m = _ISO_DATE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None

    m = _US_DATE.search(text)
    if m:
        year = int(m.group(3))
        year = year + 2000 if year < 100 else year
        try:
            return date(year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None

    relative = _RELATIVE_AGO.search(text)
    if relative:
        if reference_time is None:
            return None
        n = int(relative.group(1))
        unit = relative.group(2).lower()
        if unit.startswith("minute"):
            delta = timedelta(minutes=n)
        elif unit.startswith("hour"):
            delta = timedelta(hours=n)
        else:
            delta = timedelta(days=n)
        return (reference_time - delta).date().isoformat()

    if _TODAY.search(text):
        return reference_time.date().isoformat() if reference_time else None
    if _YESTERDAY.search(text):
        return (reference_time - timedelta(days=1)).date().isoformat() if reference_time else None
    return None


# --- Canonical URL resolution policy ----------------------------------------

MAX_INTERMEDIARY_HOPS = 4
MAX_RESOLVE_BYTES = 512_000

_RAW_HTTP_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
_JSONLD_SCRIPT = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)

# Well-known discovery-provider/ATS hosts. Public market-standard domain
# knowledge (job boards, applicant-tracking systems), not private policy --
# unlike lane compensation/visa/market thresholds, which never belong here.
DISCOVERY_INTERMEDIARY_HOSTS = {
    "jobright.ai", "lensa.com", "linkedin.com", "dice.com", "jobgether.com",
    "jobleads.com", "jooble.org", "learn4good.com", "bebee.com",
    "ziprecruiter.com", "monster.com", "clearancejobs.com",
}
ATS_HOST_MARKERS = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "myworkday.com",
    "jobvite.com", "ashbyhq.com", "smartrecruiters.com", "icims.com",
    "workable.com", "careers-page.com", "amazon.jobs",
)
JOB_PATH_HINTS = ("/job/", "/jobs/", "/career", "/position", "/opening", "/requisition", "/apply")


def _host(value: str) -> str:
    return urlsplit(value).netloc.casefold().split(":", 1)[0].removeprefix("www.")


def is_source_message_url(value: str | None) -> bool:
    """True for mailbox/message UI URLs -- these are evidence, never a
    resolution target."""
    if not value:
        return False
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return False
    host = parts.netloc.casefold().split(":", 1)[0]
    path = parts.path.casefold()
    if host == "mail.google.com" and path.startswith("/mail"):
        return True
    if host in {"outlook.live.com", "outlook.office.com", "outlook.office365.com"}:
        return path.startswith("/mail") or path.startswith("/owa")
    return False


def is_provider_intermediary_source(url: str) -> bool:
    """True for known discovery-provider hosts. DISCOVERY_INTERMEDIARY_HOSTS
    is the single authority; a discovery-provider page is never the final
    canonical apply URL, only a resolved employer/ATS destination is."""
    try:
        host = _host(url)
    except ValueError:
        return False
    return host in DISCOVERY_INTERMEDIARY_HOSTS or any(host.endswith(f".{d}") for d in DISCOVERY_INTERMEDIARY_HOSTS)


def _downstream_score(value: str) -> tuple[int, str | None]:
    candidate = canonical_url(value)
    if not candidate:
        return -1, None
    parts = urlsplit(candidate)
    host = parts.netloc.casefold().split(":", 1)[0].removeprefix("www.")
    if host in DISCOVERY_INTERMEDIARY_HOSTS or any(host.endswith(f".{d}") for d in DISCOVERY_INTERMEDIARY_HOSTS):
        return -1, None
    if any(marker in host for marker in ATS_HOST_MARKERS):
        return 3, candidate
    target = f"{parts.path.casefold()}?{parts.query.casefold()}"
    if any(hint in target for hint in JOB_PATH_HINTS) or any(
        key in target for key in ("jobid=", "job_id=", "jid=", "gh_jid=", "requisition")
    ):
        return 2, candidate
    return -1, None


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        for key, value in attrs:
            if key.casefold() == "href" and value:
                self.hrefs.append(value)


def _collect_hrefs(body: str, base_url: str) -> list[str]:
    decoded = html.unescape(body).replace("\\/", "/")
    parser = _HrefCollector()
    try:
        parser.feed(decoded)
    except Exception:
        pass
    raw = list(parser.hrefs) + _RAW_HTTP_URL.findall(decoded)
    absolute: list[str] = []
    for item in raw:
        try:
            absolute.append(urljoin(base_url, item.strip()))
        except Exception:
            continue
    return absolute


def _downstream_candidates(body: str, base_url: str) -> list[str]:
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for order, absolute in enumerate(_collect_hrefs(body, base_url)):
        score, candidate = _downstream_score(absolute)
        if score < 0 or not candidate or candidate in seen:
            continue
        seen.add(candidate)
        ranked.append((-score, order, candidate))
    ranked.sort()
    return [candidate for _, _, candidate in ranked]


def _find_next_intermediary_hop(body: str, base_url: str, visited: set[str]) -> str | None:
    seen: set[str] = set()
    for absolute in _collect_hrefs(body, base_url):
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"}:
            continue
        host = parts.netloc.casefold().split(":", 1)[0].removeprefix("www.")
        if host not in DISCOVERY_INTERMEDIARY_HOSTS and not any(
            host.endswith(f".{d}") for d in DISCOVERY_INTERMEDIARY_HOSTS
        ):
            continue
        if absolute in visited or absolute in seen:
            continue
        seen.add(absolute)
        return absolute
    return None


# --- Injected transport (Platform Core owns the real implementation) -------


@dataclass(frozen=True)
class FetchResponse:
    final_url: str
    body: str


class Fetcher(Protocol):
    """The narrow transport dependency this module needs. A real
    implementation (timeouts, retry, redaction, deadline budget) belongs to
    Platform Core; this Protocol lets Jobs's pure resolution/ranking logic
    be fully tested without any network dependency, and lets Tech Lead 1
    wire the concrete client once Core's HTTP interface lands."""

    def get(self, url: str) -> FetchResponse: ...


@dataclass(frozen=True)
class ResolutionResult:
    final_url: str | None
    chain: tuple[str, ...]


def resolve_final_vacancy_url(url: str, *, fetcher: Fetcher) -> ResolutionResult:
    """Resolve a vacancy link to its final, vacancy-specific employer/ATS
    URL. Direct employer/ATS URLs are canonicalized with no fetch. Known
    discovery-provider intermediary pages receive bounded multi-hop
    inspection (MAX_INTERMEDIARY_HOPS) with loop detection via a visited
    set -- at most one fetcher.get() call per hop; this module performs no
    retry itself. Fails closed to final_url=None; an unresolved
    intermediary is never returned merely to avoid a blank apply URL.
    """
    chain: list[str] = [url]
    if is_source_message_url(url):
        return ResolutionResult(None, tuple(chain))

    requires_downstream = is_provider_intermediary_source(url)
    direct = canonical_url(url)
    if direct and not requires_downstream:
        if direct != url:
            chain.append(direct)
        return ResolutionResult(direct, tuple(chain))

    if not requires_downstream:
        # Non-intermediary but uncanonicalizable directly (e.g. malformed):
        # one fetch to see where it actually goes, no retry.
        try:
            response = fetcher.get(url)
        except Exception:
            return ResolutionResult(None, tuple(chain))
        final_candidate = canonical_url(response.final_url)
        if final_candidate:
            if final_candidate not in chain:
                chain.append(final_candidate)
            return ResolutionResult(final_candidate, tuple(chain))
        return ResolutionResult(None, tuple(chain))

    visited: set[str] = set()
    current_url = url
    for _hop in range(MAX_INTERMEDIARY_HOPS):
        if current_url in visited:
            return ResolutionResult(None, tuple(chain))  # loop detected
        visited.add(current_url)
        try:
            response = fetcher.get(current_url)
        except Exception:
            return ResolutionResult(None, tuple(chain))

        redirect_candidate = canonical_url(response.final_url)
        if redirect_candidate:
            score, trusted = _downstream_score(redirect_candidate)
            if score >= 0 and trusted:
                if trusted not in chain:
                    chain.append(trusted)
                return ResolutionResult(trusted, tuple(chain))

        candidates = _downstream_candidates(response.body, response.final_url)
        if candidates:
            result = candidates[0]
            if result not in chain:
                chain.append(result)
            return ResolutionResult(result, tuple(chain))

        next_hop = _find_next_intermediary_hop(response.body, response.final_url, visited)
        if next_hop is None:
            return ResolutionResult(None, tuple(chain))
        if next_hop not in chain:
            chain.append(next_hop)
        current_url = next_hop

    return ResolutionResult(None, tuple(chain))  # hop limit exhausted


# --- schema.org/JobPosting extraction ---------------------------------------


def extract_job_posting_jsonld(html_text: str) -> dict[str, Any] | None:
    """Extract the first JobPosting from JSON-LD script tags, or None."""
    for m in _JSONLD_SCRIPT.finditer(html_text):
        try:
            data = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        candidates: list[Any] = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = [data] if data.get("@type") == "JobPosting" else (data.get("@graph", []) or [data])
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


@dataclass(frozen=True)
class TerminalVacancyEvidence:
    canonical_url: str
    description_text: str
    posting_date_raw: str
    evidence_source: str
    resolution_chain: tuple[str, ...]


def acquire_terminal_vacancy_evidence(source_url: str, *, fetcher: Fetcher) -> TerminalVacancyEvidence | None:
    """Resolve source_url to the canonical employer/ATS page and extract
    structured evidence. Fails closed to None when the page cannot be
    acquired, lacks complete schema.org/JobPosting evidence, or the
    resolved URL is still on a known discovery/intermediary host.

    At most one additional fetch beyond resolve_final_vacancy_url's own
    bounded hop budget -- this function never retries.
    """
    if not source_url or is_source_message_url(source_url):
        return None
    resolution = resolve_final_vacancy_url(source_url, fetcher=fetcher)
    if not resolution.final_url or is_provider_intermediary_source(resolution.final_url):
        return None

    try:
        response = fetcher.get(resolution.final_url)
    except Exception:
        return None

    posting = extract_job_posting_jsonld(response.body)
    if not posting:
        return None
    raw_description = str(posting.get("description") or "").strip()
    date_posted = str(posting.get("datePosted") or "").strip()
    if not raw_description or not date_posted:
        return None

    description = html.unescape(re.sub(r"<[^>]+>", " ", raw_description))
    description = re.sub(r"\s+", " ", description).strip()
    return TerminalVacancyEvidence(
        canonical_url=resolution.final_url,
        description_text=description,
        posting_date_raw=date_posted,
        evidence_source="schema.org/JobPosting",
        resolution_chain=resolution.chain,
    )
