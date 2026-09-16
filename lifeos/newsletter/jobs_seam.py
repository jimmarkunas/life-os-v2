"""Single narrow seam from source observations into the Jobs-owned contract.

The concrete adapter belongs with/in integration of lifeos/jobs/**. Agent 2 does
not duplicate Jobs models, Stable Job Keys, qualification, lifecycle, or persistence.
"""
from __future__ import annotations
from typing import Protocol, TypeVar
from .models import SourceVacancyObservation
JobsCandidateT = TypeVar("JobsCandidateT")
class JobsCandidateAdapter(Protocol[JobsCandidateT]):
    def to_jobs_candidate(self, observation: SourceVacancyObservation) -> JobsCandidateT: ...
def adapt_for_jobs(observations: tuple[SourceVacancyObservation, ...], adapter: JobsCandidateAdapter[JobsCandidateT]) -> tuple[JobsCandidateT, ...]:
    return tuple(adapter.to_jobs_candidate(observation) for observation in observations)
