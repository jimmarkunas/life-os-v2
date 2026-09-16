from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from lifeos.core.runtime import RunContext
from lifeos.jobs.terminal_evidence import FetchResponse
from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer


class FailingHttp:
    def request_json(self, context, method, url, **kwargs):
        raise RuntimeError("synthetic primary blocked")

    def request(self, context, method, url, **kwargs):
        raise RuntimeError("synthetic primary blocked")


class BrowserFetcher:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResponse:
        self.calls.append(url)
        return FetchResponse(final_url=url, body=self.html)


class RecentGreenhouseHttp:
    def request_json(self, context, method, url, **kwargs):
        return {
            "jobs": [
                {
                    "id": 1,
                    "title": "Technical Program Manager",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "location": {"name": "Remote - United States"},
                    "updated_at": "2026-09-16T11:00:00Z",
                },
                {
                    "id": 2,
                    "title": "Technical Program Manager",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                    "location": {"name": "Remote - United States"},
                    "updated_at": "2026-08-01T11:00:00Z",
                },
            ]
        }

    def request(self, context, method, url, **kwargs):
        raise AssertionError("unexpected html request")


def _context() -> RunContext:
    return RunContext.start(timeout_seconds=45)


def test_live_browser_fallback_recovers_blocked_html_source() -> None:
    registry = {
        "tier1_employers": [
            {
                "id": "browser-company",
                "company": "Browser Company",
                "kind": "html",
                "url": "https://careers.example.invalid/jobs",
                "enabled": True,
            }
        ],
        "staffing_agencies": [],
        "discovery_helpers": [],
    }
    browser = BrowserFetcher(
        '<html><a href="/jobs/42">Senior Technical Program Manager</a></html>'
    )
    result = USRemoteAcquirer(
        context=_context(), http=FailingHttp(), fallback_fetcher=browser
    ).acquire(registry, full_sweep=True)
    assert result.complete is True
    assert result.sources[0].detail == "browser-recovery"
    assert len(result.observations) == 1
    assert browser.calls == ["https://careers.example.invalid/jobs"]


def test_structured_api_steady_state_filters_old_unchanged_postings() -> None:
    registry = {
        "tier1_employers": [
            {"id": "acme", "company": "Acme", "kind": "greenhouse", "slug": "acme", "enabled": True}
        ],
        "staffing_agencies": [],
        "discovery_helpers": [],
    }
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    result = USRemoteAcquirer(context=_context(), http=RecentGreenhouseHttp()).acquire(
        registry,
        since=now - timedelta(hours=24),
        now=now,
    )
    assert result.complete is True
    assert len(result.observations) == 1
    assert result.observations[0].provider_job_id == "1"


def test_html_sources_are_sharded_in_steady_state_but_not_recovery() -> None:
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    interval = 24
    source_id = next(
        f"html-{index}"
        for index in range(100)
        if int(hashlib.sha256(f"html-{index}".encode()).hexdigest()[:8], 16) % interval != now.hour % interval
    )
    registry = {
        "tier1_employers": [
            {
                "id": source_id,
                "company": "Deferred Co",
                "kind": "html",
                "url": "https://deferred.example.invalid/jobs",
                "steady_state_interval_hours": interval,
                "enabled": True,
            }
        ],
        "staffing_agencies": [],
        "discovery_helpers": [],
    }
    steady = USRemoteAcquirer(context=_context(), http=FailingHttp()).acquire(registry, now=now)
    assert steady.complete is True
    assert steady.sources[0].state == "NOT_DUE"

    browser = BrowserFetcher('<html><a href="/jobs/7">Product Manager</a></html>')
    recovery = USRemoteAcquirer(
        context=_context(), http=FailingHttp(), fallback_fetcher=browser
    ).acquire(registry, now=now, full_sweep=True)
    assert recovery.sources[0].state == "COMPLETE"
    assert len(recovery.observations) == 1
