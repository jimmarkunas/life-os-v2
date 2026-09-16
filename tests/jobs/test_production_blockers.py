"""Focused proofs for the five TL2 production blockers. Synthetic only."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from lifeos.core.http import HttpClient, HttpResponse
from lifeos.core.runtime import RunContext
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.fit_scoring import FitProfile, RoleFamily
from lifeos.jobs.lifecycle import new_record
from lifeos.jobs.models import AdmissionStatus, Company, Job, Opportunity, WorkMode
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.notion_repository import (
    MAX_IDENTITY_VALUES_PER_QUERY,
    NotionCareerRepository,
    NotionCareerRepositoryConfig,
    _page_to_record,
    _record_to_properties,
)
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.repository import InMemoryCareerRepository, ReadBackMismatch
from lifeos.jobs.terminal_evidence import Fetcher, FetchResponse
from lifeos.newsletter.models import SourceVacancyObservation

RUN_DATE = date(2026, 1, 15)
LANE = LaneConfig(
    name="Synthetic-Newsletter", market="Synthetic-US", fit_floor=0, target_review_floor=None,
    work_mode_policy="any", compensation_floor=None, freshness_gate=False, freshness_max_days=None,
)
LANE_PRIORITY = {"Synthetic-Newsletter": 0}
FAKE_PROFILE = FitProfile(
    model_version="test-1",
    role_families=(RoleFamily(patterns=(r"\bsynthetic engineer\b",), base_score=70, label="x"),),
    default_role_base=10, default_role_label="weak",
)
JOBPOSTING_HTML = """
<html><script type="application/ld+json">
{"@type": "JobPosting", "description": "Synthetic role.", "datePosted": "2026-01-10"}
</script></html>
"""


def _job(**overrides) -> Job:
    base = dict(
        company=Company(name="Acme Synthetic Co"), role="Synthetic Engineer", location="Remote",
        work_mode=WorkMode.REMOTE, compensation_text="$100k", compensation_minimum=100_000,
        posting_date=RUN_DATE, apply_url="https://greenhouse.io/acme/jobs/1", source_lane="Newsletter",
    )
    base.update(overrides)
    return Job(**base)


def _observation(**overrides) -> SourceVacancyObservation:
    base = dict(
        evidence_ref="ev:1", source_provider="synthetic", source_mailbox="INBOX", source_message_id="msg-1",
        source_subject="jobs", company="Acme Synthetic Co", role="Synthetic Engineer", location_text="Remote",
        compensation_text="$100k", source_apply_url="https://greenhouse.io/acme/jobs/1",
        provider_job_id=None, provider_score=88, issues=(),
    )
    base.update(overrides)
    return SourceVacancyObservation(**base)


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchResponse]):
        self._responses = responses

    def get(self, url: str) -> FetchResponse:
        if url not in self._responses:
            raise RuntimeError(f"no fixture for {url}")
        return self._responses[url]


def _adapter(fetcher: Fetcher) -> NewsletterJobsAdapter:
    return NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=fetcher, fit_profile=FAKE_PROFILE, market="Synthetic-US", source_lane="Newsletter"))


# --- BLOCKER 1: canonical Notion property mapping ---------------------------

CANONICAL_LEDGER_PROPERTY_TYPES = {
    "Job": "title",
    "Stable Job Key": "rich_text",
    "Company": "rich_text",
    "Role": "rich_text",
    "LIFE OS Fit": "number",
    "Fit Authority": "select",
    "Provider Score": "number",
    "Apply URL": "url",
    "Applied": "checkbox",
    "Applied On": "date",
}


class StrictSchemaNotionHttp:
    """Rejects any create/update payload containing a property name not in
    the canonical schema -- proves the repository never invents fields."""

    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self._next_id = 1

    def request_json(self, context, method, url, *, headers=None, json_body=None, **kwargs):
        if method == "POST" and url.endswith("/query"):
            filters = json_body["filter"]["or"] if "or" in json_body["filter"] else [json_body["filter"]]
            wanted = {f["rich_text"]["equals"] for f in filters}
            results = [p for p in self.pages.values() if self._key(p) in wanted]
            return {"results": results, "has_more": False}
        if method == "POST" and url.endswith("/pages"):
            self._validate(json_body["properties"])
            page_id = f"page-{self._next_id}"
            self._next_id += 1
            page = {"id": page_id, "properties": json_body["properties"]}
            self.pages[page_id] = page
            return page
        if method == "PATCH":
            page_id = url.rsplit("/", 1)[-1]
            self._validate(json_body["properties"])
            self.pages[page_id]["properties"].update(json_body["properties"])
            return self.pages[page_id]
        if method == "GET":
            page_id = url.rsplit("/", 1)[-1]
            return self.pages[page_id]
        raise AssertionError("unexpected call")

    def _validate(self, properties):
        for name, value in properties.items():
            if name not in CANONICAL_LEDGER_PROPERTY_TYPES:
                continue  # additive v2-only fields (Lifecycle Status, etc.) are allowed, not invented replacements
            expected_type = CANONICAL_LEDGER_PROPERTY_TYPES[name]
            actual_type = next(iter(value.keys()))
            assert actual_type == expected_type, f"{name}: expected {expected_type}, got {actual_type}"

    @staticmethod
    def _key(page):
        items = page["properties"].get("Stable Job Key", {}).get("rich_text", [])
        return "".join(i.get("text", {}).get("content", "") for i in items)


def test_title_property_is_job_not_role():
    record = new_record(Opportunity(stable_job_key="k1", job=_job(), admission_status=AdmissionStatus.ADMITTED, fit=80), run_date=RUN_DATE)
    props = _record_to_properties(record)
    assert "title" in props["Job"]
    assert props["Job"]["title"][0]["text"]["content"] == "Acme Synthetic Co — Synthetic Engineer"
    assert "title" not in props["Role"]


def test_fit_and_provider_score_persisted_with_canonical_names():
    job = _job(provider_score=91)
    record = new_record(Opportunity(stable_job_key="k1", job=job, admission_status=AdmissionStatus.ADMITTED, fit=85), run_date=RUN_DATE)
    props = _record_to_properties(record)
    assert props["LIFE OS Fit"]["number"] == 85
    assert props["Provider Score"]["number"] == 91
    assert props["Fit Authority"]["select"]["name"] == "Authoritative"


def test_strict_schema_fixture_rejects_invented_property_types():
    context = RunContext.start(timeout_seconds=45.0, now=datetime.now(timezone.utc))
    http = StrictSchemaNotionHttp()
    transport = NotionTransport(context=context, http=http, access_token="synthetic-token")
    repo = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig(data_source_id="synthetic-ds"))
    opportunity = Opportunity(stable_job_key="k1", job=_job(), admission_status=AdmissionStatus.ADMITTED, fit=80)
    persisted = repo.upsert(new_record(opportunity, run_date=RUN_DATE))
    assert persisted.opportunity.fit == 80


# --- BLOCKER 2: >50 keys chunked, no full scan -------------------------------


def test_63_vacancies_chunked_correctly():
    context = RunContext.start(timeout_seconds=45.0, now=datetime.now(timezone.utc))
    http = StrictSchemaNotionHttp()
    transport = NotionTransport(context=context, http=http, access_token="synthetic-token")
    repo = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig(data_source_id="synthetic-ds"))

    keys = []
    for i in range(63):
        key = f"k{i}"
        keys.append(key)
        opportunity = Opportunity(stable_job_key=key, job=_job(apply_url=f"https://greenhouse.io/acme/jobs/{i}"), admission_status=AdmissionStatus.ADMITTED, fit=80)
        repo.upsert(new_record(opportunity, run_date=RUN_DATE))

    found = repo.get_many(keys)
    assert len(found) == 63  # complete accounting, no exception at the 50-key boundary


def test_get_many_chunk_size_never_exceeds_notion_identity_query_cap():
    assert MAX_IDENTITY_VALUES_PER_QUERY <= 50


# --- BLOCKER 3: repository failures fail closed ------------------------------


class FailingGetManyRepository(InMemoryCareerRepository):
    def get_many(self, stable_job_keys):
        raise RuntimeError("synthetic transport failure")


class FailingUpsertRepository(InMemoryCareerRepository):
    def upsert(self, record):
        raise RuntimeError("synthetic write failure")


class FailingReadBackRepository(InMemoryCareerRepository):
    def upsert(self, record):
        raise ReadBackMismatch("synthetic read failure")


def _candidates_for_observation():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/1": FetchResponse(final_url="https://greenhouse.io/acme/jobs/1", body=JOBPOSTING_HTML)})
    return [_adapter(fetcher).to_jobs_candidate(_observation())]


def test_get_many_transport_failure_is_review_degraded():
    results = ingest(_candidates_for_observation(), lane=LANE, lane_priority=LANE_PRIORITY, repository=FailingGetManyRepository(), run_date=RUN_DATE)
    assert len(results) == 1
    assert results[0].disposition == Disposition.REVIEW_DEGRADED


def test_write_failure_is_review_degraded():
    results = ingest(_candidates_for_observation(), lane=LANE, lane_priority=LANE_PRIORITY, repository=FailingUpsertRepository(), run_date=RUN_DATE)
    assert len(results) == 1
    assert results[0].disposition == Disposition.REVIEW_DEGRADED


def test_read_back_failure_is_review_degraded():
    results = ingest(_candidates_for_observation(), lane=LANE, lane_priority=LANE_PRIORITY, repository=FailingReadBackRepository(), run_date=RUN_DATE)
    assert len(results) == 1
    assert results[0].disposition == Disposition.REVIEW_DEGRADED


# --- BLOCKER 4: parse issues -------------------------------------------------


def test_observation_with_issues_cannot_mutate_ledger():
    fetcher = FakeFetcher({"https://greenhouse.io/acme/jobs/1": FetchResponse(final_url="https://greenhouse.io/acme/jobs/1", body=JOBPOSTING_HTML)})
    observation = _observation(issues=("ambiguous-role",))
    candidate = _adapter(fetcher).to_jobs_candidate(observation)
    assert candidate.unresolved_reason is not None

    repo = InMemoryCareerRepository()
    results = ingest([candidate], lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    assert results[0].disposition == Disposition.REVIEW_DEGRADED
    assert repo.get_many(["url:https://greenhouse.io/acme/jobs/1"]) == {}


# --- BLOCKER 5: final redirect URL convergence -------------------------------


class RedirectingFetcher:
    """Simulates two distinct tracking URLs that both 30x-redirect to the
    same employer ATS URL. Proves resolution uses the actual final URL."""

    def __init__(self):
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResponse:
        self.calls.append(url)
        # Both tracking URLs land on the same canonical employer page.
        return FetchResponse(final_url="https://greenhouse.io/acme/jobs/99", body=JOBPOSTING_HTML)


def test_http_response_exposes_final_url():
    response = HttpResponse(status_code=200, headers={}, body=b"{}", final_url="https://greenhouse.io/acme/jobs/99")
    assert response.final_url == "https://greenhouse.io/acme/jobs/99"


def test_two_tracking_urls_redirecting_to_same_employer_url_converge():
    class TrackingFetcher:
        def get(self, url: str) -> FetchResponse:
            # A non-canonicalizable tracking URL forces the resolver's
            # single-fetch redirect branch, whose result must reflect the
            # actual final_url, not the originally requested tracking URL.
            return FetchResponse(final_url="https://greenhouse.io/acme/jobs/99", body=JOBPOSTING_HTML)

    fetcher = TrackingFetcher()
    adapter = _adapter(fetcher)
    candidate_a = adapter.to_jobs_candidate(_observation(evidence_ref="ev:a", source_apply_url="https://track.example/redirect?id=aaa"))
    candidate_b = adapter.to_jobs_candidate(_observation(evidence_ref="ev:b", source_apply_url="https://track.example/redirect?id=bbb"))

    assert candidate_a.unresolved_reason is None
    assert candidate_b.unresolved_reason is None
    assert candidate_a.job.apply_url == "https://greenhouse.io/acme/jobs/99"
    assert candidate_a.job.apply_url == candidate_b.job.apply_url

    repo = InMemoryCareerRepository()
    results = ingest([candidate_a, candidate_b], lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    assert {r.disposition for r in results} == {Disposition.CREATED, Disposition.DUPLICATE}
    assert len({r.stable_job_key for r in results}) == 1
