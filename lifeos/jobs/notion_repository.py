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
from datetime import date, timedelta
from typing import Any

from lifeos.integrations.notion import NotionIdentityQuery, NotionTransport
from lifeos.jobs.identity import canonical_url
from lifeos.jobs.lifecycle import JobLedgerRecord, LifecycleStatus
from lifeos.jobs.models import AdmissionStatus, Company, FitAuthority, Job, JobObservation, WorkMode
from lifeos.jobs.repository import ReadBackMismatch

STABLE_KEY_PROPERTY = "Stable Job Key"
MAX_IDENTITY_VALUES_PER_QUERY = 50  # matches NotionIdentityQuery's own cap

# Canonical Notion option labels -- the live Job Ledger schema uses these
# exact strings, never the internal lowercase enum wire values.
_WORK_MODE_TO_CANONICAL = {
    WorkMode.REMOTE: "Remote",
    WorkMode.HYBRID: "Hybrid",
    WorkMode.ONSITE: "Onsite",
    WorkMode.UNKNOWN: "Unknown",
}
_WORK_MODE_FROM_CANONICAL = {v: k for k, v in _WORK_MODE_TO_CANONICAL.items()}

_ADMISSION_TO_CANONICAL = {
    AdmissionStatus.ADMITTED: "Admitted",
    AdmissionStatus.PASSED_REVIEW: "Passed / Review",
    AdmissionStatus.EXCLUDED: "Excluded",
}
_ADMISSION_FROM_CANONICAL = {v: k for k, v in _ADMISSION_TO_CANONICAL.items()}
_FIT_AUTHORITY_TO_CANONICAL = {
    FitAuthority.AUTHORITATIVE: "Authoritative",
    FitAuthority.NON_AUTHORITATIVE: "Non-Authoritative",
}
_FIT_AUTHORITY_FROM_CANONICAL = {v: k for k, v in _FIT_AUTHORITY_TO_CANONICAL.items()}


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


def _multi_select(values: tuple[str, ...]) -> dict[str, Any]:
    return {"multi_select": [{"name": value} for value in values]}


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


def _extract_multi_select(prop: Any) -> tuple[str, ...]:
    if not isinstance(prop, dict):
        return ()
    items = prop.get("multi_select") or []
    return tuple(str(item.get("name")) for item in items if isinstance(item, dict) and item.get("name"))


def _extract_date(prop: Any) -> date | None:
    if not isinstance(prop, dict):
        return None
    value = prop.get("date")
    if not isinstance(value, dict) or not value.get("start"):
        return None
    return date.fromisoformat(str(value["start"])[:10])


def _extract_checkbox(prop: Any) -> bool:
    return bool(isinstance(prop, dict) and prop.get("checkbox"))


def _record_to_properties(record: JobLedgerRecord) -> dict[str, Any]:
    """Emit only properties that exist in the live canonical Job Ledger
    schema, using their real types and canonical option labels -- never an
    invented property name, and never an internal lowercase enum wire
    value. "Job" is the title property (company + role), never bare
    "Role". Internal v2-only fields with no canonical Wave 1 equivalent
    (compensation_minimum, source_lane, provider_job_id, description_text,
    source_lanes, aliases, lifecycle status/live/review_ready_on) stay in
    memory only -- they are never persisted under a fabricated property."""
    job_observation = record.job.job
    fit = record.job.fit
    return {
        "Job": _title(f"{job_observation.company.name} — {job_observation.role}" if job_observation.company.name or job_observation.role else ""),
        STABLE_KEY_PROPERTY: _rich_text(record.job.stable_job_key),
        "Company": _rich_text(job_observation.company.name),
        "Role": _rich_text(job_observation.role),
        "Location / Work Mode": _rich_text(job_observation.location or ""),
        "Work Mode": _select(_WORK_MODE_TO_CANONICAL[job_observation.work_mode]),
        "Compensation": _rich_text(job_observation.compensation_text or ""),
        "Apply URL": _url(job_observation.apply_url),
        "Posting Date": _date(job_observation.posting_date),
        "LIFE OS Fit": _number(fit),
        "Fit Authority": _select(_FIT_AUTHORITY_TO_CANONICAL[record.job.fit_authority]),
        "Provider Score": _number(job_observation.provider_score),
        "Admission Status": _select(_ADMISSION_TO_CANONICAL[record.job.admission_status]),
        "Source Provider": _rich_text(", ".join(record.job.source_providers)),
        "Source Types": _multi_select(record.job.source_types),
        "Applied": _checkbox(record.applied),
        "Applied On": _date(record.applied_on),
        "First Surfaced": _date(record.first_surfaced),
        "Last Seen": _date(record.last_seen),
    }


def _canonical_view(record: JobLedgerRecord) -> tuple[Any, ...]:
    """The subset of a JobLedgerRecord this repository actually persists.
    upsert()'s authoritative read-back compares this view, not full
    dataclass equality, since fields with no canonical Wave 1 property
    (see _record_to_properties) are intentionally never round-tripped."""
    job_observation = record.job.job
    return (
        record.job.stable_job_key,
        job_observation.company.name,
        job_observation.role,
        job_observation.location,
        job_observation.work_mode,
        job_observation.compensation_text,
        job_observation.apply_url,
        job_observation.posting_date,
        record.job.fit,
        record.job.fit_authority,
        record.job.source_providers,
        record.job.source_types,
        job_observation.provider_score,
        record.job.admission_status,
        record.applied,
        record.applied_on,
        record.first_surfaced,
        record.last_seen,
    )


def _page_to_record(page: dict[str, Any]) -> JobLedgerRecord:
    """Decode the canonical properties this repository owns. Fields with no
    canonical Wave 1 property (compensation_minimum, source_lane,
    provider_job_id, description_text, source_lanes, aliases) are not
    persisted, so they decode to their in-memory-only defaults here -- see
    _canonical_view, which is what upsert()'s read-back actually verifies."""
    props = page.get("properties") or {}
    stable_job_key = _plain_text(props.get(STABLE_KEY_PROPERTY))
    if not stable_job_key:
        raise ReadBackMismatch(f"persisted page {page.get('id')} is missing {STABLE_KEY_PROPERTY!r}")

    work_mode_canonical = _extract_select(props.get("Work Mode"))
    work_mode = _WORK_MODE_FROM_CANONICAL.get(work_mode_canonical, WorkMode.UNKNOWN)
    job_observation = JobObservation(
        company=Company(name=_plain_text(props.get("Company"))),
        role=_plain_text(props.get("Role")),
        location=_plain_text(props.get("Location / Work Mode")) or None,
        work_mode=work_mode,
        compensation_text=_plain_text(props.get("Compensation")) or None,
        compensation_minimum=None,
        posting_date=_extract_date(props.get("Posting Date")),
        apply_url=_extract_url(props.get("Apply URL")),
        source_lane="",
        provider_score=_extract_number(props.get("Provider Score")),
        source_provider=_plain_text(props.get("Source Provider")) or None,
    )
    admission_canonical = _extract_select(props.get("Admission Status"))
    admission_status = _ADMISSION_FROM_CANONICAL.get(admission_canonical, AdmissionStatus.PASSED_REVIEW)
    fit = _extract_number(props.get("LIFE OS Fit"))
    fit_authority_canonical = _extract_select(props.get("Fit Authority"))
    if fit_authority_canonical in _FIT_AUTHORITY_FROM_CANONICAL:
        fit_authority = _FIT_AUTHORITY_FROM_CANONICAL[fit_authority_canonical]
    elif fit is not None:
        fit_authority = FitAuthority.AUTHORITATIVE
    else:
        fit_authority = FitAuthority.NON_AUTHORITATIVE
    source_provider_text = _plain_text(props.get("Source Provider"))
    source_providers = tuple(part.strip() for part in source_provider_text.split(",") if part.strip())
    job = Job(
        stable_job_key=stable_job_key,
        job=job_observation,
        admission_status=admission_status,
        fit=fit,
        fit_authority=fit_authority,
        source_providers=source_providers,
        source_types=_extract_multi_select(props.get("Source Types")),
    )

    first_surfaced = _extract_date(props.get("First Surfaced"))
    last_seen = _extract_date(props.get("Last Seen"))
    if first_surfaced is None or last_seen is None:
        raise ReadBackMismatch(f"persisted page {page.get('id')} is missing required lifecycle dates")

    applied = _extract_checkbox(props.get("Applied"))
    live = admission_status != AdmissionStatus.EXCLUDED
    if applied:
        status = LifecycleStatus.APPLIED
    else:
        status = LifecycleStatus.NEW if live else LifecycleStatus.HISTORICAL

    return JobLedgerRecord(
        job=job,
        status=status,
        applied=applied,
        applied_on=_extract_date(props.get("Applied On")),
        first_surfaced=first_surfaced,
        review_ready_on=first_surfaced + timedelta(days=1),
        last_seen=last_seen,
        live=live,
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

    def get_many(self, stable_job_keys: list[str]) -> dict[str, JobLedgerRecord]:
        if not stable_job_keys:
            return {}
        # NotionIdentityQuery caps a single filter at _MAX_IDENTITY_VALUES
        # (50). Chunk into bounded batches -- never a full-ledger scan, just
        # multiple narrow identity-filtered queries for the same requested set.
        found: dict[str, JobLedgerRecord] = {}
        for chunk in _chunk(stable_job_keys, MAX_IDENTITY_VALUES_PER_QUERY):
            query = NotionIdentityQuery(
                property_name=STABLE_KEY_PROPERTY,
                property_type="rich_text",
                values=tuple(chunk),
            )
            pages = self._transport.query_data_source(self._config.data_source_id, query)
            for page in pages:
                record = _page_to_record(page)
                key = record.job.stable_job_key
                found[key] = record
                page_id = page.get("id")
                if page_id:
                    self._page_ids[key] = str(page_id)
        return found

    def get_by_apply_urls(self, apply_urls: list[str]) -> dict[str, JobLedgerRecord]:
        canonical_urls = list(dict.fromkeys(url for url in (canonical_url(value) for value in apply_urls) if url))
        if not canonical_urls:
            return {}
        found: dict[str, JobLedgerRecord] = {}
        for chunk in _chunk(canonical_urls, MAX_IDENTITY_VALUES_PER_QUERY):
            query = NotionIdentityQuery(
                property_name="Apply URL",
                property_type="url",
                values=tuple(chunk),
            )
            pages = self._transport.query_data_source(self._config.data_source_id, query)
            for page in pages:
                record = _page_to_record(page)
                url = canonical_url(record.job.job.apply_url)
                if url:
                    found[url] = record
                page_id = page.get("id")
                if page_id:
                    self._page_ids[record.job.stable_job_key] = str(page_id)
        return found

    def upsert(self, record: JobLedgerRecord) -> JobLedgerRecord:
        key = record.job.stable_job_key
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
        if _canonical_view(persisted) != _canonical_view(record):
            raise ReadBackMismatch(f"read-back mismatch for {key}")
        return persisted
