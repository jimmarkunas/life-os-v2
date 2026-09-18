"""Focused equivalence proof for the Newsletter-only runtime extraction."""
from __future__ import annotations
import ast
from pathlib import Path

from lifeos.jobs import newsletter_runtime, us_remote_runtime


def test_extraction_keeps_existing_external_resolution_limit() -> None:
    """The extraction must not raise the established eight-call resolver ceiling."""
    source = Path(newsletter_runtime.__file__).read_text(encoding="utf-8")
    assert "max_workers=8" in source
