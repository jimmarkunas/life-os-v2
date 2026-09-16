"""Tiny elapsed-time budget helpers for product tests."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Iterator


NORMAL_FEATURE_SECONDS = 45.0
ANY_FEATURE_MAX_SECONDS = 300.0


@dataclass(frozen=True)
class RuntimeBudget:
    name: str
    seconds: float


NORMAL_FEATURE_BUDGET = RuntimeBudget("normal-feature", NORMAL_FEATURE_SECONDS)
ANY_FEATURE_ABSOLUTE_MAX = RuntimeBudget("any-feature-absolute-max", ANY_FEATURE_MAX_SECONDS)


class PerformanceBudgetExceeded(AssertionError):
    """Raised when a measured block exceeds its elapsed runtime budget."""


@contextmanager
def assert_runtime_budget(budget: RuntimeBudget, *, clock=monotonic) -> Iterator[None]:
    """Assert that a code block finishes within a wall-clock budget."""

    start = clock()
    yield
    elapsed = clock() - start
    if elapsed > budget.seconds:
        raise PerformanceBudgetExceeded(f"{budget.name} exceeded {budget.seconds:.1f}s budget: elapsed {elapsed:.3f}s")
