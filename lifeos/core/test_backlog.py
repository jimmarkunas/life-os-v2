from __future__ import annotations

import unittest

from lifeos.core.backlog import consume_bounded_backlog


class FakeCanonicalSource:
    """A synthetic canonical source: a fixed, domain-ordered backlog plus a
    completed-set, mirroring Gmail's own "label minus processed-label"
    shape without any platform-owned cursor/checkpoint."""

    def __init__(self, items: tuple[str, ...]) -> None:
        self._items = items
        self._completed: set[str] = set()

    def enumerate_backlog(self) -> tuple[str, ...]:
        return tuple(item for item in self._items if item not in self._completed)

    def mark_complete(self, item: str) -> None:
        self._completed.add(item)


class ConsumeBoundedBacklogTests(unittest.TestCase):
    def test_complete_enumeration_bounded_selection_and_fail_closed_completion(self) -> None:
        source = FakeCanonicalSource(("A", "B", "C", "D", "E"))
        seen_full_backlog: list[tuple[str, ...]] = []

        def process_batch(batch):
            seen_full_backlog.append(source.enumerate_backlog())  # captured before mutation below
            # Domain-owned accounting: A is safely accounted, B fails/degrades,
            # C succeeds independently. The shared primitive has no opinion
            # about this policy -- it only acts on what the domain reports.
            return {"A": True, "B": False, "C": True}

        batch = consume_bounded_backlog(
            enumerate_backlog=source.enumerate_backlog,
            batch_size=3,
            process_batch=process_batch,
            mark_complete=source.mark_complete,
        )

        # Complete enumeration saw all five before any selection/mutation.
        self.assertEqual(seen_full_backlog[0], ("A", "B", "C", "D", "E"))
        # Bounded selection took exactly the leading three, domain order preserved.
        self.assertEqual(batch, ("A", "B", "C"))
        # A and C were safely accounted -> completed. B failed -> not completed.
        self.assertEqual(source._completed, {"A", "C"})
        self.assertNotIn("B", source._completed)

    def test_failed_item_is_resumable_from_canonical_source_state_with_no_checkpoint(self) -> None:
        source = FakeCanonicalSource(("A", "B", "C", "D", "E"))

        def first_process_batch(batch):
            return {"A": True, "B": False, "C": True}

        consume_bounded_backlog(
            enumerate_backlog=source.enumerate_backlog,
            batch_size=3,
            process_batch=first_process_batch,
            mark_complete=source.mark_complete,
        )

        # Nothing platform-owned was recorded about "where we left off" --
        # calling enumerate_backlog again against the same canonical source
        # naturally reflects A and C being gone and B still present.
        self.assertEqual(source.enumerate_backlog(), ("B", "D", "E"))

        def second_process_batch(batch):
            # B succeeds this time; D also succeeds; both were genuinely
            # reprocessed, not skipped due to any prior failure memory.
            return {item: True for item in batch}

        second_batch = consume_bounded_backlog(
            enumerate_backlog=source.enumerate_backlog,
            batch_size=3,
            process_batch=second_process_batch,
            mark_complete=source.mark_complete,
        )

        self.assertEqual(second_batch, ("B", "D", "E"))
        self.assertEqual(source.enumerate_backlog(), ())

    def test_empty_backlog_is_a_safe_no_op(self) -> None:
        source = FakeCanonicalSource(())
        calls: list[object] = []
        batch = consume_bounded_backlog(
            enumerate_backlog=source.enumerate_backlog,
            batch_size=10,
            process_batch=lambda b: calls.append(b) or {},
            mark_complete=source.mark_complete,
        )
        self.assertEqual(batch, ())
        self.assertEqual(calls, [])

    def test_batch_size_must_be_positive(self) -> None:
        source = FakeCanonicalSource(("A",))
        with self.assertRaises(ValueError):
            consume_bounded_backlog(
                enumerate_backlog=source.enumerate_backlog,
                batch_size=0,
                process_batch=lambda b: {},
                mark_complete=source.mark_complete,
            )

    def test_admission_callback_stops_before_starting_next_item(self) -> None:
        source = FakeCanonicalSource(("A", "B", "C", "D"))
        attempted: list[tuple[str, ...]] = []

        batch = consume_bounded_backlog(
            enumerate_backlog=source.enumerate_backlog,
            batch_size=4,
            process_batch=lambda b: attempted.append(tuple(b)) or {item: True for item in b},
            mark_complete=source.mark_complete,
            admit_item=lambda _item, index: index < 2,
        )

        self.assertEqual(batch, ("A", "B"))
        self.assertEqual(attempted, [("A", "B")])
        self.assertEqual(source.enumerate_backlog(), ("C", "D"))

    def test_none_batch_size_uses_admission_without_fixed_count_cap(self) -> None:
        source = FakeCanonicalSource(tuple(str(index) for index in range(12)))

        batch = consume_bounded_backlog(
            enumerate_backlog=source.enumerate_backlog,
            batch_size=None,
            process_batch=lambda b: {item: True for item in b},
            mark_complete=source.mark_complete,
            admit_item=lambda _item, index: index < 11,
        )

        self.assertEqual(len(batch), 11)
        self.assertEqual(source.enumerate_backlog(), ("11",))


if __name__ == "__main__":
    unittest.main()
