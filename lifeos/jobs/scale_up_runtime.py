"""One bounded Scale-Up acquisition -> shared Jobs reconciliation."""
from __future__ import annotations
import json
from dataclasses import replace
from pathlib import Path
from datetime import date
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter, _adapt_all
from lifeos.jobs.terminal_evidence import browser_evidence, fallback_fetcher
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.models import EvidenceStatus
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.scale_up_acquisition import ScaleUpAcquirer
REGISTRY = Path(__file__).resolve().parents[2] / "contracts" / "scale_up_sources.json"
def _route_status(value: str | None) -> EvidenceStatus:
    try:
        return EvidenceStatus(str(value or "").upper())
    except ValueError:
        return EvidenceStatus.UNRESOLVED


def _geography_status(location: str | None) -> EvidenceStatus:
    text = (location or "").casefold()
    if not text:
        return EvidenceStatus.UNRESOLVED
    if "ontario" in text or "canada" in text:
        return EvidenceStatus.NEGATIVE
    if "london" in text or "greater london" in text:
        return EvidenceStatus.POSITIVE
    if any(place in text for place in ("manchester", "paris", "new york")):
        return EvidenceStatus.NEGATIVE
    return EvidenceStatus.UNRESOLVED

def execute_scale_up(*, context, http, notion, data_source_id: str, lane: LaneConfig, lane_priority: dict[str, int], fit_profile: FitProfile, market: str, source_lane: str, recovery_evidence: dict | None = None):
    lane = replace(lane, visa_route="Scale-up", visa_route_gate=True, geography_gate=True)
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    for source in registry["sources"]:
        if recovery_evidence and source["company"] in recovery_evidence:
            source["recovery_evidence"] = recovery_evidence[source["company"]]
    acquired = ScaleUpAcquirer(context=context, http=http).acquire(registry)
    repository = NotionCareerRepository(transport=notion, config=NotionCareerRepositoryConfig(data_source_id=data_source_id))
    fallback = fallback_fetcher(context, recovery_evidence or browser_evidence())
    adapter = NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=HttpClientFetcher(http=http, context=context), fallback_fetcher=fallback, fit_profile=fit_profile, market=market, source_lane=source_lane))
    initial_candidates = [adapter.to_identity_candidate(obs) for obs in acquired.observations]
    initial_results = ingest(initial_candidates, lane=lane, lane_priority=lane_priority, repository=repository, run_date=date.today(), context=context)
    initial_by_ref = {c.evidence_ref: r for c, r in zip(initial_candidates, initial_results)}
    persistence_verified = {obs.evidence_ref for obs in acquired.observations if initial_by_ref.get(obs.evidence_ref) and initial_by_ref[obs.evidence_ref].persistence_verified}
    enrichment_observations = tuple(obs for obs in acquired.observations if obs.evidence_ref in persistence_verified and not initial_by_ref[obs.evidence_ref].terminal_evidence_satisfied)
    enrichment_candidates = _adapt_all(enrichment_observations, adapter=adapter, context=context, max_workers=8)
    enrichment_candidates = [
        replace(
            c,
            job=replace(c.job, canonical_identity=initial_by_ref[c.evidence_ref].stable_job_key),
            unresolved_reason=None if c.unresolved_reason else c.unresolved_reason,
            fit_reason="terminal_unresolved" if c.unresolved_reason else c.fit_reason,
            route_evidence_status=_route_status(next(obs for obs in acquired.observations if obs.evidence_ref == c.evidence_ref).route_evidence_status),
            geography_evidence_status=_geography_status(c.job.location),
        )
        for c in enrichment_candidates
    ]
    enrichment_results = ingest(enrichment_candidates, lane=lane, lane_priority=lane_priority, repository=repository, run_date=date.today(), context=context)
    final_by_ref = dict(initial_by_ref)
    final_by_ref.update({c.evidence_ref: r for c, r in zip(enrichment_candidates, enrichment_results)})
    enriched_refs = {c.evidence_ref for c in enrichment_candidates}
    final_by_ref.update({obs.evidence_ref: replace(initial_by_ref[obs.evidence_ref], disposition=Disposition.UPDATED) for obs in acquired.observations if initial_by_ref[obs.evidence_ref].terminal_evidence_satisfied and obs.evidence_ref not in enriched_refs})
    return acquired, [final_by_ref[obs.evidence_ref] for obs in acquired.observations]
