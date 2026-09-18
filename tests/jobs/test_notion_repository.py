from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from lifeos.core.runtime import RunContext
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.fit_scoring import FitProfile, RoleFamily
from lifeos.jobs.lifecycle import new_record
from lifeos.jobs.models import AdmissionStatus, Company, FitAuthority, Job, JobObservation, WorkMode
from lifeos.jobs.newsletter_adapter import NewsletterAdapterConfig, NewsletterJobsAdapter
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.terminal_evidence import FetchResponse
from lifeos.jobs.repository import ReadBackMismatch
from lifeos.newsletter.models import SourceVacancyObservation
from tests.jobs.fixtures import REMOTE_LANE, make_candidate, make_job

RUN_DATE = date(2026, 1, 15)
SYNTHETIC_DATA_SOURCE_ID = "synthetic-data-source-id"  # never a real production ID
FAKE_PROFILE = FitProfile(
    model_version="test-1",
    role_families=(RoleFamily(patterns=(r"\btechnical program manager\b",), base_score=86, label="TPM"),),
    default_role_base=10,
    default_role_label="weak",
)


def _job(**overrides) -> Job:
    base = dict(
        company=Company(name="Acme Synthetic Co"),
        role="Synthetic Engineer",
        location="Remote - Synthetic Country",
        work_mode=WorkMode.REMOTE,
        compensation_text="$100,000 - $120,000",
        compensation_minimum=100_000,
        posting_date=RUN_DATE,
        apply_url="https://greenhouse.io/acme/jobs/42",
        source_lane="Newsletter",
        provider_job_id="prov-1",
        description_text="Synthetic description.",
    )
    base.update(overrides)
    return JobObservation(**base)


class FakeNotionHttp:
    """Duck-typed HttpClient replacement (only request_json is used by
    NotionTransport). Simulates one in-memory Notion data source -- no
    network, no real Notion IDs."""

    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self._next_id = 1
        self.query_calls: list[dict] = []

    def request_json(self, context, method, url, *, headers=None, json_body=None, **kwargs):
        if method == "POST" and url.endswith("/query"):
            self.query_calls.append(json_body)
            matched = self._match(json_body["filter"])
            return {"results": matched, "has_more": False}
        if method == "POST" and url.endswith("/pages"):
            page_id = f"page-{self._next_id}"
            self._next_id += 1
            page = {"id": page_id, "properties": json_body["properties"]}
            self.pages[page_id] = page
            return page
        if method == "PATCH" and "/pages/" in url:
            page_id = url.rsplit("/", 1)[-1]
            page = self.pages[page_id]
            page["properties"].update(json_body["properties"])
            return page
        if method == "GET" and "/pages/" in url:
            page_id = url.rsplit("/", 1)[-1]
            return self.pages[page_id]
        raise AssertionError(f"unexpected Notion call {method} {url}")

    def _match(self, filter_payload) -> list[dict]:
        filters = filter_payload["or"] if "or" in filter_payload else [filter_payload]
        rich_text_wanted = {f["rich_text"]["equals"] for f in filters if "rich_text" in f}
        url_wanted = {f["url"]["equals"] for f in filters if "url" in f}
        return [
            p for p in self.pages.values()
            if _plain(p["properties"].get("Stable Job Key")) in rich_text_wanted
            or _url(p["properties"].get("Apply URL")) in url_wanted
        ]


class EmptyFetcher:
    def get(self, url: str) -> FetchResponse:
        raise RuntimeError(f"no fixture for {url}")


class StaticFetcher:
    def __init__(self, *, url: str, final_url: str) -> None:
        self._url = url
        self._final_url = final_url

    def get(self, url: str) -> FetchResponse:
        if url != self._url:
            raise RuntimeError(f"no fixture for {url}")
        return FetchResponse(
            final_url=self._final_url,
            body='<html><script type="application/ld+json">{"@type":"JobPosting","description":"Synthetic terminal text.","datePosted":"2026-01-10"}</script></html>',
        )


def _plain(prop):
    if not isinstance(prop, dict):
        return ""
    items = prop.get("rich_text") or []
    return "".join(i.get("text", {}).get("content", "") for i in items)


def _url(prop):
    return prop.get("url") if isinstance(prop, dict) else None


def _multi_select_names(prop):
    if not isinstance(prop, dict):
        return ()
    return tuple(item["name"] for item in prop.get("multi_select", ()))


def _repository():
    http = FakeNotionHttp()
    return _repository_with_http(http), http


def _repository_with_http(http: FakeNotionHttp):
    context = RunContext.start(timeout_seconds=45.0, now=datetime.now(timezone.utc))
    transport = NotionTransport(context=context, http=http, access_token="synthetic-token")
    repo = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig(data_source_id=SYNTHETIC_DATA_SOURCE_ID))
    return repo


def _fresh_ingest(
    http: FakeNotionHttp,
    candidate,
    *,
    run_date: date = RUN_DATE,
    lane=REMOTE_LANE,
    lane_priority: dict[str, int] | None = None,
):
    repo = _repository_with_http(http)
    assert repo._page_ids == {}
    result = ingest(
        [candidate],
        lane=lane,
        lane_priority=lane_priority or {lane.name: 0},
        repository=repo,
        run_date=run_date,
    )
    return result, repo


def _fresh_record(http: FakeNotionHttp, key: str):
    repo = _repository_with_http(http)
    assert repo._page_ids == {}
    return repo.get_many([key])[key]


def _newsletter_candidate(*, provider: str, mailbox: str, apply_url: str | None, evidence_ref: str):
    fetcher = EmptyFetcher() if apply_url is None else StaticFetcher(url=apply_url, final_url=apply_url)
    adapter = NewsletterJobsAdapter(
        NewsletterAdapterConfig(
            fetcher=fetcher,
            fit_profile=FAKE_PROFILE,
            market="Synthetic-US",
            source_lane="Synthetic-Remote",
        )
    )
    return adapter.to_jobs_candidate(
        SourceVacancyObservation(
            evidence_ref=evidence_ref,
            source_provider=provider,
            source_mailbox=mailbox,
            source_message_id=evidence_ref,
            source_subject="Synthetic jobs",
            company="Acme Synthetic Co",
            role="Technical Program Manager",
            location_text="Remote",
            compensation_text="$100,000 - $120,000",
            source_apply_url=apply_url,
            provider_job_id=None,
            provider_score=None,
            source_received_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        )
    )


def test_upsert_then_get_many_round_trips():
    repo, http = _repository()
    job = Job(stable_job_key="url:https://greenhouse.io/acme/jobs/42", job=_job(), admission_status=AdmissionStatus.ADMITTED)
    record = new_record(job, run_date=RUN_DATE)
    persisted = repo.upsert(record)
    assert persisted.job.stable_job_key == job.stable_job_key

    found = repo.get_many([job.stable_job_key])
    assert job.stable_job_key in found
    assert found[job.stable_job_key].job.job.role == "Synthetic Engineer"


def test_second_upsert_updates_same_page_not_a_new_one():
    repo, http = _repository()
    job = Job(stable_job_key="k1", job=_job(), admission_status=AdmissionStatus.ADMITTED)
    record = new_record(job, run_date=RUN_DATE)
    repo.upsert(record)
    assert len(http.pages) == 1

    updated_job = _job(compensation_text="$130,000+")
    updated_opportunity = Job(stable_job_key="k1", job=updated_job, admission_status=AdmissionStatus.ADMITTED)
    from lifeos.jobs.lifecycle import apply_observation

    existing = repo.get_many(["k1"])["k1"]
    merged = apply_observation(existing, updated_opportunity, run_date=RUN_DATE)
    repo.upsert(merged)
    assert len(http.pages) == 1  # still one page -- update, not a second create


def test_get_many_issues_exactly_one_query_call_never_a_full_scan():
    repo, http = _repository()
    for i in range(5):
        job = Job(stable_job_key=f"k{i}", job=_job(), admission_status=AdmissionStatus.ADMITTED)
        repo.upsert(new_record(job, run_date=RUN_DATE))

    http.query_calls.clear()
    repo.get_many(["k2", "k4"])
    assert len(http.query_calls) == 1
    filter_payload = http.query_calls[0]["filter"]
    queried_values = {f["rich_text"]["equals"] for f in filter_payload["or"]}
    assert queried_values == {"k2", "k4"}


def test_get_by_apply_urls_uses_bounded_url_property_query():
    repo, http = _repository()
    job = Job(
        stable_job_key="acme|synthetic engineer|remote",
        job=_job(apply_url="https://greenhouse.io/acme/jobs/42"),
        admission_status=AdmissionStatus.ADMITTED,
    )
    repo.upsert(new_record(job, run_date=RUN_DATE))

    http.query_calls.clear()
    found = repo.get_by_apply_urls(["https://greenhouse.io/acme/jobs/42"])

    assert set(found) == {"https://greenhouse.io/acme/jobs/42"}
    assert found["https://greenhouse.io/acme/jobs/42"].job.stable_job_key == "acme|synthetic engineer|remote"
    assert len(http.query_calls) == 1
    filter_payload = http.query_calls[0]["filter"]
    assert filter_payload["property"] == "Apply URL"
    assert filter_payload["url"]["equals"] == "https://greenhouse.io/acme/jobs/42"


def test_fallback_key_job_later_url_converges_across_fresh_repository_instances():
    http = FakeNotionHttp()
    lane_priority = {"Synthetic-Remote": 0}
    fallback_key = "acme synthetic co|technical program manager|remote"

    repo_1 = _repository_with_http(http)
    assert repo_1._page_ids == {}
    first = ingest(
        [
            make_candidate(
                job=make_job(role="Technical Program Manager", location="Remote", apply_url=None),
                fit=None,
                evidence_ref="ev:first",
            )
        ],
        lane=REMOTE_LANE,
        lane_priority=lane_priority,
        repository=repo_1,
        run_date=RUN_DATE,
    )
    assert first[0].disposition == Disposition.CREATED
    assert first[0].stable_job_key == fallback_key
    assert len(http.pages) == 1

    repo_2 = _repository_with_http(http)
    assert repo_2._page_ids == {}
    second = ingest(
        [
            make_candidate(
                job=make_job(
                    role="Technical Program Manager",
                    location="Remote",
                    apply_url="https://greenhouse.io/acme/jobs/123",
                ),
                evidence_ref="ev:second",
            )
        ],
        lane=REMOTE_LANE,
        lane_priority=lane_priority,
        repository=repo_2,
        run_date=RUN_DATE,
    )
    assert second[0].disposition == Disposition.UPDATED
    assert second[0].stable_job_key == fallback_key
    assert len(http.pages) == 1
    persisted = repo_2.get_many([fallback_key])[fallback_key]
    assert persisted.job.job.apply_url == "https://greenhouse.io/acme/jobs/123"

    repo_3 = _repository_with_http(http)
    assert repo_3._page_ids == {}
    third = ingest(
        [
            make_candidate(
                job=make_job(
                    company_name="Acme Inc",
                    role="TPM",
                    location="Remote US",
                    apply_url="https://greenhouse.io/acme/jobs/123",
                ),
                evidence_ref="ev:third",
            )
        ],
        lane=REMOTE_LANE,
        lane_priority=lane_priority,
        repository=repo_3,
        run_date=RUN_DATE,
    )
    assert third[0].disposition == Disposition.UPDATED
    assert third[0].stable_job_key == fallback_key
    assert len(http.pages) == 1
    assert repo_3.get_many(["url:https://greenhouse.io/acme/jobs/123"]) == {}


def test_source_types_round_trip_acquisition_provenance_across_fresh_repository_instances():
    http = FakeNotionHttp()
    key = "acme synthetic co|technical program manager|remote"
    us_remote_lane = replace(REMOTE_LANE, name="US Remote")

    first, _ = _fresh_ingest(
        http,
        _newsletter_candidate(
            provider="LinkedIn Jobs",
            mailbox="gmail-primary",
            apply_url=None,
            evidence_ref="ev:linkedin",
        ),
        lane=us_remote_lane,
        lane_priority={"US Remote": 1},
    )
    assert first[0].disposition == Disposition.CREATED
    assert first[0].stable_job_key == key
    assert _fresh_record(http, key).job.eligible_lanes == ("US Remote",)
    page = next(iter(http.pages.values()))
    assert page["properties"]["Visible Lane"]["select"]["name"] == "US Remote"
    page["properties"]["Applied"] = {"checkbox": True}
    page["properties"]["Applied On"] = {"date": {"start": "2026-01-12"}}

    first_record = _fresh_record(http, key)
    assert first_record.job.source_providers == ("LinkedIn Jobs",)
    assert set(first_record.job.source_types) == {"LinkedIn Jobs", "Gmail Alert"}
    page = next(iter(http.pages.values()))
    assert set(_multi_select_names(page["properties"]["Source Types"])) == {"LinkedIn Jobs", "Gmail Alert"}
    assert "Synthetic-Remote" not in _multi_select_names(page["properties"]["Source Types"])
    assert "Newsletter" not in _multi_select_names(page["properties"]["Source Types"])

    scale_up_lane = replace(REMOTE_LANE, name="Scale-Up")
    second, repo_2 = _fresh_ingest(
        http,
        _newsletter_candidate(
            provider="Lensa",
            mailbox="gmail-primary",
            apply_url="https://greenhouse.io/acme/jobs/123",
            evidence_ref="ev:lensa",
        ),
        run_date=RUN_DATE + timedelta(days=1),
        lane=scale_up_lane,
        lane_priority={"Scale-Up": 0, "US Remote": 1},
    )

    assert second[0].disposition == Disposition.UPDATED
    assert second[0].stable_job_key == key
    assert len(http.pages) == 1
    assert repo_2.get_many(["url:https://greenhouse.io/acme/jobs/123"]) == {}

    record = _fresh_record(http, key)
    assert record.job.eligible_lanes == ("Scale-Up", "US Remote")
    assert record.job.job.apply_url == "https://greenhouse.io/acme/jobs/123"
    assert record.job.source_providers == ("Lensa", "LinkedIn Jobs")
    assert set(record.job.source_types) == {"LinkedIn Jobs", "Lensa", "Gmail Alert"}
    page = next(iter(http.pages.values()))
    persisted_source_types = set(_multi_select_names(page["properties"]["Source Types"]))
    assert persisted_source_types == {"LinkedIn Jobs", "Lensa", "Gmail Alert"}
    assert set(_multi_select_names(page["properties"]["Eligible Lanes"])) == {"Scale-Up", "US Remote"}
    assert page["properties"]["Visible Lane"]["select"]["name"] == "Scale-up"
    assert page["properties"]["Applied"]["checkbox"] is True
    assert page["properties"]["Applied On"]["date"]["start"] == "2026-01-12"
    assert "Primary Lane" not in page["properties"]
    assert "Synthetic-Remote" not in persisted_source_types
    assert "Newsletter" not in persisted_source_types

    excluded, _ = _fresh_ingest(
        http,
        make_candidate(
            job=make_job(work_mode=WorkMode.ONSITE, source_provider="Scale-Up"),
            fit=86,
            evidence_ref="ev:excluded",
        ),
        run_date=RUN_DATE + timedelta(days=2),
        lane=scale_up_lane,
        lane_priority={"Scale-Up": 0, "US Remote": 1},
    )
    assert excluded[0].disposition == Disposition.EXCLUDED
    assert _fresh_record(http, key).job.eligible_lanes == ("Scale-Up", "US Remote")

    reverse_http = FakeNotionHttp()
    first_reverse, _ = _fresh_ingest(
        reverse_http,
        _newsletter_candidate(
            provider="Lensa",
            mailbox="gmail-primary",
            apply_url=None,
            evidence_ref="ev:scale-first",
        ),
        lane=scale_up_lane,
        lane_priority={"Scale-Up": 0, "US Remote": 1},
    )
    second_reverse, _ = _fresh_ingest(
        reverse_http,
        _newsletter_candidate(
            provider="LinkedIn Jobs",
            mailbox="gmail-primary",
            apply_url="https://greenhouse.io/acme/jobs/123",
            evidence_ref="ev:remote-second",
        ),
        run_date=RUN_DATE + timedelta(days=1),
        lane=us_remote_lane,
        lane_priority={"Scale-Up": 0, "US Remote": 1},
    )
    assert first_reverse[0].disposition == Disposition.CREATED
    assert second_reverse[0].disposition == Disposition.UPDATED
    reverse_page = next(iter(reverse_http.pages.values()))
    assert set(_multi_select_names(reverse_page["properties"]["Eligible Lanes"])) == {"Scale-Up", "US Remote"}
    assert reverse_page["properties"]["Visible Lane"]["select"]["name"] == "Scale-up"


def test_package_c_no_downgrade_merge_survives_fresh_repository_instances():
    http = FakeNotionHttp()
    key = "acme synthetic co|technical program manager|remote"
    first_date = RUN_DATE
    second_date = RUN_DATE + timedelta(days=1)

    first, _ = _fresh_ingest(
        http,
        make_candidate(
            job=make_job(
                role="Technical Program Manager",
                location="Remote",
                apply_url=None,
                posting_date=None,
                source_provider="Source A",
            ),
            fit=None,
            fit_authority=FitAuthority.NON_AUTHORITATIVE,
            evidence_ref="ev:first",
        ),
        run_date=first_date,
    )
    assert first[0].disposition == Disposition.CREATED
    assert first[0].stable_job_key == key

    stronger, _ = _fresh_ingest(
        http,
        make_candidate(
            job=make_job(
                role="Technical Program Manager",
                location="Remote",
                apply_url="https://greenhouse.io/acme/jobs/123",
                posting_date=date(2026, 1, 10),
                source_provider="Source A",
            ),
            fit=86,
            fit_authority=FitAuthority.AUTHORITATIVE,
            evidence_ref="ev:stronger",
        ),
        run_date=second_date,
    )
    assert stronger[0].disposition == Disposition.UPDATED

    page = next(iter(http.pages.values()))
    del page["properties"]["Fit Authority"]
    legacy = _fresh_record(http, key)
    assert legacy.job.fit == 86
    assert legacy.job.fit_authority == FitAuthority.AUTHORITATIVE
    page["properties"]["Saturn Decision"] = {"select": {"name": "Synthetic Hold"}}
    page["properties"]["Decision On"] = {"date": {"start": "2026-01-12"}}
    page["properties"]["Saturn Ready"] = {"date": {"start": "2026-01-13"}}

    third_date = RUN_DATE + timedelta(days=2)
    weak, _ = _fresh_ingest(
        http,
        make_candidate(
            job=make_job(
                role="Technical Program Manager",
                location="Remote",
                apply_url=None,
                posting_date=None,
                compensation_text=None,
                compensation_minimum=None,
                source_provider="Source B",
            ),
            fit=None,
            fit_authority=FitAuthority.NON_AUTHORITATIVE,
            evidence_ref="ev:weak",
        ),
        run_date=third_date,
    )
    assert weak[0].disposition == Disposition.UPDATED

    record = _fresh_record(http, key)
    assert len(http.pages) == 1
    assert record.job.job.apply_url == "https://greenhouse.io/acme/jobs/123"
    assert record.job.job.posting_date == date(2026, 1, 10)
    assert record.job.job.compensation_text == "$100,000 - $120,000"
    assert record.job.fit == 86
    assert record.job.fit_authority == FitAuthority.AUTHORITATIVE
    assert record.job.source_providers == ("Source A", "Source B")
    assert record.first_surfaced == first_date
    assert record.last_seen == third_date
    assert record.job.admission_status == AdmissionStatus.ADMITTED
    assert page["properties"]["Saturn Decision"]["select"]["name"] == "Synthetic Hold"
    assert page["properties"]["Decision On"]["date"]["start"] == "2026-01-12"
    assert page["properties"]["Saturn Ready"]["date"]["start"] == "2026-01-13"


def test_package_c_missing_then_stronger_evidence_fills_canonical_row():
    http = FakeNotionHttp()
    key = "acme synthetic co|technical program manager|remote"
    first, _ = _fresh_ingest(
        http,
        make_candidate(
            job=make_job(role="Technical Program Manager", location="Remote", apply_url=None, posting_date=None, source_provider="Source A"),
            fit=None,
            fit_authority=FitAuthority.NON_AUTHORITATIVE,
            evidence_ref="ev:first",
        ),
    )
    assert first[0].stable_job_key == key

    second, _ = _fresh_ingest(
        http,
        make_candidate(
            job=make_job(
                role="Technical Program Manager",
                location="Remote",
                apply_url="https://greenhouse.io/acme/jobs/123",
                posting_date=date(2026, 1, 10),
                source_provider="Source B",
            ),
            fit=86,
            fit_authority=FitAuthority.AUTHORITATIVE,
            evidence_ref="ev:stronger",
        ),
        run_date=RUN_DATE + timedelta(days=1),
    )
    assert second[0].disposition == Disposition.UPDATED
    record = _fresh_record(http, key)
    assert record.job.job.apply_url == "https://greenhouse.io/acme/jobs/123"
    assert record.job.job.posting_date == date(2026, 1, 10)
    assert record.job.fit == 86
    assert record.job.fit_authority == FitAuthority.AUTHORITATIVE
    assert record.job.source_providers == ("Source A", "Source B")


def test_read_back_mismatch_raised_when_persisted_page_diverges():
    repo, http = _repository()
    job = Job(stable_job_key="k1", job=_job(), admission_status=AdmissionStatus.ADMITTED)
    record = new_record(job, run_date=RUN_DATE)

    original_get_page = repo._transport.get_page

    def corrupting_get_page(page_id):
        page = original_get_page(page_id)
        page = dict(page)
        page["properties"] = dict(page["properties"])
        page["properties"]["Role"] = {"title": [{"type": "text", "text": {"content": "Corrupted Role"}}]}
        return page

    repo._transport.get_page = corrupting_get_page  # simulate a write that didn't actually take
    with pytest.raises(ReadBackMismatch):
        repo.upsert(record)


def test_human_owned_state_preserved_across_reupsert():
    """Jobs writes omit pursuit fields while preserving existing Notion values."""
    http = FakeNotionHttp()
    key = "k1"
    repo = _repository_with_http(http)
    job = Job(stable_job_key=key, job=_job(), admission_status=AdmissionStatus.ADMITTED)
    repo.upsert(new_record(job, run_date=RUN_DATE))
    page = next(iter(http.pages.values()))
    page["properties"]["Applied"] = {"checkbox": True}
    page["properties"]["Applied On"] = {"date": {"start": "2026-01-12"}}

    refreshed, _ = _fresh_ingest(
        http,
        make_candidate(
            job=make_job(
                compensation_text="$999,000",
                apply_url="https://greenhouse.io/acme/jobs/42",
            ),
            evidence_ref="ev:refresh",
        ),
        run_date=RUN_DATE + timedelta(days=1),
    )
    assert refreshed[0].disposition == Disposition.UPDATED
    assert page["properties"]["Applied"]["checkbox"] is True
    assert page["properties"]["Applied On"]["date"]["start"] == "2026-01-12"
    assert page["properties"]["Compensation"]["rich_text"][0]["text"]["content"] == "$999,000"
    record = _fresh_record(http, key)
    assert not hasattr(record, "applied")
    assert not hasattr(record, "applied_on")
