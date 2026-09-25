"""Greenfield POC: Greenhouse authoritative JD -> requirements -> LIFE OS Fit.

Non-production by design. No network, scheduler, browser, Notion, or Job Ledger
dependencies. The only input is a Greenhouse jobs?content=true payload plus an
injected Fit profile.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from fractions import Fraction
from html import unescape
from html.parser import HTMLParser
import re
from typing import Any


DIMENSIONS = {
    "role_seniority": Fraction(29),
    "functional": Fraction(29),
    "technical_platform": Fraction(21),
    "delivery_complexity": Fraction(14),
    "competitive_advantage": Fraction(7),
}
EVIDENCE = {
    "DIRECT": Fraction(1),
    "ADJACENT": Fraction(3, 4),
    "METHOD_EQUIVALENT": Fraction(3, 5),
    "UNSUPPORTED": Fraction(0),
}
PRIORITY = {"required": 2, "normal": 1}
TITLE_CLASSES = ("DIRECT", "ADJACENT", "METHOD_EQUIVALENT", "UNSUPPORTED")


@dataclass(frozen=True)
class GreenhouseJob:
    provider: str
    provider_job_id: str
    company: str
    title: str
    location: str
    apply_url: str
    jd_text: str
    evidence_kind: str = "employer_ats_jd"
    evidence_authority: str = "authoritative_provider_api"


@dataclass(frozen=True)
class Requirement:
    label: str
    dimension: str
    priority: str
    evidence: str


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


def _html_to_text(value: str) -> str:
    # Greenhouse content=true can be entity-encoded more than once.
    decoded = unescape(unescape(value or ""))
    parser = _TextExtractor()
    parser.feed(decoded)
    return "\n".join(parser.parts).strip()


def parse_greenhouse_job(payload: dict[str, Any], *, job_id: str | int | None = None) -> GreenhouseJob:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Greenhouse payload has no jobs")

    chosen = None
    wanted = str(job_id) if job_id is not None else None
    for item in jobs:
        if not isinstance(item, dict):
            continue
        if wanted is None or str(item.get("id")) == wanted:
            chosen = item
            break
    if chosen is None:
        raise ValueError(f"Greenhouse job {wanted} not found")

    title = str(chosen.get("title") or "").strip()
    apply_url = str(chosen.get("absolute_url") or "").strip()
    raw_content = str(chosen.get("content") or "")
    jd_text = _html_to_text(raw_content)
    location_obj = chosen.get("location") or {}
    location = str(location_obj.get("name") or "").strip() if isinstance(location_obj, dict) else ""
    company = str(chosen.get("company_name") or payload.get("company_name") or "").strip()

    if not title:
        raise ValueError("Greenhouse job missing title")
    if not apply_url.startswith(("https://", "http://")):
        raise ValueError("Greenhouse job missing actionable Apply URL")
    if len(jd_text) < 80:
        raise ValueError("Greenhouse job missing authoritative full JD")

    return GreenhouseJob(
        provider="greenhouse",
        provider_job_id=str(chosen.get("id") or ""),
        company=company,
        title=title,
        location=location,
        apply_url=apply_url,
        jd_text=jd_text,
    )


def _matches(patterns: list[str] | tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _classify_title(title: str, profile: dict[str, Any]) -> tuple[str, bool]:
    patterns = profile["title_patterns"]
    normalized = re.sub(r"[^\w]+", " ", re.sub("programme", "Program", title, flags=re.IGNORECASE)).strip()
    evidence = next(
        (
            kind
            for kind in TITLE_CLASSES
            if _matches(patterns.get(kind, ()), normalized)
        ),
        "UNSUPPORTED",
    )
    direct_specialization = _matches(profile.get("direct_specialization_patterns", ()), normalized)
    return evidence, direct_specialization


def extract_requirements(jd_text: str, profile: dict[str, Any]) -> list[Requirement]:
    dimension_patterns = profile["dimension_patterns"]
    evidence_patterns = profile["evidence_patterns"]
    material_patterns = profile.get("material_patterns", ())
    ignore_patterns = profile.get("ignore_patterns", ())
    rows: list[Requirement] = []
    seen: set[str] = set()

    for clause in re.split(r"(?:\r?\n+|[.!?;]+)", jd_text):
        clause = re.sub(r"^\s*[-*•]\s*", "", clause).strip()
        key = re.sub(r"\W+", " ", clause.casefold()).strip()
        if not clause or key in seen or _matches(ignore_patterns, clause):
            continue

        hits = [
            dimension
            for dimension in DIMENSIONS
            if _matches(dimension_patterns.get(dimension, ()), clause)
        ]
        material = (
            _matches(material_patterns, clause)
            or bool(hits)
            or any(_matches(patterns, clause) for patterns in evidence_patterns.values())
        )
        if not material or not hits:
            continue

        dimension = hits[0]
        priority = (
            "required"
            if re.search(r"\b(required|must(?:-have)?|minimum|mandatory)\b|\bat least \d+ years?\b|\b\d+\+ years\b", clause, re.I)
            else "normal"
        )
        evidence = next(
            (
                kind
                for kind in TITLE_CLASSES
                if _matches(evidence_patterns.get(kind, ()), clause)
            ),
            "UNSUPPORTED",
        )
        rows.append(Requirement(clause, dimension, priority, evidence))
        seen.add(key)

    return rows


def calculate_fit(title: str, requirements: list[Requirement], profile: dict[str, Any]) -> dict[str, Any]:
    if not title.strip():
        raise ValueError("missing title")
    if not requirements:
        raise ValueError("missing scoreable JD requirements")

    title_evidence, direct_specialization = _classify_title(title, profile)
    title_points = Fraction(29, 4) * EVIDENCE[title_evidence]
    applicable_max = Fraction(29, 4)
    dimensions: dict[str, Fraction] = {}

    for dimension, budget in DIMENSIONS.items():
        rows = [row for row in requirements if row.dimension == dimension]
        if dimension == "role_seniority":
            budget -= Fraction(29, 4)
        if rows:
            applicable_max += budget
        total_priority = sum(PRIORITY[row.priority] for row in rows)
        earned = (
            budget
            * sum(
                (PRIORITY[row.priority] * EVIDENCE[row.evidence] for row in rows),
                Fraction(),
            )
            / total_priority
            if total_priority
            else Fraction()
        )
        if dimension == "role_seniority":
            earned += title_points
        dimensions[dimension] = earned

    scale = Fraction(100) / applicable_max
    scaled_dimensions = {name: value * scale for name, value in dimensions.items()}
    score = sum(scaled_dimensions.values(), Fraction())
    if direct_specialization:
        score = min(Fraction(100), score + 3)

    hard_family_mismatch = _matches(profile.get("hard_family_patterns", ()), title)
    if hard_family_mismatch:
        score = min(score, Fraction(40))

    final_score = (score.numerator * 2 // score.denominator + 1) // 2
    return {
        "score": final_score,
        "authority": "AUTHORITATIVE",
        "evidence_kind": "employer_ats_jd",
        "title_evidence": title_evidence,
        "direct_specialization": direct_specialization,
        "hard_family_mismatch": hard_family_mismatch,
        "dimensions": {name: float(value) for name, value in scaled_dimensions.items()},
    }


def evaluate_greenhouse_job(
    payload: dict[str, Any],
    profile: dict[str, Any],
    *,
    job_id: str | int | None = None,
) -> dict[str, Any]:
    job = parse_greenhouse_job(payload, job_id=job_id)
    requirements = extract_requirements(job.jd_text, profile)
    fit = calculate_fit(job.title, requirements, profile)
    return {
        "job": asdict(job),
        "requirements": [asdict(row) for row in requirements],
        "fit": fit,
        "network_fetches": 0,
        "browser_fetches": 0,
    }
