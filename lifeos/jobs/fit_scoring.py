"""Injected V3 Fit rule-bundle shape."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class FitProfile:
    model_version: str
    title_patterns: dict[str, tuple[str, ...]]
    direct_specialization_patterns: tuple[str, ...]
    dimension_patterns: dict[str, tuple[str, ...]]
    evidence_patterns: dict[str, tuple[str, ...]]
    material_patterns: tuple[str, ...]
    ignore_patterns: tuple[str, ...] = ()
    hard_family_patterns: tuple[str, ...] = ()
