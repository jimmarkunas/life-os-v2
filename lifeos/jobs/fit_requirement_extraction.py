"""Pure injected-rule extraction of structured Jobs requirements."""
from __future__ import annotations
from dataclasses import dataclass
import re
from lifeos.jobs.fit_arithmetic import Requirement

DIMENSIONS = ("role_seniority", "functional", "technical_platform", "delivery_complexity", "competitive_advantage")
EVIDENCE = ("DIRECT", "ADJACENT", "METHOD_EQUIVALENT", "UNSUPPORTED")

@dataclass(frozen=True)
class ExtractionResult:
    requirements: tuple[Requirement, ...]
    hard_family_mismatch: bool

def _match(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)

def extract_requirements(raw_text: str, dimension_patterns: dict[str, tuple[str, ...]],
                         evidence_patterns: dict[str, tuple[str, ...]], material_patterns: tuple[str, ...],
                         ignore_patterns: tuple[str, ...] = (), hard_family_patterns: tuple[str, ...] = ()) -> ExtractionResult:
    rows, seen, mismatch = [], set(), False
    for clause in re.split(r"(?:\r?\n+|[.!?;]+)", raw_text):
        clause = re.sub(r"^\s*[-*•]\s*", "", clause).strip()
        key = re.sub(r"\W+", " ", clause.lower()).strip()
        if not clause or key in seen or _match(ignore_patterns, clause): continue
        hits = {d for d in DIMENSIONS if _match(dimension_patterns.get(d, ()), clause)}
        material = _match(material_patterns, clause) or hits or any(_match(v, clause) for v in evidence_patterns.values())
        if not material: continue
        if not hits: raise ValueError(f"unclassified material clause: {clause}")
        dimension = next(d for d in DIMENSIONS if d in hits)
        priority = "required" if re.search(r"\b(required|must(?:-have)?|minimum|mandatory)\b|\bat least \d+ years?\b", clause, re.I) else "normal"
        evidence = next((e for e in EVIDENCE if _match(evidence_patterns.get(e, ()), clause)), "UNSUPPORTED")
        rows.append(Requirement(clause, dimension, priority, evidence)); seen.add(key)
    return ExtractionResult(tuple(rows), mismatch)
