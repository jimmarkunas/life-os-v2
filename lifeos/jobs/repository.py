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

from lifeos.jobs.lifecycle import LifecycleRecord


class ReadBackMismatch(RuntimeError):
    """Raised when a write's authoritative read-back does not match the
    intended result. A write is not successful until this check passes."""


class CareerRepository(Protocol):
    def get_many(self, stable_job_keys: list[str]) -> dict[str, LifecycleRecord]:
        """Narrow identity lookup for exactly the keys this run needs.
        Must never require scanning the full canonical store."""
        ...

    def upsert(self, record: LifecycleRecord) -> LifecycleRecord:
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
        self._store: dict[str, LifecycleRecord] = {}

    def get_many(self, stable_job_keys: list[str]) -> dict[str, LifecycleRecord]:
        return {key: self._store[key] for key in stable_job_keys if key in self._store}

    def upsert(self, record: LifecycleRecord) -> LifecycleRecord:
        key = record.opportunity.stable_job_key
        self._store[key] = record
        persisted = self._store.get(key)
        if persisted is None or persisted != record:
            raise ReadBackMismatch(f"read-back mismatch for {key}")
        return replace(persisted)
