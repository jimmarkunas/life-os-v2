"""Generic, deterministic LIFE OS Fit evaluator.

MIGRATE/REFACTOR from v1 `jobs/fit_scoring.py`, generalized per D-011: v1's
scoring *mechanics* (a role-family base score, additive capped scope-term
groups, and penalty terms, combined into one deterministic 0-100 score with
an auditable rationale string) are proven and reused. v1's actual content
-- specific role-family regexes (Technical Program Manager, etc.), specific
scope terms, and specific penalty terms -- was Jim's personal career
profile and is NOT ported. It never enters this public repository.

All of that content now lives in an injected `FitProfile`, supplied at
trusted runtime from private configuration (see D-011). This module
contains zero personal career data; `FitProfile` is a plain, generic,
serializable shape any user's private profile can populate. Tests use a
synthetic fake profile with made-up terms, never Jim's real criteria.

Provider match percentages/scores are evidence only (see NormalizedCandidate/
Job.provider_job_id-adjacent provider_score handling in the adapter) and
never contribute points to the computed score -- see `score()`'s deliberate
omission of any provider-score input.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RoleFamily:
    """One recognizable role family: if any pattern matches the role text,
    `base_score` applies. Ordered list; first match wins."""

    patterns: tuple[str, ...]
    base_score: int
    label: str


@dataclass(frozen=True)
class ScopeCategory:
    """One additive, capped scoring dimension (e.g. "technical scope",
    "domain scope") built from term groups. Each group that matches
    contributes its points, up to `cap` total for the category."""

    name: str
    term_groups: tuple[tuple[tuple[str, ...], int, str], ...]
    """Each entry: (terms, points, reason). A group scores if ANY of its
    terms appears in the evaluated text."""
    cap: int


@dataclass(frozen=True)
class PenaltyRule:
    """Subtract `penalty` points when at least `min_hits` of `terms` appear."""

    terms: tuple[str, ...]
    penalty: int
    reason: str
    min_hits: int = 1


@dataclass(frozen=True)
class FitProfile:
    """Private, injected career-fit configuration. Real values are supplied
    by trusted runtime configuration (never hardcoded here -- D-011)."""

    model_version: str
    role_families: tuple[RoleFamily, ...]
    default_role_base: int
    default_role_label: str
    scope_categories: tuple[ScopeCategory, ...] = ()
    penalties: tuple[PenaltyRule, ...] = ()


@dataclass(frozen=True)
class FitEvidence:
    """Text evidence a Fit score is computed from. Sourced from Jobs-owned
    terminal evidence (role, resolved description) -- never provider-
    supplied scoring, only provider-supplied facts."""

    role: str
    description_text: str = ""
    location_text: str = ""


@dataclass(frozen=True)
class FitResult:
    score: int
    rationale: str
    model_version: str


def _evaluated_text(evidence: FitEvidence) -> str:
    return " ".join(
        part for part in (evidence.role, evidence.description_text, evidence.location_text) if part
    ).lower()


def _role_base(role: str, profile: FitProfile) -> tuple[int, str]:
    padded = f" {role.lower()} "
    for family in profile.role_families:
        if any(re.search(pattern, padded, re.I) for pattern in family.patterns):
            return family.base_score, family.label
    return profile.default_role_base, profile.default_role_label


def _scope_points(text: str, category: ScopeCategory) -> tuple[int, list[str]]:
    points = 0
    reasons: list[str] = []
    for terms, value, reason in category.term_groups:
        if any(term in text for term in terms):
            points += value
            reasons.append(reason)
    return min(points, category.cap), reasons


def _penalty_points(role: str, text: str, rules: tuple[PenaltyRule, ...]) -> tuple[int, list[str]]:
    total = 0
    reasons: list[str] = []
    for rule in rules:
        hits = sum(1 for term in rule.terms if term in role or term in text)
        if hits >= rule.min_hits:
            total += rule.penalty
            reasons.append(rule.reason)
    return total, reasons


def score(evidence: FitEvidence, *, profile: FitProfile) -> FitResult:
    """Deterministic 0-100 score with an auditable rationale. Never accepts
    or considers a provider-supplied score -- see module docstring."""
    role = evidence.role.strip().lower()
    text = _evaluated_text(evidence)

    base, base_label = _role_base(role, profile)
    category_points: dict[str, int] = {}
    category_reasons: list[str] = []
    for category in profile.scope_categories:
        points, reasons = _scope_points(text, category)
        category_points[category.name] = points
        category_reasons.extend(reasons)

    penalty, penalty_reasons = _penalty_points(role, text, profile.penalties)

    total = base + sum(category_points.values()) - penalty
    total = max(0, min(100, total))

    component_summary = " + ".join(f"{name} {points}" for name, points in category_points.items())
    components = f"role {base}" + (f" + {component_summary}" if component_summary else "") + f" - penalty {penalty}"
    reasons = [f"{base_label} alignment", *category_reasons, *penalty_reasons]
    compact: list[str] = []
    for reason in reasons:
        if reason and reason not in compact:
            compact.append(reason)
    rationale = f"LIFE OS Fit v{profile.model_version}: {components}. " + "; ".join(compact[:5]) + "."
    return FitResult(score=int(total), rationale=rationale, model_version=profile.model_version)
