"""Pure scoreability gate and authority mapping for structured Fit inputs."""
from __future__ import annotations

from dataclasses import dataclass

from lifeos.jobs.fit_arithmetic import FitResult, Requirement, score
from lifeos.jobs.fit_title_semantics import TitleSemantics
from lifeos.jobs.models import FitAuthority, FitEvidenceKind

@dataclass(frozen=True)
class ScoreabilityResult:
    fit_result: FitResult | None
    authority: FitAuthority | None
    reason: str | None

def compile_fit(*, title: str, title_semantics: TitleSemantics,
                requirements: list[Requirement], evidence_kind: FitEvidenceKind,
                hard_family_mismatch: bool = False) -> ScoreabilityResult:
    if not title.strip(): return ScoreabilityResult(None, None, "missing_title")
    authorities = {FitEvidenceKind.EMPLOYER_ATS_JD: FitAuthority.AUTHORITATIVE,
                   FitEvidenceKind.SOURCE_DESCRIPTION: FitAuthority.NON_AUTHORITATIVE}
    if evidence_kind not in authorities: return ScoreabilityResult(None, None, "missing_fit_evidence")
    if not requirements: return ScoreabilityResult(None, None, "missing_scoreable_jd_requirements")
    result = score(requirements, title_evidence=title_semantics.evidence_class,
                   hard_family_mismatch=hard_family_mismatch,
                   direct_title_specialization=title_semantics.direct_specialization)
    return ScoreabilityResult(result, authorities[evidence_kind], None)
