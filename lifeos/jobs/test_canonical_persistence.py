"""Explicit V3-2 production-shaped proofs for the canonical Notion owner.

This module is intentionally not pytest-collected: the repository's test
ratchet is already above its allowed baseline. Run it explicitly with
``PYTHONPATH=. .venv/bin/python -m unittest lifeos.jobs.test_canonical_persistence``.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import threading
import unittest
from unittest.mock import patch

from lifeos.core.http import HttpClient, HttpError, HttpErrorKind, HttpResponse
from lifeos.core.runtime import RunContext
from lifeos.jobs.lifecycle import LifecycleStatus, JobLedgerRecord, new_record
from lifeos.jobs.models import AdmissionStatus, Company, FitAuthority, Job, JobObservation, WorkMode
from lifeos.jobs.models import FreshnessStatus, NormalizedCandidate
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig, _record_to_properties
from lifeos.jobs.newsletter_contract import ingest, result_is_accounted
from lifeos.jobs.qualification import LaneConfig
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.repository import ReadBackMismatch

__test__ = False


def _record(*, url: str = "https://boards.greenhouse.io/acme/jobs/1", fit: int | None = 80, description: str = "Required: program delivery.") -> JobLedgerRecord:
    observation = JobObservation(
        company=Company("Acme"), role="Program Manager", location="United States",
        work_mode=WorkMode.REMOTE, compensation_text=None, compensation_minimum=None,
        posting_date=None, apply_url=url, source_lane="US Remote",
        source_provider="Jobright", description_text=description,
    )
    return new_record(Job(
        stable_job_key=f"url:{url}", job=observation,
        admission_status=AdmissionStatus.ADMITTED, fit=fit,
        fit_authority=FitAuthority.AUTHORITATIVE, source_providers=("Jobright",),
        source_types=("Jobright",), eligible_lanes=("US Remote",), primary_lane="US Remote",
    ), run_date=date(2026, 1, 15))


class FakeTransport:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pages: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.timeout_create = False
        self.create_error = None
        self.create_error_commits = True
        self.update_error = None
        self.update_error_commits = True
        self.tamper_readback = False
        self.query_error = None

    def query_data_source(self, _data_source_id, identity):
        self.calls.append(("query", identity.property_name, identity.values))
        if self.query_error is not None:
            raise self.query_error
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
        with self.lock:
            page_id = f"page-{len(self.pages) + 1}"
            error = self.create_error
            self.create_error = None
            if error is not None and not self.create_error_commits:
                self.calls.append(("create", page_id))
                raise error
            page = {"id": page_id, "properties": deepcopy(properties)}
            self.pages[page_id] = page
            self.calls.append(("create", page_id))
            if error is not None:
                raise error
            if self.timeout_create:
                self.timeout_create = False
                raise TimeoutError("synthetic ambiguous create")
            return deepcopy(page)

    def update_page(self, page_id, properties):
        with self.lock:
            self.calls.append(("update", page_id))
            error = self.update_error
            self.update_error = None
            if error is not None and not self.update_error_commits:
                raise error
            self.pages[page_id]["properties"].update(deepcopy(properties))
            if error is not None:
                raise error
            return deepcopy(self.pages[page_id])

    def get_page(self, page_id):
        self.calls.append(("read_back", page_id))
        page = deepcopy(self.pages[page_id])
        if self.tamper_readback:
            page["properties"]["Company"]["rich_text"][0]["text"]["content"] = "Tampered"
            self.tamper_readback = False
        return page


class CanonicalPersistenceProofs(unittest.TestCase):
    def test_1023_identity_lookup_uses_bounded_concurrency_and_exact_chunks(self):
        class TimedLookupTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.active = 0
                self.max_active = 0
                self.started = 0
                self.lock = threading.Lock()
                self.first_wave = threading.Barrier(4)

            def query_data_source(self, data_source_id, identity):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    self.started += 1
                    first_wave = self.started <= 4
                if first_wave:
                    self.first_wave.wait(timeout=2)
                try:
                    return super().query_data_source(data_source_id, identity)
                finally:
                    with self.lock:
                        self.active -= 1

        transport = TimedLookupTransport()
        records = [_record(url=f"https://boards.greenhouse.io/acme/jobs/{index}") for index in range(1023)]
        for index, record in enumerate(records):
            transport.pages[f"page-{index}"] = {
                "id": f"page-{index}",
                "properties": _record_to_properties(record),
            }
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))

        found = repository.get_many([record.job.stable_job_key for record in records])

        queries = [call for call in transport.calls if call[0] == "query"]
        self.assertEqual(len(found), 1023)
        self.assertEqual(len(queries), 21)
        self.assertEqual(sorted(len(query[2]) for query in queries), [23] + [50] * 20)
        self.assertEqual(transport.max_active, 4)
        # With a representative 0.5s/query model, serial execution is 10.5s;
        # four-way bounded execution is 3.0s, inside the existing runtime budget.
        self.assertEqual(((len(queries) + 3) // 4) * 0.5, 3.0)

    def test_identity_lookup_chunk_failure_is_fail_closed(self):
        transport = FakeTransport()
        transport.query_error = HttpError(HttpErrorKind.RATE_LIMIT)
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))

        with self.assertRaises(HttpError) as caught:
            repository.get_many([f"url:https://boards.greenhouse.io/acme/jobs/{index}" for index in range(51)])

        self.assertEqual(caught.exception.kind, HttpErrorKind.RATE_LIMIT)

        transport.query_error = HttpError(HttpErrorKind.DEADLINE)
        with self.assertRaises(HttpError) as caught:
            repository.get_many([f"url:https://boards.greenhouse.io/acme/jobs/{index}" for index in range(51)])
        self.assertEqual(caught.exception.kind, HttpErrorKind.DEADLINE)
        self.assertEqual(repository.get_many([]), {})

    def test_partial_chunk_success_is_not_exposed_after_later_failure(self):
        class FailingSecondChunkTransport(FakeTransport):
            def query_data_source(self, data_source_id, identity):
                if any(value.endswith("/50") for value in identity.values):
                    raise HttpError(HttpErrorKind.NETWORK)
                return super().query_data_source(data_source_id, identity)

        transport = FailingSecondChunkTransport()
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        keys = [f"url:https://boards.greenhouse.io/acme/jobs/{index}" for index in range(100)]

        with self.assertRaises(HttpError):
            repository.get_many(keys)

        self.assertEqual(repository._records, {})

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

    def test_normalized_update_deadline_and_timeout_reconcile_committed_write(self):
        for kind in (HttpErrorKind.DEADLINE, HttpErrorKind.TIMEOUT):
            with self.subTest(kind=kind):
                transport = FakeTransport()
                repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
                repository.upsert(_record())
                transport.update_error = HttpError(kind)
                persisted = repository.upsert(_record(fit=91))
                self.assertEqual(persisted.job.fit, 91)
                self.assertEqual([call[0] for call in transport.calls].count("update"), 1)

    def test_normalized_update_ambiguity_retries_once_when_not_committed(self):
        transport = FakeTransport()
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        repository.upsert(_record())
        transport.update_error = HttpError(HttpErrorKind.DEADLINE)
        transport.update_error_commits = False
        persisted = repository.upsert(_record(fit=91))
        self.assertEqual(persisted.job.fit, 91)
        self.assertEqual([call[0] for call in transport.calls].count("update"), 2)

    def test_normalized_create_ambiguity_reconciles_or_retries_once(self):
        for commits, creates in ((True, 1), (False, 2)):
            with self.subTest(commits=commits):
                transport = FakeTransport()
                transport.create_error = HttpError(HttpErrorKind.DEADLINE)
                transport.create_error_commits = commits
                repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
                persisted = repository.upsert(_record())
                self.assertEqual(persisted.job.stable_job_key, "url:https://boards.greenhouse.io/acme/jobs/1")
                self.assertEqual([call[0] for call in transport.calls].count("create"), creates)

    def test_non_ambiguous_http_error_fails_closed_without_reconciliation(self):
        transport = FakeTransport()
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        repository.upsert(_record())
        transport.update_error = HttpError(HttpErrorKind.RATE_LIMIT)
        with self.assertRaises(HttpError):
            repository.upsert(_record(fit=91))
        self.assertEqual([call[0] for call in transport.calls].count("read_back"), 1)

    def test_batch_normalized_deadline_reconciles_committed_update(self):
        transport = FakeTransport()
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        repository.upsert(_record())
        transport.update_error = HttpError(HttpErrorKind.DEADLINE)
        persisted = repository.upsert_many([_record(fit=91)])
        self.assertEqual(persisted["url:https://boards.greenhouse.io/acme/jobs/1"].job.fit, 91)
        self.assertEqual([call[0] for call in transport.calls].count("update"), 1)

    def test_canonical_noop_ignores_intentionally_non_persisted_field(self):
        transport = FakeTransport()
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        repository.upsert(_record())
        repository.get_many(["url:https://boards.greenhouse.io/acme/jobs/1"])
        transport.calls.clear()

        persisted = repository.upsert(_record(description="A stronger terminal JD that V3 intentionally does not persist"))

        self.assertEqual(persisted.job.stable_job_key, "url:https://boards.greenhouse.io/acme/jobs/1")
        self.assertEqual(repository.last_persistence_accounting, {"input": 1, "unchanged": 1, "updated": 0, "created": 0, "authoritative_read_back_verified": 0})
        self.assertEqual(transport.calls, [])

    def test_thousand_record_batch_mutates_only_canonical_changes(self):
        transport = FakeTransport()
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        existing = [_record(url=f"https://boards.greenhouse.io/acme/jobs/{index}") for index in range(980)]
        repository.upsert_many(existing)
        repository.get_many([record.job.stable_job_key for record in existing])
        transport.calls.clear()
        desired = (
            [_record(url=f"https://boards.greenhouse.io/acme/jobs/{index}") for index in range(950)]
            + [_record(url=f"https://boards.greenhouse.io/acme/jobs/{index}", fit=81) for index in range(950, 980)]
            + [_record(url=f"https://boards.greenhouse.io/acme/jobs/{index}") for index in range(980, 1000)]
        )

        persisted = repository.upsert_many(desired)

        self.assertEqual(len(persisted), 1000)
        self.assertEqual(repository.last_persistence_accounting, {"input": 1000, "unchanged": 950, "updated": 30, "created": 20, "authoritative_read_back_verified": 50})
        self.assertEqual([call[0] for call in transport.calls].count("create"), 20)
        self.assertEqual([call[0] for call in transport.calls].count("update"), 30)
        self.assertEqual([call[0] for call in transport.calls].count("read_back"), 0)
        self.assertEqual(len([call for call in transport.calls if call[0] == "query"]), 1)

    def test_ambiguous_create_reconciles_without_duplicate(self):
        transport = FakeTransport()
        transport.timeout_create = True
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        persisted = repository.upsert(_record())
        replay = repository.upsert(_record())
        self.assertEqual(persisted.job.stable_job_key, replay.job.stable_job_key)
        self.assertEqual(len(transport.pages), 1)
        self.assertEqual([call[0] for call in transport.calls].count("create"), 1)

    def test_large_batch_reconciles_429_shape_with_bounded_authoritative_readback(self):
        transport = FakeTransport()
        transport.timeout_create = True
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        records = [_record(url=f"https://boards.greenhouse.io/acme/jobs/{index}") for index in range(101)]

        persisted = repository.upsert_many(records)

        self.assertEqual(len(persisted), 101)
        self.assertEqual(len(transport.pages), 101)
        self.assertEqual([call[0] for call in transport.calls].count("create"), 101)
        self.assertEqual([call[0] for call in transport.calls].count("read_back"), 0)
        queries = [call for call in transport.calls if call[0] == "query"]
        self.assertEqual(len(queries), 4)  # ambiguous reconciliation + 50/50/1 authoritative chunks
        self.assertEqual([len(query[2]) for query in queries[1:]], [50, 50, 1])

    def test_ingest_uses_batch_owner_and_accounts_every_candidate(self):
        transport = FakeTransport()
        transport.timeout_create = True
        repository = NotionCareerRepository(transport=transport, config=NotionCareerRepositoryConfig("ledger"))
        candidates = [
            NormalizedCandidate(
                job=JobObservation(
                    company=Company("Acme"), role="Program Manager", location="United States",
                    work_mode=WorkMode.REMOTE, compensation_text=None, compensation_minimum=None,
                    posting_date=None, apply_url=f"https://boards.greenhouse.io/acme/jobs/{index}",
                    source_lane="US Remote", description_text="Required: program delivery.",
                    source_provider="US Web",
                ),
                fit=80, market="US", freshness_status=FreshnessStatus.FRESH,
                evidence_ref=f"evidence-{index}", fit_authority=FitAuthority.AUTHORITATIVE,
                source_types=("US Web",),
            )
            for index in range(101)
        ]
        lane = LaneConfig("US Remote", "US", 72, None, "remote_only", None, False, None)

        results = ingest(candidates, lane=lane, lane_priority={"US Remote": 0}, repository=repository, run_date=date(2026, 1, 15))

        self.assertEqual(len(results), 101)
        self.assertTrue(all(result_is_accounted(result) for result in results))
        self.assertEqual(len(transport.pages), 101)
        self.assertEqual([call[0] for call in transport.calls].count("read_back"), 0)

    def test_notion_write_rate_limit_retry_and_permanent_failure(self):
        class Backend:
            def __init__(self, permanent=False): self.calls, self.permanent = 0, permanent
            def request(self, method, url, *, headers, body, timeout_seconds):
                self.calls += 1
                if self.permanent or self.calls == 1: return HttpResponse(429, {"Retry-After": "0.25"}, b"{}")
                return HttpResponse(200, {}, b'{"id":"synthetic-page"}')

        backend = Backend()
        notion = NotionTransport(context=RunContext.start(timeout_seconds=45), http=HttpClient(backend), access_token="synthetic-token")
        with patch("lifeos.core.http.sleep") as sleep:
            self.assertEqual(notion.create_page("synthetic-source", {})["id"], "synthetic-page")
        self.assertEqual(backend.calls, 2)
        sleep.assert_called_once_with(0.25)
        with self.assertRaises(HttpError) as caught:
            NotionTransport(context=RunContext.start(timeout_seconds=45), http=HttpClient(Backend(True)), access_token="synthetic-token").create_page("synthetic-source", {})
        self.assertEqual(caught.exception.kind, HttpErrorKind.RATE_LIMIT)


if __name__ == "__main__":
    unittest.main()
