"""Pure, deterministic arithmetic for the LIFE OS Fit score."""
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

DIMENSIONS = {"role_seniority": Fraction(29), "functional": Fraction(29),
              "technical_platform": Fraction(21), "delivery_complexity": Fraction(14),
              "competitive_advantage": Fraction(7)}
EVIDENCE = {"DIRECT": Fraction(1), "ADJACENT": Fraction(3, 4),
            "METHOD_EQUIVALENT": Fraction(3, 5), "UNSUPPORTED": Fraction(0)}
PRIORITY = {"required": 2, "normal": 1}

@dataclass(frozen=True)
class Requirement:
    label: str
    dimension: str
    priority: str = "normal"
    evidence: str = "UNSUPPORTED"

@dataclass(frozen=True)
class FitResult:
    final_score: int
    uncapped_score: Fraction
    capped_score: Fraction
    cap_applied: bool
    applicable_max: Fraction
    specialization_bonus: Fraction
    title_contribution: Fraction
    dimensions: dict[str, Fraction]
    requirements: tuple[dict[str, Any], ...]

def _check(dimension: str, evidence: str, priority: str) -> None:
    if dimension not in DIMENSIONS: raise ValueError(f"unknown dimension: {dimension}")
    if evidence not in EVIDENCE: raise ValueError(f"unknown evidence class: {evidence}")
    if priority not in PRIORITY: raise ValueError(f"unknown priority: {priority}")

def score(requirements: list[Requirement], title_evidence: str = "UNSUPPORTED",
          hard_family_mismatch: bool = False, direct_title_specialization: bool = False) -> FitResult:
    if title_evidence not in EVIDENCE: raise ValueError(f"unknown evidence class: {title_evidence}")
    for r in requirements: _check(r.dimension, r.evidence, r.priority)
    title = Fraction(29, 4) * EVIDENCE[title_evidence]
    applicable_max = Fraction(29, 4)
    traces, dimensions = [], {}
    for dimension, budget in DIMENSIONS.items():
        rows = [r for r in requirements if r.dimension == dimension]
        if dimension == "role_seniority": budget -= Fraction(29, 4)
        if rows: applicable_max += budget
        total = sum((PRIORITY[r.priority] for r in rows), 0)
        dimensions[dimension] = ((budget * sum((PRIORITY[r.priority] * EVIDENCE[r.evidence] for r in rows), Fraction()) / total) if total else Fraction()) + (title if dimension == "role_seniority" else Fraction())
        for r in rows:
            traces.append({"label": r.label, "dimension": r.dimension, "priority_weight": PRIORITY[r.priority], "evidence_class": r.evidence, "contribution": budget * PRIORITY[r.priority] * EVIDENCE[r.evidence] / total if total else Fraction()})
    scale = Fraction(100) / applicable_max
    dimensions = {d: value * scale for d, value in dimensions.items()}
    for trace in traces: trace["contribution"] *= scale
    title *= scale
    uncapped = sum(dimensions.values(), Fraction())
    specialization_bonus = Fraction(3) if direct_title_specialization else Fraction()
    post_specialization = min(Fraction(100), uncapped + specialization_bonus)
    capped = min(post_specialization, Fraction(40)) if hard_family_mismatch else post_specialization
    return FitResult((capped.numerator * 2 // capped.denominator + 1) // 2, uncapped, capped, hard_family_mismatch and capped < post_specialization, applicable_max, specialization_bonus, title, dimensions, tuple(traces))
