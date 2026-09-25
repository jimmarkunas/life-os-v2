"""Idempotent Career repository contract.

New for v2 -- v1's `jobs/notion_repository.py` had the right upsert/property
-mapping shape but its `index()` method queried the *entire* Notion database
on every run (RETIRE: that is exactly the "complete Job Ledger reread for
every run" the platform contract prohibits as a normal hot path). v2's
contract instead requires narrow identity lookup: `get_many(keys)` for the
bounded set of stable_job_keys a run actually touches, never a full scan.

This module defines the Protocol only. A real Notion-backed implementation
belongs in `integrations/notion` (platform-core owned) once a second real
consumer proves the shared client is worth building; until then, Career
tests use `InMemoryCareerRepository` below, which is synthetic-only and
must never be used against a production data source.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from lifeos.jobs.identity import canonical_url
from lifeos.jobs.lifecycle import JobLedgerRecord


class ReadBackMismatch(RuntimeError):
    """Raised when a write's authoritative read-back does not match the
    intended result. A write is not successful until this check passes."""
    def __init__(self, message: str, *, mismatches: list[dict] | None = None):
        super().__init__(message)
        self.mismatches = mismatches or []


class CareerRepository(Protocol):
    def known(self, stable_job_keys: list[str]) -> dict[str, JobLedgerRecord]:
        """Return only execution-local authoritative rows already known.

        This is intentionally not a persistence lookup and must never query
        the canonical store.
        """
        ...

    def cache(self, record: JobLedgerRecord) -> None:
        """Register a record in the execution-local cache without writing to
        the canonical store. Used by dry-run passes to seed the known() cache
        so subsequent passes can resolve identity without extra queries."""
        ...

    def get_many(self, stable_job_keys: list[str]) -> dict[str, JobLedgerRecord]:
        """Narrow identity lookup for exactly the keys this run needs.
        Must never require scanning the full canonical store."""
        ...

    def get_by_apply_urls(self, apply_urls: list[str]) -> dict[str, JobLedgerRecord]:
        """Narrow lookup by canonical Apply URL for exactly the URLs this run
        touches. Must never require scanning the full canonical store."""
        ...

    def upsert(self, record: JobLedgerRecord) -> JobLedgerRecord:
        """Write one record, preserving any existing human-owned fields the
        caller did not intend to change (see lifecycle.apply_observation),
        then read back and return the authoritative persisted state. Must
        raise ReadBackMismatch rather than return an unverified result."""
        ...


class InMemoryCareerRepository:
    """Synthetic-only reference implementation for tests. Proves the
    Protocol's read-back and narrow-lookup contract without any network or
    production dependency. Never wire this to a real production data path."""

    def __init__(self) -> None:
        self._store: dict[str, JobLedgerRecord] = {}
        self._last_persistence_accounting: dict[str, int] = {"input": 0, "unchanged": 0, "updated": 0, "created": 0, "authoritative_read_back_verified": 0}

    @property
    def last_persistence_accounting(self) -> dict[str, int]:
        return dict(self._last_persistence_accounting)

    def get_many(self, stable_job_keys: list[str]) -> dict[str, JobLedgerRecord]:
        return {key: self._store[key] for key in stable_job_keys if key in self._store}

    def known(self, stable_job_keys: list[str]) -> dict[str, JobLedgerRecord]:
        return self.get_many(stable_job_keys)

    def get_by_apply_urls(self, apply_urls: list[str]) -> dict[str, JobLedgerRecord]:
        requested = {url for url in (canonical_url(value) for value in apply_urls) if url}
        found: dict[str, JobLedgerRecord] = {}
        for record in self._store.values():
            url = canonical_url(record.job.job.apply_url)
            if url in requested:
                found[url] = record
        return found

    def cache(self, record: JobLedgerRecord) -> None:
        self._store[record.job.stable_job_key] = record

    def upsert(self, record: JobLedgerRecord) -> JobLedgerRecord:
        key = record.job.stable_job_key
        existed = key in self._store
        self._store[key] = record
        persisted = self._store.get(key)
        if persisted is None or persisted != record:
            raise ReadBackMismatch(f"read-back mismatch for {key}")
        self._last_persistence_accounting = {"input": 1, "unchanged": 0, "updated": int(existed), "created": int(not existed), "authoritative_read_back_verified": 1}
        return replace(persisted)

    def upsert_many(self, records: list[JobLedgerRecord]) -> dict[str, JobLedgerRecord]:
        unchanged = created = updated = 0
        result: dict[str, JobLedgerRecord] = {}
        for record in records:
            key = record.job.stable_job_key
            existed = key in self._store
            if existed and self._store[key] == record:
                unchanged += 1
                result[key] = replace(self._store[key])
                continue
            persisted = self.upsert(record)
            result[key] = persisted
            if existed:
                updated += 1
            else:
                created += 1
        self._last_persistence_accounting = {"input": len(records), "unchanged": unchanged, "updated": updated, "created": created, "authoritative_read_back_verified": updated + created}
        return result
