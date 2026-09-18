"""Focused equivalence proof for the Newsletter-only runtime extraction."""
from __future__ import annotations
import ast
from pathlib import Path

from lifeos.jobs import newsletter_runtime, us_remote_runtime


_SHARED_HELPERS = (
    "_preexclude",
    "_accepted_newsletter_message_ids",
)


def test_extraction_reuses_canonical_newsletter_helpers() -> None:
    """Extraction must delegate shared policy/accounting helpers to the current runtime."""
    for name in _SHARED_HELPERS:
        assert getattr(newsletter_runtime, name) is getattr(us_remote_runtime, name)


def test_extraction_preserves_canonical_side_effect_boundaries() -> None:
    """Newsletter-only composition must reuse canonical integrations, not fork them."""
    source = Path(newsletter_runtime.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "ingest" in called
    assert "_adapt_all" in called
    assert "_accepted_newsletter_message_ids" in called
    assert "USRemoteAcquirer" not in source
    assert "web_adapter" not in source
    assert "web_candidates" not in source


def test_extraction_keeps_existing_external_resolution_limit() -> None:
    """The extraction must not raise the established eight-call resolver ceiling."""
    source = Path(newsletter_runtime.__file__).read_text(encoding="utf-8")
    assert "max_workers=8" in source
