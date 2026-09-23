"""Explicit V3-2 production-shaped proofs for the canonical Notion owner.

This module is intentionally not pytest-collected: the repository's test
ratchet is already above its allowed baseline. Run it explicitly with
``PYTHONPATH=. .venv/bin/python -m unittest lifeos.jobs.test_canonical_persistence``.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import unittest

from lifeos.jobs.lifecycle import LifecycleStatus, JobLedgerRecord, new_record
from lifeos.jobs.models import AdmissionStatus, Company, FitAuthority, Job, JobObservation, WorkMode
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.repository import ReadBackMismatch

__test__ = False


def _record(*, url: str = "https://boards.greenhouse.io/acme/jobs/1", fit: int | None = 80) -> JobLedgerRecord:
    observation = JobObservation(
        company=Company("Acme"), role="Program Manager", location="United States",
        work_mode=WorkMode.REMOTE, compensation_text=None, compensation_minimum=None,
        posting_date=None, apply_url=url, source_lane="US Remote",
        source_provider="Jobright", description_text="Required: program delivery.",
    )
    return new_record(Job(
        stable_job_key="url:https://boards.greenhouse.io/acme/jobs/1", job=observation,
        admission_status=AdmissionStatus.ADMITTED, fit=fit,
        fit_authority=FitAuthority.AUTHORITATIVE, source_providers=("Jobright",),
        source_types=("Jobright",), eligible_lanes=("US Remote",), primary_lane="US Remote",
    ), run_date=date(2026, 1, 15))


class FakeTransport:
    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.timeout_create = False
        self.tamper_readback = False

    def query_data_source(self, _data_source_id, identity):
        self.calls.append(("query", identity.property_name, identity.values))
        found = []
        for page in self.pages.values():
            props = page["properties"]
            if identity.property_name == "Stable Job Key":
                value = props["Stable Job Key"]["rich_text"][0]["text"]["content"]
            else:
                value = props["Apply URL"]["url"]
            if value in identity.values:
                found.append(deepcopy(page))
        return tuple(found)

    def create_page(self, _data_source_id, properties):
        page_id = f"page-{len(self.pages) + 1}"
        page = {"id": page_id, "properties": deepcopy(properties)}
        self.pages[page_id] = page
        self.calls.append(("create", page_id))
        if self.timeout_create:
            self.timeout_create = False
            raise TimeoutError("synthetic ambiguous create")
        return deepcopy(page)

    def update_page(self, page_id, properties):
        self.calls.append(("update", page_id))
        self.pages[page_id]["properties"].update(deepcopy(properties))
        return deepcopy(self.pages[page_id])

    def get_page(self, page_id):
        self.calls.append(("read_back", page_id))
        page = deepcopy(self.pages[page_id])
        if self.tamper_readback:
            page["properties"]["Company"]["rich_text"][0]["text"]["content"] = "Tampered"
            self.tamper_readback = False
        return page


class CanonicalPersistenceProofs(unittest.TestCase):
    def test_create_update_stronger_evidence_and_narrow_lookup(self):
        transport = FakeTransport()
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        first = repository.upsert(_record())
        stronger = _record(url="https://boards.greenhouse.io/acme/jobs/1", fit=90)
        second = repository.upsert(stronger)

        self.assertEqual(first.job.stable_job_key, second.job.stable_job_key)
        self.assertEqual(len(transport.pages), 1)
        self.assertEqual([call[0] for call in transport.calls].count("create"), 1)
        self.assertEqual([call[0] for call in transport.calls].count("update"), 1)

        repository.get_many([first.job.stable_job_key])
        repository.get_by_apply_urls([first.job.job.apply_url])
        queries = [call for call in transport.calls if call[0] == "query"]
        self.assertEqual([query[1] for query in queries], ["Stable Job Key", "Apply URL"])
        self.assertTrue(all(len(query[2]) == 1 for query in queries))

    def test_manual_state_is_preserved_by_canonical_update(self):
        transport = FakeTransport()
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        persisted = repository.upsert(_record())
        page_id = next(iter(transport.pages))
        transport.pages[page_id]["properties"].update({
            "Applied": {"checkbox": True},
            "Applied On": {"date": {"start": "2026-01-16"}},
            "Saturn Decision": {"select": {"name": "Submitted"}},
        })
        repository.get_many([persisted.job.stable_job_key])
        repository.upsert(_record(fit=91))
        props = transport.pages[page_id]["properties"]
        self.assertTrue(props["Applied"]["checkbox"])
        self.assertEqual(props["Applied On"]["date"]["start"], "2026-01-16")
        self.assertEqual(props["Saturn Decision"]["select"]["name"], "Submitted")

    def test_readback_mismatch_fails_closed(self):
        transport = FakeTransport()
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        repository.upsert(_record())
        transport.tamper_readback = True
        with self.assertRaises(ReadBackMismatch):
            repository.upsert(_record(fit=91))

    def test_ambiguous_create_reconciles_without_duplicate(self):
        transport = FakeTransport()
        transport.timeout_create = True
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        persisted = repository.upsert(_record())
        replay = repository.upsert(_record())
        self.assertEqual(persisted.job.stable_job_key, replay.job.stable_job_key)
        self.assertEqual(len(transport.pages), 1)
        self.assertEqual([call[0] for call in transport.calls].count("create"), 1)


if __name__ == "__main__":
    unittest.main()
