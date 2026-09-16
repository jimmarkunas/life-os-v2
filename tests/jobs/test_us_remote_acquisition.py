from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from lifeos.core.http import HttpResponse
from lifeos.core.runtime import RunContext
from lifeos.jobs.fit_scoring import FitProfile, RoleFamily
from lifeos.jobs.newsletter_adapter import NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.repository import InMemoryCareerRepository
from lifeos.jobs.terminal_evidence import FetchResponse
from lifeos.jobs.us_remote_acquisition import USRemoteAcquirer
from lifeos.newsletter.models import SourceVacancyObservation


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def request_json(self, context, method, url, **kwargs):
        self.calls.append(url)
        if "greenhouse" in url:
            return {"jobs": [{"id": 42, "title": "Senior Technical Program Manager", "absolute_url": "https://greenhouse.io/acme/jobs/42", "location": {"name": "Remote - United States"}}]}
        raise RuntimeError("synthetic blocked source")

    def request(self, context, method, url, **kwargs):
        self.calls.append(url)
        raise RuntimeError("synthetic blocked html source")


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchResponse]):
        self.responses = responses

    def get(self, url: str) -> FetchResponse:
        return self.responses[url]


JOB_HTML = '''<html><script type="application/ld+json">{"@type":"JobPosting","description":"Lead enterprise delivery and integrations.","datePosted":"2026-09-15"}</script></html>'''


def _context() -> RunContext:
    return RunContext.start(timeout_seconds=45.0, now=datetime.now(timezone.utc))


def test_public_source_registry_has_exact_43_sources_and_no_personal_exclusions():
    registry = json.loads((Path(__file__).parents[2] / "contracts" / "us_remote_sources.json").read_text())
    assert len(registry["tier1_employers"]) == 30
    assert len(registry["staffing_agencies"]) == 10
    assert len(registry["discovery_helpers"]) == 3
    assert "hard_excluded_employers" not in registry


def test_acquisition_accounts_for_deterministic_and_browser_recovered_sources():
    registry = {
        "tier1_employers": [
            {"id": "acme", "company": "Acme", "kind": "greenhouse", "slug": "acme", "enabled": True},
            {"id": "browser-only", "company": "Browser Co", "kind": "html", "url": "https://example.test/jobs", "enabled": True},
        ],
        "staffing_agencies": [],
        "discovery_helpers": [],
    }
    browser = {"sources": [{"source_id": "browser-only", "state": "RECOVERED", "candidates": [{"title": "Technical Program Manager", "company": "Browser Co", "location": "Remote - US", "url": "https://careers.browser.test/jobs/7"}]}]}
    result = USRemoteAcquirer(context=_context(), http=FakeHttp()).acquire(registry, browser_evidence=browser, full_sweep=True)
    assert result.complete is True
    assert len(result.sources) == 2
    assert {row.source_id for row in result.sources} == {"acme", "browser-only"}
    assert len(result.observations) == 2
    assert all(obs.evidence_ref.startswith("us-web:") for obs in result.observations)


def test_newsletter_and_web_same_vacancy_mutate_canonical_job_once():
    url = "https://greenhouse.io/acme/jobs/42"
    fetcher = FakeFetcher({url: FetchResponse(final_url=url, body=JOB_HTML)})
    profile = FitProfile(model_version="synthetic", role_families=(RoleFamily(patterns=(r"technical program manager",), base_score=90, label="TPM"),), default_role_base=10, default_role_label="other")
    lane = LaneConfig(name="US Remote", market="US", fit_floor=0, target_review_floor=None, work_mode_policy="any", compensation_floor=None, freshness_gate=False, freshness_max_days=None)
    priority = {"US Remote": 0}
    base = dict(source_mailbox="synthetic", source_message_id="1", source_subject="jobs", company="Acme", role="Technical Program Manager", location_text="Remote - US", compensation_text=None, source_apply_url=url, provider_job_id=None, provider_score=None, issues=(), source_received_at=datetime(2026, 9, 16, tzinfo=timezone.utc))
    newsletter = SourceVacancyObservation(evidence_ref="newsletter:1", source_provider="newsletter", **base)
    web = SourceVacancyObservation(evidence_ref="web:1", source_provider="web", **base)
    newsletter_adapter = NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=fetcher, fit_profile=profile, market="US", source_lane="Newsletter"))
    web_adapter = NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=fetcher, fit_profile=profile, market="US", source_lane="US Web"))
    candidates = [newsletter_adapter.to_jobs_candidate(newsletter), web_adapter.to_jobs_candidate(web)]
    repo = InMemoryCareerRepository()
    results = ingest(candidates, lane=lane, lane_priority=priority, repository=repo, run_date=date(2026, 9, 16), context=_context())
    assert {row.disposition for row in results} == {Disposition.CREATED, Disposition.DUPLICATE}
    assert len({row.stable_job_key for row in results}) == 1
    assert len(repo.get_many([results[0].stable_job_key])) == 1
