"""Executable public contract for canonical Job Ledger writes.

This stays synthetic: no production IDs, records, or private policy.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from lifeos.jobs.lifecycle import new_record
from lifeos.jobs.models import AdmissionStatus, Company, FitAuthority, Job, JobObservation, WorkMode
from lifeos.jobs.notion_repository import _record_to_properties

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "contracts" / "job_ledger_schema.json").read_text())


def _job(*, work_mode: WorkMode = WorkMode.REMOTE, provider_score: int | None = 87) -> Job:
    return JobObservation(
        company=Company(name="Synthetic Company"),
        role="Synthetic Program Manager",
        location="Remote",
        work_mode=work_mode,
        compensation_text="$100k",
        compensation_minimum=100000,
        posting_date=date(2026, 1, 10),
        apply_url="https://example.invalid/jobs/1",
        source_lane="Newsletter",
        provider_job_id="synthetic-1",
        description_text="Synthetic description",
        provider_score=provider_score,
    )


def _properties(
    *,
    work_mode: WorkMode = WorkMode.REMOTE,
    admission_status: AdmissionStatus = AdmissionStatus.ADMITTED,
    fit: int | None = 82,
    fit_authority: FitAuthority = FitAuthority.AUTHORITATIVE,
    source_lanes: tuple[str, ...] = (),
    source_types: tuple[str, ...] = (),
):
    job = Job(
        stable_job_key="synthetic-key",
        job=_job(work_mode=work_mode),
        admission_status=admission_status,
        source_lanes=source_lanes,
        fit=fit,
        fit_authority=fit_authority,
        source_types=source_types,
    )
    return _record_to_properties(new_record(job, run_date=date(2026, 1, 15)))


def test_repository_writes_only_contract_properties_and_types():
    properties = _properties()
    unknown = sorted(set(properties) - set(SCHEMA))
    assert unknown == [], f"repository writes noncanonical Job Ledger properties: {unknown}"

    mismatches = []
    for name, payload in properties.items():
        actual_type = next(iter(payload.keys()))
        expected_type = SCHEMA[name]["type"]
        if actual_type != expected_type:
            mismatches.append(f"{name}: expected {expected_type}, got {actual_type}")
    assert mismatches == []
    required = {"Job", "Stable Job Key", "Company", "Role", "LIFE OS Fit", "Fit Authority", "Provider Score"}
    assert required <= set(properties)
    provenance = _properties(
        source_lanes=("Synthetic-Remote", "Newsletter"),
        source_types=("LinkedIn Jobs", "Gmail Alert"),
    )
    names = {item["name"] for item in provenance["Source Types"]["multi_select"]}
    assert names == {"LinkedIn Jobs", "Gmail Alert"}
    assert "Synthetic-Remote" not in names
    assert "Newsletter" not in names


def test_repository_select_values_match_canonical_contract():
    for work_mode in WorkMode:
        properties = _properties(work_mode=work_mode)
        actual = properties["Work Mode"]["select"]["name"]
        assert actual in SCHEMA["Work Mode"]["allowed_values"]

    for admission_status in AdmissionStatus:
        properties = _properties(admission_status=admission_status)
        actual = properties["Admission Status"]["select"]["name"]
        assert actual in SCHEMA["Admission Status"]["allowed_values"]

    authoritative = _properties(fit=82)["Fit Authority"]["select"]["name"]
    non_authoritative = _properties(fit=None, fit_authority=FitAuthority.NON_AUTHORITATIVE)["Fit Authority"]["select"]["name"]
    assert authoritative == "Authoritative"
    assert non_authoritative == "Non-Authoritative"
    assert {authoritative, non_authoritative} <= set(SCHEMA["Fit Authority"]["allowed_values"])
    assert "remote" not in SCHEMA["Work Mode"]["allowed_values"]
    assert "admitted" not in SCHEMA["Admission Status"]["allowed_values"]
    assert "passed_review" not in SCHEMA["Admission Status"]["allowed_values"]
