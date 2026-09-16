from __future__ import annotations

from lifeos.jobs.lifecycle import new_record
from lifeos.jobs.models import AdmissionStatus, Opportunity
from lifeos.jobs.repository import InMemoryCareerRepository
from tests.jobs.fixtures import RUN_DATE, make_job


def test_get_many_returns_only_requested_keys_present():
    repo = InMemoryCareerRepository()
    opp = Opportunity(stable_job_key="k1", job=make_job(), admission_status=AdmissionStatus.ADMITTED)
    repo.upsert(new_record(opp, run_date=RUN_DATE))
    found = repo.get_many(["k1", "k2"])
    assert set(found) == {"k1"}


def test_upsert_read_back_matches_written_record():
    repo = InMemoryCareerRepository()
    opp = Opportunity(stable_job_key="k1", job=make_job(), admission_status=AdmissionStatus.ADMITTED)
    record = new_record(opp, run_date=RUN_DATE)
    persisted = repo.upsert(record)
    assert persisted == record


def test_no_full_scan_required_for_narrow_lookup():
    """Documents the contract: get_many must not require enumerating the
    whole store. This in-memory repo is a dict lookup by construction; a
    real implementation's get_many must be implemented as a filtered query,
    never repo.query_rows()-equivalent full enumeration."""
    repo = InMemoryCareerRepository()
    for i in range(50):
        opp = Opportunity(stable_job_key=f"k{i}", job=make_job(), admission_status=AdmissionStatus.ADMITTED)
        repo.upsert(new_record(opp, run_date=RUN_DATE))
    found = repo.get_many(["k7"])
    assert set(found) == {"k7"}
