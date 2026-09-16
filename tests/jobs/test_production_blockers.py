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
from lifeos.jobs.terminal_evidence import Fetcher, FetchResponse, resolve_final_vacancy_url
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
    "Location / Work Mode": "rich_text",
    "Work Mode": "select",
    "Compensation": "rich_text",
    "Apply URL": "url",
    "Posting Date": "date",
    "LIFE OS Fit": "number",
    "Fit Authority": "select",
    "Provider Score": "number",
    "Admission Status": "select",
    "Applied": "checkbox",
    "Applied On": "date",
    "First Surfaced": "date",
    "Last Seen": "date",
}

CANONICAL_ADMISSION_LABELS = {"Admitted", "Passed / Review", "Excluded"}
CANONICAL_WORK_MODE_LABELS = {"Remote", "Hybrid", "Onsite", "Unknown"}


class StrictSchemaNotionHttp:
    """Rejects any create/update payload containing a property name not in
    the canonical schema, a wrong Notion property type, or an internal
    (non-canonical) enum option label -- proves the repository never
    invents fields and never leaks internal wire enum values."""

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
            assert name in CANONICAL_LEDGER_PROPERTY_TYPES, f"invented property name: {name!r}"
            expected_type = CANONICAL_LEDGER_PROPERTY_TYPES[name]
            actual_type = next(iter(value.keys()))
            assert actual_type == expected_type, f"{name}: expected {expected_type}, got {actual_type}"
            if name == "Admission Status":
                label = value["select"]["name"]
                assert label in CANONICAL_ADMISSION_LABELS, f"non-canonical Admission Status label: {label!r}"
            if name == "Work Mode":
                label = value["select"]["name"]
                assert label in CANONICAL_WORK_MODE_LABELS, f"non-canonical Work Mode label: {label!r}"

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


def test_no_emitted_property_is_outside_canonical_schema():
    record = new_record(Opportunity(stable_job_key="k1", job=_job(), admission_status=AdmissionStatus.ADMITTED, fit=80), run_date=RUN_DATE)
    props = _record_to_properties(record)
    assert set(props) <= set(CANONICAL_LEDGER_PROPERTY_TYPES)
    # Blocker 1's originally-invented properties must never appear.
    for invented in ("Compensation Minimum", "Source Lane", "Provider Job ID", "Description",
                      "Source Lanes", "Aliases", "Lifecycle Status", "Review Ready On", "Live"):
        assert invented not in props


def test_invented_property_name_fails_strict_schema_fixture():
    http = StrictSchemaNotionHttp()
    with pytest.raises(AssertionError):
        http._validate({"Source Lane": {"rich_text": []}})


def test_internal_enum_wire_values_fail_strict_schema_fixture():
    http = StrictSchemaNotionHttp()
    with pytest.raises(AssertionError):
        http._validate({"Admission Status": {"select": {"name": "admitted"}}})
    with pytest.raises(AssertionError):
        http._validate({"Work Mode": {"select": {"name": "remote"}}})


def test_admission_status_and_work_mode_use_canonical_labels():
    record = new_record(
        Opportunity(stable_job_key="k1", job=_job(work_mode=WorkMode.REMOTE), admission_status=AdmissionStatus.ADMITTED, fit=80),
        run_date=RUN_DATE,
    )
    props = _record_to_properties(record)
    assert props["Admission Status"]["select"]["name"] == "Admitted"
    assert props["Work Mode"]["select"]["name"] == "Remote"


def test_applied_state_survives_upsert_read_back():
    from lifeos.jobs.lifecycle import mark_applied

    context = RunContext.start(timeout_seconds=45.0, now=datetime.now(timezone.utc))
    http = StrictSchemaNotionHttp()
    transport = NotionTransport(context=context, http=http, access_token="synthetic-token")
    repo = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig(data_source_id="synthetic-ds"))
    opportunity = Opportunity(stable_job_key="k1", job=_job(), admission_status=AdmissionStatus.ADMITTED, fit=80)
    record = mark_applied(new_record(opportunity, run_date=RUN_DATE), run_date=RUN_DATE)
    persisted = repo.upsert(record)
    assert persisted.applied is True
    assert persisted.applied_on == RUN_DATE


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


# --- BLOCKER 2 (Tech Lead 2 re-review): job-looking tracking URLs on an ----
# --- unknown host must not fork one vacancy --------------------------------


def test_two_job_looking_tracking_urls_on_unknown_host_converge_via_verified_redirect():
    """Both https://track.example/apply?id=aaa and .../apply?id=bbb have a
    path that matches JOB_PATH_HINTS ("/apply") on a host that is neither a
    known discovery intermediary nor a trusted ATS host. Neither may become
    canonical on path shape alone -- both must be verified via a single
    bounded fetch and converge on the actual HTTP-redirected ATS URL."""

    class TrackingRedirectFetcher:
        def __init__(self):
            self.calls: list[str] = []

        def get(self, url: str) -> FetchResponse:
            self.calls.append(url)
            return FetchResponse(final_url="https://greenhouse.io/acme/jobs/99", body=JOBPOSTING_HTML)

    fetcher = TrackingRedirectFetcher()
    adapter = _adapter(fetcher)
    candidate_a = adapter.to_jobs_candidate(_observation(evidence_ref="ev:a", source_apply_url="https://track.example/apply?id=aaa"))
    candidate_b = adapter.to_jobs_candidate(_observation(evidence_ref="ev:b", source_apply_url="https://track.example/apply?id=bbb"))

    assert candidate_a.unresolved_reason is None
    assert candidate_b.unresolved_reason is None
    # Neither tracking URL itself became canonical identity.
    assert candidate_a.job.apply_url == "https://greenhouse.io/acme/jobs/99"
    assert candidate_b.job.apply_url == "https://greenhouse.io/acme/jobs/99"
    assert candidate_a.job.apply_url == candidate_b.job.apply_url
    # A verification fetch was actually performed for each tracking URL.
    assert "https://track.example/apply?id=aaa" in fetcher.calls
    assert "https://track.example/apply?id=bbb" in fetcher.calls

    repo = InMemoryCareerRepository()
    results = ingest([candidate_a, candidate_b], lane=LANE, lane_priority=LANE_PRIORITY, repository=repo, run_date=RUN_DATE)
    assert {r.disposition for r in results} == {Disposition.CREATED, Disposition.DUPLICATE}
    assert len({r.stable_job_key for r in results}) == 1  # exactly one canonical mutation


def test_job_looking_path_on_unknown_host_fails_closed_without_verified_redirect():
    """An unknown host whose path merely looks job-like must never resolve
    to itself as canonical merely because no distinct, trusted terminal
    destination was verified."""

    class SameHostFetcher:
        def get(self, url: str) -> FetchResponse:
            return FetchResponse(final_url=url, body="<html></html>")

    result = resolve_final_vacancy_url("https://track.example/apply?id=aaa", fetcher=SameHostFetcher())
    assert result.final_url is None


def test_genuine_trusted_ats_url_still_skips_verification_fetch():
    fetcher = FakeFetcher({})
    result = resolve_final_vacancy_url("https://greenhouse.io/acme/jobs/1?utm_source=x", fetcher=fetcher)
    assert result.final_url == "https://greenhouse.io/acme/jobs/1"
