"""Terminal vacancy evidence: canonical URL resolution and Posting Date evidence.

Jobs owns vacancy-resolution policy. Platform Core owns transport, retries, and
execution deadlines. Resolution stays bounded and fail-closed; an optional
fallback Fetcher lets the production orchestrator supply browser-rendered
recovery without adding a second retry/runtime system here.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from threading import Semaphore
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.jobs.identity import canonical_url

_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_RELATIVE_AGO = re.compile(r"\b(\d+)\s+(minute|minutes|hour|hours|day|days)\s+ago\b", re.I)
_TODAY = re.compile(r"\btoday\b", re.I)
_YESTERDAY = re.compile(r"\byesterday\b", re.I)
_RAW_HTTP_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
_JSONLD_SCRIPT = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
_META_DESCRIPTION = re.compile(r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)["\']', re.I | re.S)

MAX_INTERMEDIARY_HOPS = 4

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


def parse_posting_date(value: Any, *, reference_time: datetime | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _ISO_DATE.search(text)
    if match:
        try:
            return date(*(int(match.group(i)) for i in range(1, 4))).isoformat()
        except ValueError:
            return None
    match = _US_DATE.search(text)
    if match:
        year = int(match.group(3)); year = year + 2000 if year < 100 else year
        try:
            return date(year, int(match.group(1)), int(match.group(2))).isoformat()
        except ValueError:
            return None
    match = _RELATIVE_AGO.search(text)
    if match and reference_time:
        amount = int(match.group(1)); unit = match.group(2).lower()
        delta = timedelta(minutes=amount) if unit.startswith("minute") else timedelta(hours=amount) if unit.startswith("hour") else timedelta(days=amount)
        return (reference_time - delta).date().isoformat()
    if _TODAY.search(text):
        return reference_time.date().isoformat() if reference_time else None
    if _YESTERDAY.search(text):
        return (reference_time - timedelta(days=1)).date().isoformat() if reference_time else None
    return None


def _host(value: str) -> str:
    return urlsplit(value).netloc.casefold().split(":", 1)[0].removeprefix("www.")


def is_source_message_url(value: str | None) -> bool:
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
    return host in {"outlook.live.com", "outlook.office.com", "outlook.office365.com"} and (path.startswith("/mail") or path.startswith("/owa"))


def is_provider_intermediary_source(url: str) -> bool:
    try:
        host = _host(url)
    except ValueError:
        return False
    return host in DISCOVERY_INTERMEDIARY_HOSTS or any(host.endswith(f".{domain}") for domain in DISCOVERY_INTERMEDIARY_HOSTS)


def _is_linkedin_url(url: str) -> bool:
    try:
        host = _host(url)
    except ValueError:
        return False
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _has_linkedin_quick_apply_signal(body: str) -> bool:
    text = _html_to_text(body).casefold()
    if "easy apply is not available" in text or "quick apply is not available" in text:
        return False
    return "easy apply" in text or "quick apply" in text


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
    if any(hint in target for hint in JOB_PATH_HINTS) or any(key in target for key in ("jobid=", "job_id=", "jid=", "gh_jid=", "requisition")):
        return 2, candidate
    return -1, None


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.hrefs: list[str] = []
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            self.hrefs.extend(value for key, value in attrs if key.casefold() == "href" and value)


def _collect_hrefs(body: str, base_url: str) -> list[str]:
    decoded = html.unescape(body).replace("\\/", "/")
    parser = _HrefCollector()
    try:
        parser.feed(decoded)
    except Exception:
        pass
    out: list[str] = []
    for item in list(parser.hrefs) + _RAW_HTTP_URL.findall(decoded):
        try:
            out.append(urljoin(base_url, item.strip()))
        except Exception:
            continue
    return out


def _downstream_candidates(body: str, base_url: str) -> list[str]:
    ranked: list[tuple[int, int, str]] = []; seen: set[str] = set()
    for order, absolute in enumerate(_collect_hrefs(body, base_url)):
        score, candidate = _downstream_score(absolute)
        if score < 0 or not candidate or candidate in seen:
            continue
        seen.add(candidate); ranked.append((-score, order, candidate))
    ranked.sort()
    return [candidate for _, _, candidate in ranked]


def _find_next_intermediary_hop(body: str, base_url: str, visited: set[str]) -> str | None:
    for absolute in _collect_hrefs(body, base_url):
        try:
            if is_provider_intermediary_source(absolute) and absolute not in visited:
                return absolute
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class FetchResponse:
    final_url: str
    body: str


class _LinkedInDomProbe(HTMLParser):
    _TERMS = ("responsibilities", "qualifications", "requirements", "about the job", "what you'll do", "what you’ll do", "job description", "preferred qualifications")

    def __init__(self) -> None:
        super().__init__(); self._stack: list[dict[str, Any]] = []; self.candidates: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}; parent = self._stack[-1] if self._stack else None
        if parent: parent["child_count"] += 1
        self._stack.append({"tag": tag.casefold(), "attributes": attributes, "text": [], "child_count": 0, "parent": parent})

    def handle_data(self, data: str) -> None:
        if self._stack and data.strip():
            for node in self._stack: node["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        node = self._stack.pop(); text = re.sub(r"\s+", " ", " ".join(node["text"])).strip(); attributes = node["attributes"]
        classes = [item for item in attributes.get("class", "").split() if item]; identifiers = " ".join((attributes.get("id", ""), *classes, attributes.get("role", ""))).casefold(); terms = [term for term in self._TERMS if term in text.casefold()]
        if not text or not (terms or any(token in identifiers for token in ("description", "details", "show-more", "job"))): return
        parent = node["parent"] or {}; parent_attrs = parent.get("attributes", {})
        self.candidates.append({"tag": node["tag"], "id": attributes.get("id") or None, "classes": classes, "data_attributes": {key: value[:80] for key, value in attributes.items() if key.startswith("data-")}, "aria_attributes": {key: value[:80] for key, value in attributes.items() if key.startswith("aria-")}, "role": attributes.get("role") or None, "parent_tag": parent.get("tag"), "parent_id": parent_attrs.get("id") or None, "parent_classes": parent_attrs.get("class", "").split(), "child_count": node["child_count"], "text_length": len(text), "text_prefix": text[:80], "matching_terms": terms})


def _linkedin_dom_candidates(html_text: str) -> tuple[dict[str, Any], ...]:
    probe = _LinkedInDomProbe()
    try: probe.feed(html_text)
    except Exception: return ()
    return tuple(probe.candidates[:10])


class Fetcher(Protocol):
    def get(self, url: str) -> FetchResponse: ...


class MappingFetcher(Fetcher):
    """Optional explicitly supplied browser evidence, kept in memory only."""

    def __init__(self, evidence: dict | None) -> None:
        self._pages = {
            str(item.get("url")): FetchResponse(str(item.get("final_url") or item.get("url") or ""), str(item.get("html") or ""))
            for item in (evidence or {}).get("pages", [])
            if isinstance(item, dict) and item.get("url") and item.get("html")
        }

    @property
    def available(self) -> bool:
        return bool(self._pages)

    def get(self, url: str) -> FetchResponse:
        if url not in self._pages:
            raise RuntimeError("injected browser evidence unavailable")
        return self._pages[url]


class ChromeFetcher(Fetcher):
    """Bounded headless-browser fallback using the GitHub runner's Chrome."""

    def __init__(self, context: RunContext, *, max_concurrency: int = 2) -> None:
        self._context = context
        self._binary = (
            shutil.which("google-chrome")
            or shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chromium-browser")
        )
        self._permits = Semaphore(max(1, min(int(max_concurrency), 2)))

    @property
    def available(self) -> bool:
        return bool(self._binary)

    def get(self, url: str) -> FetchResponse:
        if not self._binary:
            raise RuntimeError("headless browser unavailable")
        wait = self._context.require_time(0.5)
        acquired = self._permits.acquire(timeout=wait)
        if not acquired:
            raise DeadlineExceeded("browser fallback concurrency wait exhausted deadline")
        try:
            with self._context.http_permit():
                remaining = self._context.require_time(0.5)
                timeout = max(0.5, min(12.0, remaining - 0.25))
                completed = subprocess.run(
                    [self._binary, "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--dump-dom", url],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("browser fallback timed out") from exc
        finally:
            self._permits.release()
        body = completed.stdout or ""
        if completed.returncode != 0 or len(body.strip()) < 20:
            raise RuntimeError("browser fallback did not return usable DOM")
        return FetchResponse(final_url=url, body=body)


class CompositeFetcher(Fetcher):
    def __init__(self, fetchers: list[Fetcher]) -> None:
        self._fetchers = tuple(fetchers)

    def get(self, url: str) -> FetchResponse:
        for fetcher in self._fetchers:
            try:
                return fetcher.get(url)
            except DeadlineExceeded:
                raise
            except Exception:
                continue
        raise RuntimeError("all browser fallback transports failed")


def browser_evidence() -> dict | None:
    raw = os.getenv("US_REMOTE_BROWSER_EVIDENCE_JSON")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        from lifeos.core.config import ConfigurationError

        raise ConfigurationError("US_REMOTE_BROWSER_EVIDENCE_JSON is invalid") from exc
    if not isinstance(value, dict):
        from lifeos.core.config import ConfigurationError

        raise ConfigurationError("US_REMOTE_BROWSER_EVIDENCE_JSON must be an object")
    return value


def fallback_fetcher(context: RunContext, evidence: dict | None) -> Fetcher | None:
    fetchers = [fetcher for fetcher in (MappingFetcher(evidence), ChromeFetcher(context)) if fetcher.available]
    return CompositeFetcher(fetchers) if fetchers else None


@dataclass(frozen=True)
class ResolutionResult:
    final_url: str | None
    chain: tuple[str, ...]
    verified_body: str | None = None
    provider_source_description: str | None = None
    linkedin_dom_candidates: tuple[dict[str, Any], ...] = ()


def extract_job_posting_jsonld(html_text: str) -> dict[str, Any] | None:
    for match in _JSONLD_SCRIPT.finditer(html_text):
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        candidates: list[Any]
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = [data] if data.get("@type") == "JobPosting" else (data.get("@graph", []) or [data])
        else:
            candidates = []
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _html_to_text(html_text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html_text))).strip()


def _extract_terminal_description(html_text: str) -> str | None:
    posting = extract_job_posting_jsonld(html_text)
    if posting:
        raw = str(posting.get("description") or "").strip()
        if raw:
            return _html_to_text(raw)
    match = _META_DESCRIPTION.search(html_text)
    if match:
        text = _html_to_text(match.group(1))
        if text:
            return text
    text = _html_to_text(html_text)
    return text if len(text) >= 20 else None


def _extract_linkedin_source_description(html_text: str) -> str | None:
    posting = extract_job_posting_jsonld(html_text)
    if posting:
        raw = str(posting.get("description") or "").strip()
        if raw:
            return _html_to_text(raw)
    match = _META_DESCRIPTION.search(html_text)
    if match:
        description = _html_to_text(match.group(1))
        if description:
            return description
    return None


def _extract_posting_date_raw(html_text: str) -> str | None:
    posting = extract_job_posting_jsonld(html_text)
    if posting:
        raw = str(posting.get("datePosted") or "").strip()
        if raw:
            return raw
    for pattern in (
        r"(?:datePosted|postedDate|posted_at|postedOn)[\"'\s:=]+([^\"'<>,]{4,40})",
        r"(Posted\s+(?:today|yesterday|\d+\s+(?:minutes?|hours?|days?)\s+ago))",
        r"(Posted\s+(?:20\d{2}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}))",
    ):
        match = re.search(pattern, html_text, re.I)
        if match:
            return html.unescape(match.group(1)).strip()
    return None


def _has_complete_vacancy_evidence(body: str) -> bool:
    return bool(_extract_terminal_description(body) and _extract_posting_date_raw(body))


def resolve_final_vacancy_url(url: str, *, fetcher: Fetcher) -> ResolutionResult:
    """Resolve to a vacancy-specific non-intermediary URL in <=4 hops.

    Employer-owned job pages are accepted when the fetched page itself proves
    complete vacancy evidence; they no longer need to be on a hard-coded ATS
    hostname or redirect elsewhere first. Intermediary URLs are fetched exactly
    as supplied so provider routing URLs are not altered before resolution.
    """
    chain: list[str] = [url]
    if is_source_message_url(url):
        return ResolutionResult(None, tuple(chain))
    direct = canonical_url(url)
    if direct and not is_provider_intermediary_source(direct):
        score, _ = _downstream_score(direct)
        if score >= 3:
            return ResolutionResult(direct, tuple(chain))
        try:
            response = fetcher.get(direct)
        except Exception:
            return ResolutionResult(None, tuple(chain))
        final_candidate = canonical_url(response.final_url)
        if final_candidate and not is_provider_intermediary_source(final_candidate):
            final_score, trusted = _downstream_score(final_candidate)
            if final_candidate != direct and trusted and final_score >= 0:
                if trusted not in chain: chain.append(trusted)
                return ResolutionResult(trusted, tuple(chain), response.body)
            if _has_complete_vacancy_evidence(response.body):
                if final_candidate not in chain: chain.append(final_candidate)
                return ResolutionResult(final_candidate, tuple(chain), response.body)
        candidates = _downstream_candidates(response.body, response.final_url)
        if candidates:
            if candidates[0] not in chain: chain.append(candidates[0])
            return ResolutionResult(candidates[0], tuple(chain))
        return ResolutionResult(None, tuple(chain))

    if not direct:
        return ResolutionResult(None, tuple(chain))

    visited: set[str] = set(); current_url = url
    for _ in range(MAX_INTERMEDIARY_HOPS):
        if current_url in visited:
            return ResolutionResult(None, tuple(chain))
        visited.add(current_url)
        try:
            response = fetcher.get(current_url)
        except Exception:
            return ResolutionResult(None, tuple(chain))
        final_candidate = canonical_url(response.final_url)
        if final_candidate and not is_provider_intermediary_source(final_candidate):
            score, trusted = _downstream_score(final_candidate)
            if trusted and score >= 0:
                if trusted not in chain: chain.append(trusted)
                return ResolutionResult(trusted, tuple(chain), response.body if _has_complete_vacancy_evidence(response.body) else None)
            if _has_complete_vacancy_evidence(response.body):
                if final_candidate not in chain: chain.append(final_candidate)
                return ResolutionResult(final_candidate, tuple(chain), response.body)
        candidates = _downstream_candidates(response.body, response.final_url)
        if candidates:
            if candidates[0] not in chain: chain.append(candidates[0])
            return ResolutionResult(candidates[0], tuple(chain))
        if (
            final_candidate
            and _is_linkedin_url(final_candidate)
            and _has_complete_vacancy_evidence(response.body)
            and _has_linkedin_quick_apply_signal(response.body)
        ):
            if final_candidate not in chain: chain.append(final_candidate)
            return ResolutionResult(
                final_candidate, tuple(chain), response.body,
                linkedin_dom_candidates=_linkedin_dom_candidates(response.body),
            )
        next_hop = _find_next_intermediary_hop(response.body, response.final_url, visited)
        if not next_hop:
            provider_description = (
                _extract_linkedin_source_description(response.body)
                if _is_linkedin_url(current_url)
                else None
            )
            return ResolutionResult(
                None, tuple(chain), provider_source_description=provider_description,
                linkedin_dom_candidates=(
                    _linkedin_dom_candidates(response.body)
                    if _is_linkedin_url(current_url) else ()
                ),
            )
        if next_hop not in chain: chain.append(next_hop)
        current_url = next_hop
    return ResolutionResult(None, tuple(chain))


@dataclass(frozen=True)
class TerminalVacancyEvidence:
    canonical_url: str | None
    description_text: str
    posting_date_raw: str
    evidence_source: str
    resolution_chain: tuple[str, ...]
    provider_source_description: str | None = None
    linkedin_dom_candidates: tuple[dict[str, Any], ...] = ()


def _acquire_once(source_url: str, fetcher: Fetcher) -> TerminalVacancyEvidence | None:
    resolution = resolve_final_vacancy_url(source_url, fetcher=fetcher)
    if not resolution.final_url or is_provider_intermediary_source(resolution.final_url):
        if resolution.provider_source_description or resolution.linkedin_dom_candidates:
            return TerminalVacancyEvidence(
                canonical_url=None,
                description_text="",
                posting_date_raw="",
                evidence_source="linkedin_source",
                resolution_chain=resolution.chain,
                provider_source_description=resolution.provider_source_description,
                linkedin_dom_candidates=resolution.linkedin_dom_candidates,
            )
        return None
    body = resolution.verified_body
    if body is None:
        try:
            body = fetcher.get(resolution.final_url).body
        except Exception:
            return None
    description = _extract_terminal_description(body)
    date_posted = _extract_posting_date_raw(body)
    if not description or not date_posted:
        return None
    return TerminalVacancyEvidence(
        canonical_url=resolution.final_url,
        description_text=description,
        posting_date_raw=date_posted,
        evidence_source="schema.org/JobPosting" if extract_job_posting_jsonld(body) else "vacancy_page",
        resolution_chain=resolution.chain,
    )


def acquire_terminal_vacancy_evidence(
    source_url: str,
    *,
    fetcher: Fetcher,
    fallback_fetcher: Fetcher | None = None,
) -> TerminalVacancyEvidence | None:
    """Acquire complete terminal vacancy evidence with one bounded fallback.

    The optional fallback is intended for orchestrator-owned browser rendering
    when ordinary HTTP is blocked/JS-only. Jobs still owns evidence policy;
    the fallback only supplies a different transport view of the same URL.
    """
    if not source_url or is_source_message_url(source_url):
        return None
    evidence = _acquire_once(source_url, fetcher)
    if evidence is not None or fallback_fetcher is None:
        return evidence
    return _acquire_once(source_url, fallback_fetcher)
