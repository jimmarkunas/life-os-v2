"""Small reusable bounded-backlog consumption mechanic.

Proven by the Newsletter production backlog drain: complete canonical-source
enumeration -> bounded domain-owned batch -> domain-owned processing ->
per-item completion marking only after that item is safely and canonically
accounted for -> the next call naturally resumes by re-reading canonical
source state. This module owns only that coordination. It has no opinion
about what a backlog is, how items are ordered, how they are processed, what
"accounted for" means, or what to do about a failed item -- all of that
stays with the calling domain.
"""
from __future__ import annotations

from typing import Callable, Hashable, Mapping, Sequence, TypeVar

T = TypeVar("T", bound=Hashable)


def consume_bounded_backlog(
    *,
    enumerate_backlog: Callable[[], Sequence[T]],
    batch_size: int,
    process_batch: Callable[[Sequence[T]], Mapping[T, bool]],
    mark_complete: Callable[[T], None],
    admit_item: Callable[[T, int], bool] | None = None,
) -> Sequence[T]:
    """Enumerate the domain's complete current backlog, in whatever
    deterministic order the domain already supplies, take at most
    ``batch_size`` of its leading items, hand exactly that batch to the
    domain's own processing/accounting, and mark complete only the items the
    domain reports as safely accounted for.

    Returns the batch that was attempted, for the caller's own reporting.
    Keeps no cursor or checkpoint of its own: a later call naturally resumes
    from wherever ``enumerate_backlog`` says the canonical source currently
    stands (e.g. because completed items no longer appear in it).
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    backlog = tuple(enumerate_backlog())
    batch_items: list[T] = []
    for item in backlog:
        if len(batch_items) >= batch_size:
            break
        if admit_item is not None and not admit_item(item, len(batch_items)):
            break
        batch_items.append(item)
    batch = tuple(batch_items)
    if not batch:
        return ()
    accounted = process_batch(batch)
    for item in batch:
        if accounted.get(item, False):
            mark_complete(item)
    return batch
