"""Bounded US Remote public-web acquisition.

This module owns source enumeration only. It emits provider-neutral vacancy
observations and source health; Jobs identity/Fit/qualification/persistence
remain in the shared Jobs engine. Source calls share the caller's RunContext
and HttpClient, so the execution-wide HTTP ceiling and deadline still apply.

Steady-state acquisition is incremental without a second datastore: structured
APIs keep only recently changed postings when source timestamps exist, while
HTML/search sources are deterministically sharded across a six-hour cadence.
Explicit recovery/full-sweep mode scans every configured source.
"""
from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext
from lifeos.jobs.terminal_evidence import Fetcher
from lifeos.newsletter.models import SourceVacancyObservation

MAX_SOURCE_WORKERS = 8
HTML_STEADY_STATE_INTERVAL_HOURS = 6
_READ = RetryPolicy(max_attempts=2, backoff_seconds=0.1, max_backoff_seconds=0.5)
_ROLE = re.compile(
    r"\b(program manager|technical program|project manager|technical project|product manager|"
    r"product owner|solution(?:s)? architect|enterprise architect|delivery|implementation|"
    r"transformation|scrum master|pmo|portfolio|platform|integration|migration|release manager|"
    r"agentic|artificial intelligence|\bai\b)\b",
    re.I,
)
_REMOTE = re.compile(r"\b(remote|distributed|work from home|wfh)\b", re.I)
_US = re.compile(r"\b(united states|usa|u\.s\.|us|north america|americas)\b", re.I)
_JOB_PATH = re.compile(r"/(?:job|jobs|career|careers|position|positions|opening|openings|requisition)/", re.I)
_JOB_QUERY_KEYS = frozenset({"id", "job", "jobid", "job_id", "position", "positionid", "requisition", "req"}); _CONTROL_TEXT = re.compile(r"\b(?:view all|see all|search|filter|category|categories|sign in|log in|subscribe|learn more|privacy|terms)\b", re.I)
_GENERIC_CTA = frozenset({"apply", "apply now", "view job", "view details", "job details", "learn more", "read more"}); _CARD_MARKERS = re.compile(r"(?:job|vacancy|position|posting|card|result-item|search-result)", re.I)


@dataclass(frozen=True)
class SourceHealth:
    source_id: str
    state: str
    candidate_count: int
    detail: str | None = None


@dataclass(frozen=True)
class AcquisitionResult:
    observations: tuple[SourceVacancyObservation, ...]
    sources: tuple[SourceHealth, ...]

    @property
    def complete(self) -> bool:
        return bool(self.sources) and all(source.state in {"COMPLETE", "NOT_DUE"} for source in self.sources)


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._stack: list[tuple[str, dict[str, str], list[str]]] = []
        self.anchors: list[tuple[str, str, dict[str, str], str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack.append((tag.casefold(), {key.casefold(): str(value) for key, value in attrs if value is not None}, []))

    def handle_data(self, data: str) -> None:
        for _, _, parts in self._stack:
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        index = next((i for i in range(len(self._stack) - 1, -1, -1) if self._stack[i][0] == tag), None)
        if index is None:
            return
        _, attrs, parts = self._stack[index]
        if tag == "a" and attrs.get("href"):
            card = next((node for node in reversed(self._stack[:index]) if _is_card_node(node)), None)
            card_attrs, card_parts = (card[1], card[2]) if card else (attrs, parts)
            self.anchors.append((attrs["href"], " ".join(" ".join(parts).split()), {**card_attrs, **attrs}, "\n".join(" ".join(part.split()) for part in card_parts if part.strip())))
        del self._stack[index:]


def _is_card_node(node: tuple[str, dict[str, str], list[str]]) -> bool:
    tag, attrs, _ = node
    return tag in {"article", "li"} or any(key.startswith("data-job") or key in {"data-title", "data-job-title", "data-requisition-id"} for key in attrs) or bool(_CARD_MARKERS.search(f"{attrs.get('class', '')} {attrs.get('id', '')}"))
def _vacancy_url(url: str, anchor_text: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    path = (parts.path or "").rstrip("/").casefold()
    if not path or _CONTROL_TEXT.search(anchor_text) or path in {"/jobs", "/careers", "/job-search", "/search"}: return False
    return bool(_JOB_PATH.search(path) or any(key.casefold() in _JOB_QUERY_KEYS and value for key, values in parse_qs(parts.query).items() for value in values) or re.search(r"/(?:view|detail|posting|vacancy|opening|position|requisition)(?:/|$)", path, re.I))


def _role_from_anchor(anchor_text: str, anchor_attrs: dict[str, str], card_text: str) -> str | None:
    values = [anchor_attrs.get(key) for key in ("data-job-title", "data-title", "aria-label", "title")] + [anchor_text]
    values.extend(part.strip(" -|·") for part in re.split(r"\s*[|\n]\s*", card_text) if part.strip())
    for value in values:
        candidate = " ".join((value or "").split()).strip()
        if not candidate or candidate.casefold() in _GENERIC_CTA or _CONTROL_TEXT.search(candidate): continue
        if _ROLE.search(candidate) and len(candidate) <= 180: return candidate
    return None


def _opaque_ref(source_id: str, provider_id: str | None, url: str | None) -> str:
    raw = f"{source_id}|{provider_id or ''}|{url or ''}".encode("utf-8")
    return f"us-web:{source_id}:{hashlib.sha256(raw).hexdigest()[:16]}"


def _observation(
    *,
    source_id: str,
    company: str,
    role: str,
    url: str | None,
    location: str | None = None,
    compensation: str | None = None,
    provider_job_id: str | None = None,
    received_at: datetime,
) -> SourceVacancyObservation:
    return SourceVacancyObservation(
        evidence_ref=_opaque_ref(source_id, provider_job_id, url),
        source_provider=source_id,
        source_mailbox="public-web",
        source_message_id=source_id,
        source_subject=role,
        company=company,
        role=role,
        location_text=location,
        compensation_text=compensation,
        source_apply_url=url,
        provider_job_id=provider_job_id,
        source_received_at=received_at,
    )


def _plausible(role: str, location: str | None) -> bool:
    if not _ROLE.search(role or ""):
        return False
    if not location:
        return True
    return bool(_REMOTE.search(location) or _US.search(location))


def _source_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _recent_payload(item: dict[str, Any], since: datetime | None, *keys: str) -> bool:
    if since is None:
        return True
    for key in keys:
        if not item.get(key):
            continue
        parsed = _source_datetime(item.get(key))
        return True if parsed is None else parsed >= since
    # Unknown timestamp stays eligible; fail-open on acquisition, fail-closed later on evidence.
    return True


def _source_due(source: dict[str, Any], now: datetime, *, full_sweep: bool) -> bool:
    if full_sweep or str(source.get("kind") or "") != "html":
        return True
    interval = max(1, int(source.get("steady_state_interval_hours") or HTML_STEADY_STATE_INTERVAL_HOURS))
    if interval == 1:
        return True
    digest = hashlib.sha256(str(source.get("id") or "").encode("utf-8")).hexdigest()
    shard = int(digest[:8], 16) % interval
    return now.hour % interval == shard


class USRemoteAcquirer:
    def __init__(
        self,
        *,
        context: RunContext,
        http: HttpClient,
        fallback_fetcher: Fetcher | None = None,
        max_workers: int = MAX_SOURCE_WORKERS,
    ) -> None:
        self._context = context
        self._http = http
        self._fallback_fetcher = fallback_fetcher
        self._workers = max(1, min(int(max_workers), MAX_SOURCE_WORKERS))

    def acquire(
        self,
        registry: dict[str, Any],
        *,
        browser_evidence: dict[str, Any] | None = None,
        since: datetime | None = None,
        full_sweep: bool = False,
        now: datetime | None = None,
    ) -> AcquisitionResult:
        if since is not None and since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        current = now or datetime.now(timezone.utc)
        sources = [
            source
            for bucket in ("tier1_employers", "staffing_agencies", "discovery_helpers")
            for source in registry.get(bucket, [])
            if source.get("enabled", True)
        ]
        browser_by_id = {
            str(item.get("source_id")): item
            for item in (browser_evidence or {}).get("sources", [])
            if isinstance(item, dict) and item.get("source_id")
        }
        observations: list[SourceVacancyObservation] = []
        health: list[SourceHealth] = []
        due_sources: list[dict[str, Any]] = []

        for source in sources:
            if _source_due(source, current, full_sweep=full_sweep):
                due_sources.append(source)
            else:
                health.append(SourceHealth(str(source.get("id")), "NOT_DUE", 0, "steady-state-cadence"))

        effective_since = None if full_sweep else since
        if due_sources:
            with ThreadPoolExecutor(max_workers=min(self._workers, len(due_sources))) as pool:
                for start in range(0, len(due_sources), self._workers):
                    chunk = due_sources[start : start + self._workers]
                    futures = {
                        pool.submit(self._enumerate_source, source, current, effective_since): source
                        for source in chunk
                    }
                    for future in as_completed(futures):
                        source = futures[future]
                        try:
                            rows = future.result()
                            state, detail = "COMPLETE", None
                        except Exception as exc:
                            recovered = self._browser_recovery(
                                source,
                                browser_by_id.get(str(source.get("id"))),
                                current,
                            )
                            if recovered is None:
                                rows = []
                                state = "DEGRADED"
                                detail = type(exc).__name__
                            else:
                                rows = recovered
                                state = "COMPLETE"
                                detail = "browser-recovery"
                        observations.extend(rows)
                        health.append(SourceHealth(str(source.get("id")), state, len(rows), detail))

        observations.sort(
            key=lambda item: (item.source_provider, item.company or "", item.role or "", item.evidence_ref)
        )
        health.sort(key=lambda item: item.source_id)
        return AcquisitionResult(tuple(observations), tuple(health))

    def _enumerate_source(
        self,
        source: dict[str, Any],
        now: datetime,
        since: datetime | None,
    ) -> list[SourceVacancyObservation]:
        self._context.require_time()
        kind = str(source.get("kind") or "")
        if kind == "greenhouse":
            return self._greenhouse(source, now, since)
        if kind == "ashby":
            return self._ashby(source, now, since)
        if kind == "smartrecruiters":
            return self._smartrecruiters(source, now, since)
        if kind == "workday_adobe":
            return self._workday_adobe(source, now, since)
        if kind == "html":
            return self._html(source, now)
        raise ValueError("unsupported-source-kind")

    def _json(self, method: str, url: str, *, json_body: Any | None = None) -> Any:
        return self._http.request_json(
            self._context,
            method,
            url,
            json_body=json_body,
            timeout_seconds=8.0,
            retry=_READ,
            headers={"Accept": "application/json", "User-Agent": "LIFE-OS-v2/1.0"},
        )

    def _greenhouse(
        self, source: dict[str, Any], now: datetime, since: datetime | None
    ) -> list[SourceVacancyObservation]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{source['slug']}/jobs?content=true"
        payload = self._json("GET", url)
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        out = []
        for job in jobs:
            if not isinstance(job, dict) or not _recent_payload(job, since, "updated_at", "created_at"):
                continue
            role = str(job.get("title") or "")
            location = str((job.get("location") or {}).get("name") or "")
            if _plausible(role, location):
                out.append(
                    _observation(
                        source_id=source["id"],
                        company=source["company"],
                        role=role,
                        url=job.get("absolute_url"),
                        location=location,
                        provider_job_id=str(job.get("id") or "") or None,
                        received_at=now,
                    )
                )
        return out

    def _ashby(
        self, source: dict[str, Any], now: datetime, since: datetime | None
    ) -> list[SourceVacancyObservation]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{source['slug']}?includeCompensation=true"
        payload = self._json("GET", url)
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        out = []
        for job in jobs:
            if not isinstance(job, dict) or not _recent_payload(
                job, since, "updatedAt", "publishedAt", "createdAt"
            ):
                continue
            role = str(job.get("title") or "")
            locations = [str(job.get("location") or "")] + [
                str(item.get("location") or item.get("name") or "")
                for item in job.get("secondaryLocations", [])
                if isinstance(item, dict)
            ]
            location = " | ".join(part for part in locations if part)
            if _plausible(role, location):
                compensation = json.dumps(job.get("compensation"), sort_keys=True) if job.get("compensation") else None
                out.append(
                    _observation(
                        source_id=source["id"],
                        company=source["company"],
                        role=role,
                        url=job.get("jobUrl") or job.get("applyUrl"),
                        location=location,
                        compensation=compensation,
                        provider_job_id=str(job.get("id") or job.get("jobId") or "") or None,
                        received_at=now,
                    )
                )
        return out

    def _smartrecruiters(
        self, source: dict[str, Any], now: datetime, since: datetime | None
    ) -> list[SourceVacancyObservation]:
        base = f"https://api.smartrecruiters.com/v1/companies/{source['slug']}/postings"
        jobs = []; out = []
        offset = 0
        while offset < 2000:
            payload = self._json("GET", f"{base}?limit=100&offset={offset}")
            batch = payload.get("content", []) if isinstance(payload, dict) else []
            if not batch: break
            jobs.extend(batch)
            offset += len(batch)
            total_found = payload.get("totalFound") if isinstance(payload, dict) else None
            if isinstance(total_found, int) and offset >= total_found:
                break
        for job in jobs:
            if not isinstance(job, dict) or not _recent_payload(
                job, since, "lastUpdatedDate", "releasedDate"
            ):
                continue
            loc = job.get("location") or {}
            location = ", ".join(
                str(loc.get(k) or "") for k in ("city", "region", "country") if loc.get(k)
            )
            if loc.get("remote"):
                location = (location + " · Remote").strip(" ·")
            role = str(job.get("name") or "")
            job_id = str(job.get("id") or "")
            if _plausible(role, location):
                out.append(
                    _observation(
                        source_id=source["id"],
                        company=source["company"],
                        role=role,
                        url=f"https://jobs.smartrecruiters.com/{source['slug']}/{job_id}" if job_id else None,
                        location=location,
                        provider_job_id=job_id or None,
                        received_at=now,
                    )
                )
        return out

    def _workday_adobe(
        self, source: dict[str, Any], now: datetime, since: datetime | None
    ) -> list[SourceVacancyObservation]:
        jobs = []; out = []
        offset = 0
        while offset < 2000:
            payload = self._json(
                "POST",
                source["url"],
                json_body={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
            )
            batch = payload.get("jobPostings", []) if isinstance(payload, dict) else []
            if not batch: break
            jobs.extend(batch)
            offset += len(batch)
            total = payload.get("total") if isinstance(payload, dict) else None
            if isinstance(total, int) and offset >= total:
                break
        for job in jobs:
            if not isinstance(job, dict) or not _recent_payload(job, since, "postedDate", "startDate"):
                continue
            role = str(job.get("title") or "")
            location = str(job.get("locationsText") or "")
            path = job.get("externalPath")
            if _plausible(role, location):
                out.append(
                    _observation(
                        source_id=source["id"],
                        company=source["company"],
                        role=role,
                        url=f"https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced{path}" if path else None,
                        location=location,
                        provider_job_id=str(path or "") or None,
                        received_at=now,
                    )
                )
        return out

    def _html(self, source: dict[str, Any], now: datetime) -> list[SourceVacancyObservation]:
        response = self._http.request(
            self._context,
            "GET",
            source["url"],
            timeout_seconds=8.0,
            retry=_READ,
            headers={"Accept": "text/html,*/*", "User-Agent": "Mozilla/5.0 LIFE-OS-v2/1.0"},
        )
        body = response.body.decode("utf-8", errors="replace")
        rows = self._html_rows(source, body, response.final_url or source["url"], now)
        if not rows:
            raise RuntimeError("no-deterministic-vacancy-links")
        return rows

    @staticmethod
    def _html_rows(
        source: dict[str, Any], body: str, base_url: str, now: datetime
    ) -> list[SourceVacancyObservation]:
        parser = _AnchorParser()
        parser.feed(body)
        seen: set[str] = set()
        out: list[SourceVacancyObservation] = []
        for href, text, attrs, card_text in parser.anchors:
            absolute = urljoin(base_url, href).split("#", 1)[0]
            role = _role_from_anchor(text, attrs, card_text)
            if absolute in seen or not _vacancy_url(absolute, text) or not role:
                continue
            seen.add(absolute)
            out.append(
                _observation(
                    source_id=source["id"],
                    company=source["company"],
                    role=role,
                    url=absolute,
                    location="Remote" if _REMOTE.search(f"{role} {card_text}") else None,
                    provider_job_id=None,
                    received_at=now,
                )
            )
        return out

    def _browser_recovery(
        self,
        source: dict[str, Any],
        evidence: dict[str, Any] | None,
        now: datetime,
    ) -> list[SourceVacancyObservation] | None:
        injected = self._browser_recovery_injected(source, evidence, now)
        if injected is not None:
            return injected
        if self._fallback_fetcher is None:
            return None
        target = str(source.get("browser_url") or source.get("url") or "").strip()
        if not target:
            return None
        try:
            response = self._fallback_fetcher.get(target)
            rows = self._html_rows(source, response.body, response.final_url or target, now)
        except Exception:
            return None
        return rows or None

    @staticmethod
    def _browser_recovery_injected(
        source: dict[str, Any], evidence: dict[str, Any] | None, now: datetime
    ) -> list[SourceVacancyObservation] | None:
        if not evidence:
            return None
        state = str(evidence.get("state") or "")
        if state == "SEARCHED_NO_TARGET_MATCHES":
            return []
        if state not in {"COMPLETE", "RECOVERED"}:
            return None
        rows = []
        for item in evidence.get("candidates", []):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("title") or "")
            location = str(item.get("location") or "") or None
            url = str(item.get("url") or "").strip()
            if not _ROLE.search(role) or not url or not _vacancy_url(url, role):
                continue
            rows.append(
                _observation(
                    source_id=str(source["id"]),
                    company=str(item.get("company") or source.get("company") or ""),
                    role=role,
                    url=url,
                    location=location,
                    compensation=item.get("compensation"),
                    provider_job_id=str(item.get("provider_job_id") or "") or None,
                    received_at=now,
                )
            )
        return rows
