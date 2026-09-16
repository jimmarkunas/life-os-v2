from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from lifeos.core.runtime import RunContext
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.lifecycle import new_record
from lifeos.jobs.models import AdmissionStatus, Company, Job, Opportunity, WorkMode
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.repository import ReadBackMismatch

RUN_DATE = date(2026, 1, 15)
SYNTHETIC_DATA_SOURCE_ID = "synthetic-data-source-id"  # never a real production ID


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
    return Job(**base)


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
        wanted = {f["rich_text"]["equals"] for f in filters}
        return [p for p in self.pages.values() if _plain(p["properties"].get("Stable Job Key")) in wanted]


def _plain(prop):
    if not isinstance(prop, dict):
        return ""
    items = prop.get("rich_text") or []
    return "".join(i.get("text", {}).get("content", "") for i in items)


def _repository():
    http = FakeNotionHttp()
    context = RunContext.start(timeout_seconds=45.0, now=datetime.now(timezone.utc))
    transport = NotionTransport(context=context, http=http, access_token="synthetic-token")
    repo = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig(data_source_id=SYNTHETIC_DATA_SOURCE_ID))
    return repo, http


def test_get_many_returns_empty_for_unknown_keys():
    repo, http = _repository()
    assert repo.get_many(["nonexistent"]) == {}


def test_upsert_then_get_many_round_trips():
    repo, http = _repository()
    opportunity = Opportunity(stable_job_key="url:https://greenhouse.io/acme/jobs/42", job=_job(), admission_status=AdmissionStatus.ADMITTED)
    record = new_record(opportunity, run_date=RUN_DATE)
    persisted = repo.upsert(record)
    assert persisted.opportunity.stable_job_key == opportunity.stable_job_key

    found = repo.get_many([opportunity.stable_job_key])
    assert opportunity.stable_job_key in found
    assert found[opportunity.stable_job_key].opportunity.job.role == "Synthetic Engineer"


def test_second_upsert_updates_same_page_not_a_new_one():
    repo, http = _repository()
    opportunity = Opportunity(stable_job_key="k1", job=_job(), admission_status=AdmissionStatus.ADMITTED)
    record = new_record(opportunity, run_date=RUN_DATE)
    repo.upsert(record)
    assert len(http.pages) == 1

    updated_job = _job(compensation_text="$130,000+")
    updated_opportunity = Opportunity(stable_job_key="k1", job=updated_job, admission_status=AdmissionStatus.ADMITTED)
    from lifeos.jobs.lifecycle import apply_observation

    existing = repo.get_many(["k1"])["k1"]
    merged = apply_observation(existing, updated_opportunity, run_date=RUN_DATE)
    repo.upsert(merged)
    assert len(http.pages) == 1  # still one page -- update, not a second create


def test_get_many_issues_exactly_one_query_call_never_a_full_scan():
    repo, http = _repository()
    for i in range(5):
        opportunity = Opportunity(stable_job_key=f"k{i}", job=_job(), admission_status=AdmissionStatus.ADMITTED)
        repo.upsert(new_record(opportunity, run_date=RUN_DATE))

    http.query_calls.clear()
    repo.get_many(["k2", "k4"])
    assert len(http.query_calls) == 1
    filter_payload = http.query_calls[0]["filter"]
    queried_values = {f["rich_text"]["equals"] for f in filter_payload["or"]}
    assert queried_values == {"k2", "k4"}


def test_read_back_mismatch_raised_when_persisted_page_diverges():
    repo, http = _repository()
    opportunity = Opportunity(stable_job_key="k1", job=_job(), admission_status=AdmissionStatus.ADMITTED)
    record = new_record(opportunity, run_date=RUN_DATE)

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
    """applied/applied_on must survive a second upsert reflecting only new
    source evidence -- the repository itself must not need special-casing
    for this since lifecycle.apply_observation already guarantees it; this
    test proves the whole path (get_many -> apply_observation -> upsert)
    together."""
    repo, http = _repository()
    opportunity = Opportunity(stable_job_key="k1", job=_job(), admission_status=AdmissionStatus.ADMITTED)
    record = new_record(opportunity, run_date=RUN_DATE)
    repo.upsert(record)

    from lifeos.jobs.lifecycle import mark_applied

    existing = repo.get_many(["k1"])["k1"]
    applied = mark_applied(existing, run_date=RUN_DATE)
    repo.upsert(applied)

    from lifeos.jobs.lifecycle import apply_observation

    existing_after_apply = repo.get_many(["k1"])["k1"]
    assert existing_after_apply.applied is True

    reobserved_opportunity = Opportunity(stable_job_key="k1", job=_job(compensation_text="$999,000"), admission_status=AdmissionStatus.ADMITTED)
    merged = apply_observation(existing_after_apply, reobserved_opportunity, run_date=RUN_DATE)
    persisted = repo.upsert(merged)
    assert persisted.applied is True
    assert persisted.applied_on == RUN_DATE
