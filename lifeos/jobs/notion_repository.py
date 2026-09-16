"""Jobs-owned concrete CareerRepository backed by the canonical Notion Job
Ledger.

MIGRATE/REFACTOR from v1 `jobs/notion_repository.py`: the property-mapping
shape (dataclass <-> Notion property JSON) is the proven, reused part.
RETIRED, not ported: v1's `index()` (full-database query on every run --
exactly what CareerRepository.get_many's narrow-lookup contract replaces),
its own urllib transport/retry loop (Platform Core's `lifeos.integrations
.notion.NotionTransport` + `lifeos.core.http.HttpClient` own that now), and
its hardcoded production data-source-ID constant (private runtime
configuration only -- see `data_source_id` below, never a module constant).

get_many() issues exactly one identity-filtered Notion query (values
bounded by NotionIdentityQuery's own 50-value cap) -- never a full scan.
upsert() creates-or-updates exactly one page, then performs an authoritative
get_page() read-back and raises ReadBackMismatch on any divergence, per the
CareerRepository Protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from lifeos.integrations.notion import NotionIdentityQuery, NotionTransport
from lifeos.jobs.lifecycle import LifecycleRecord, LifecycleStatus
from lifeos.jobs.models import AdmissionStatus, Company, FreshnessStatus, Job, Opportunity, WorkMode
from lifeos.jobs.repository import ReadBackMismatch

STABLE_KEY_PROPERTY = "Stable Job Key"
_LIST_SEPARATOR = "|"
MAX_IDENTITY_VALUES_PER_QUERY = 50  # matches NotionIdentityQuery's own cap


def _chunk(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _rich_text(value: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": value}}] if value else []}


def _title(value: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": value}}] if value else []}


def _url(value: str | None) -> dict[str, Any]:
    return {"url": value}


def _select(value: str) -> dict[str, Any]:
    return {"select": {"name": value}}


def _date(value: date | None) -> dict[str, Any]:
    return {"date": {"start": value.isoformat()} if value else None}


def _checkbox(value: bool) -> dict[str, Any]:
    return {"checkbox": value}


def _number(value: int | None) -> dict[str, Any]:
    return {"number": value}


def _extract_number(prop: Any) -> int | None:
    if not isinstance(prop, dict):
        return None
    value = prop.get("number")
    return int(value) if value is not None else None


def _plain_text(prop: Any) -> str:
    if not isinstance(prop, dict):
        return ""
    items = prop.get("rich_text") or prop.get("title") or []
    return "".join(str(item.get("plain_text") or item.get("text", {}).get("content") or "") for item in items if isinstance(item, dict))


def _extract_url(prop: Any) -> str | None:
    if not isinstance(prop, dict):
        return None
    return prop.get("url")


def _extract_select(prop: Any) -> str | None:
    if not isinstance(prop, dict):
        return None
    value = prop.get("select")
    return value.get("name") if isinstance(value, dict) else None


def _extract_date(prop: Any) -> date | None:
    if not isinstance(prop, dict):
        return None
    value = prop.get("date")
    if not isinstance(value, dict) or not value.get("start"):
        return None
    return date.fromisoformat(str(value["start"])[:10])


def _extract_checkbox(prop: Any) -> bool:
    return bool(isinstance(prop, dict) and prop.get("checkbox"))


def _record_to_properties(record: LifecycleRecord) -> dict[str, Any]:
    """Canonical v1 production Job Ledger property names/types (see
    jimmarkunas/life-os-automation jobs/notion_repository.py), reused
    rather than invented. "Job" is the title property (company + role),
    never bare "Role". Fields with no v1 canonical equivalent (this
    domain's new lifecycle-state shape) keep descriptive v2-only names,
    clearly additive rather than colliding with/renaming a canonical field."""
    job = record.opportunity.job
    fit = record.opportunity.fit
    return {
        "Job": _title(f"{job.company.name} — {job.role}" if job.company.name or job.role else ""),
        STABLE_KEY_PROPERTY: _rich_text(record.opportunity.stable_job_key),
        "Company": _rich_text(job.company.name),
        "Role": _rich_text(job.role),
        "Location / Work Mode": _rich_text(job.location or ""),
        "Work Mode": _select(job.work_mode.value),
        "Compensation": _rich_text(job.compensation_text or ""),
        "Compensation Minimum": _number(job.compensation_minimum),
        "Apply URL": _url(job.apply_url),
        "Posting Date": _date(job.posting_date),
        "LIFE OS Fit": _number(fit),
        "Fit Authority": _select("Authoritative" if fit is not None else "Non-Authoritative"),
        "Provider Score": _number(job.provider_score),
        "Source Lane": _rich_text(job.source_lane),
        "Provider Job ID": _rich_text(job.provider_job_id or ""),
        "Description": _rich_text((job.description_text or "")[:2000]),
        "Admission Status": _select(record.opportunity.admission_status.value),
        "Source Lanes": _rich_text(_LIST_SEPARATOR.join(record.opportunity.source_lanes)),
        "Aliases": _rich_text(_LIST_SEPARATOR.join(record.opportunity.aliases)),
        "Lifecycle Status": _select(record.status.value),
        "Applied": _checkbox(record.applied),
        "Applied On": _date(record.applied_on),
        "First Surfaced": _date(record.first_surfaced),
        "Review Ready On": _date(record.review_ready_on),
        "Last Seen": _date(record.last_seen),
        "Live": _checkbox(record.live),
    }


def _page_to_record(page: dict[str, Any]) -> LifecycleRecord:
    props = page.get("properties") or {}
    stable_job_key = _plain_text(props.get(STABLE_KEY_PROPERTY))
    if not stable_job_key:
        raise ReadBackMismatch(f"persisted page {page.get('id')} is missing {STABLE_KEY_PROPERTY!r}")

    work_mode_value = _extract_select(props.get("Work Mode")) or WorkMode.UNKNOWN.value
    job = Job(
        company=Company(name=_plain_text(props.get("Company"))),
        role=_plain_text(props.get("Role")),
        location=_plain_text(props.get("Location / Work Mode")) or None,
        work_mode=WorkMode(work_mode_value),
        compensation_text=_plain_text(props.get("Compensation")) or None,
        compensation_minimum=_extract_number(props.get("Compensation Minimum")),
        posting_date=_extract_date(props.get("Posting Date")),
        apply_url=_extract_url(props.get("Apply URL")),
        source_lane=_plain_text(props.get("Source Lane")),
        provider_job_id=_plain_text(props.get("Provider Job ID")) or None,
        description_text=_plain_text(props.get("Description")) or None,
        provider_score=_extract_number(props.get("Provider Score")),
    )
    admission_value = _extract_select(props.get("Admission Status")) or AdmissionStatus.PASSED_REVIEW.value
    source_lanes_raw = _plain_text(props.get("Source Lanes"))
    aliases_raw = _plain_text(props.get("Aliases"))
    opportunity = Opportunity(
        stable_job_key=stable_job_key,
        job=job,
        admission_status=AdmissionStatus(admission_value),
        source_lanes=tuple(x for x in source_lanes_raw.split(_LIST_SEPARATOR) if x),
        aliases=tuple(x for x in aliases_raw.split(_LIST_SEPARATOR) if x),
        fit=_extract_number(props.get("LIFE OS Fit")),
    )

    status_value = _extract_select(props.get("Lifecycle Status")) or LifecycleStatus.NEW.value
    first_surfaced = _extract_date(props.get("First Surfaced"))
    review_ready_on = _extract_date(props.get("Review Ready On"))
    last_seen = _extract_date(props.get("Last Seen"))
    if first_surfaced is None or review_ready_on is None or last_seen is None:
        raise ReadBackMismatch(f"persisted page {page.get('id')} is missing required lifecycle dates")

    return LifecycleRecord(
        opportunity=opportunity,
        status=LifecycleStatus(status_value),
        applied=_extract_checkbox(props.get("Applied")),
        applied_on=_extract_date(props.get("Applied On")),
        first_surfaced=first_surfaced,
        review_ready_on=review_ready_on,
        last_seen=last_seen,
        live=_extract_checkbox(props.get("Live")),
    )


@dataclass(frozen=True)
class NotionCareerRepositoryConfig:
    """Private runtime configuration -- never a source constant. The real
    data_source_id is injected at trusted runtime, exactly like
    NotionTransport's access token."""

    data_source_id: str


class NotionCareerRepository:
    """Concrete CareerRepository. One instance is scoped to one bounded
    feature execution; its page-ID cache exists only to avoid a second
    lookup between get_many() and upsert() within that single run -- it is
    not a second persistence layer and holds no data across runs."""

    def __init__(self, *, transport: NotionTransport, config: NotionCareerRepositoryConfig) -> None:
        self._transport = transport
        self._config = config
        self._page_ids: dict[str, str] = {}

    def get_many(self, stable_job_keys: list[str]) -> dict[str, LifecycleRecord]:
        if not stable_job_keys:
            return {}
        # NotionIdentityQuery caps a single filter at _MAX_IDENTITY_VALUES
        # (50). Chunk into bounded batches -- never a full-ledger scan, just
        # multiple narrow identity-filtered queries for the same requested set.
        found: dict[str, LifecycleRecord] = {}
        for chunk in _chunk(stable_job_keys, MAX_IDENTITY_VALUES_PER_QUERY):
            query = NotionIdentityQuery(
                property_name=STABLE_KEY_PROPERTY,
                property_type="rich_text",
                values=tuple(chunk),
            )
            pages = self._transport.query_data_source(self._config.data_source_id, query)
            for page in pages:
                record = _page_to_record(page)
                key = record.opportunity.stable_job_key
                found[key] = record
                page_id = page.get("id")
                if page_id:
                    self._page_ids[key] = str(page_id)
        return found

    def upsert(self, record: LifecycleRecord) -> LifecycleRecord:
        key = record.opportunity.stable_job_key
        properties = _record_to_properties(record)
        existing_page_id = self._page_ids.get(key)

        if existing_page_id:
            written = self._transport.update_page(existing_page_id, properties)
            page_id = str(written.get("id") or existing_page_id)
        else:
            written = self._transport.create_page(self._config.data_source_id, properties)
            page_id = written.get("id")
            if not page_id:
                raise ReadBackMismatch(f"Notion create response for {key} did not return a page id")
            page_id = str(page_id)

        self._page_ids[key] = page_id

        read_back_page = self._transport.get_page(page_id)
        persisted = _page_to_record(read_back_page)
        if persisted != record:
            raise ReadBackMismatch(f"read-back mismatch for {key}")
        return persisted
