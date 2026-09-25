"""One bounded Scale-Up acquisition -> shared Jobs reconciliation."""
from __future__ import annotations
import json
from dataclasses import replace
from pathlib import Path
from datetime import date
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter, TerminalEvidenceCache, _adapt_all
from lifeos.jobs.terminal_evidence import browser_evidence, fallback_fetcher
from lifeos.jobs.newsletter_contract import Disposition, ingest
from lifeos.jobs.notion_repository import NotionCareerRepository, NotionCareerRepositoryConfig
from lifeos.jobs.qualification import LaneConfig
from lifeos.jobs.scale_up_acquisition import ScaleUpAcquirer
REGISTRY = Path(__file__).resolve().parents[2] / "contracts" / "scale_up_sources.json"
def execute_scale_up(*, context, http, notion, data_source_id: str, lane: LaneConfig, lane_priority: dict[str, int], fit_profile: FitProfile, market: str, source_lane: str, recovery_evidence: dict | None = None):
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    for source in registry["sources"]:
        if recovery_evidence and source["company"] in recovery_evidence:
            source["recovery_evidence"] = recovery_evidence[source["company"]]
    acquired = ScaleUpAcquirer(context=context, http=http).acquire(registry)
    repository = NotionCareerRepository(transport=notion, config=NotionCareerRepositoryConfig(data_source_id=data_source_id))
    fallback = fallback_fetcher(context, recovery_evidence or browser_evidence())
    terminal_cache = TerminalEvidenceCache()
    adapter = NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=HttpClientFetcher(http=http, context=context), fallback_fetcher=fallback, fit_profile=fit_profile, market=market, source_lane=source_lane), terminal_cache=terminal_cache)
    # Pass 1 — identity: persist canonical identity before any terminal HTTP resolution
    initial_candidates = [adapter.to_identity_candidate(obs) for obs in acquired.observations]
    initial_results = ingest(initial_candidates, lane=lane, lane_priority=lane_priority, repository=repository, run_date=date.today(), context=context)
    initial_by_ref = {c.evidence_ref: r for c, r in zip(initial_candidates, initial_results)}
    persistence_verified = {obs.evidence_ref for obs in acquired.observations if initial_by_ref.get(obs.evidence_ref) and initial_by_ref[obs.evidence_ref].persistence_verified}
    enrichment_observations = tuple(obs for obs in acquired.observations if obs.evidence_ref in persistence_verified and not initial_by_ref[obs.evidence_ref].terminal_evidence_satisfied)
    # Pass 2 — terminal evidence + reconciliation
    if not enrichment_observations:
        enrichment_candidates = []
    else:
        enrichment_candidates = _adapt_all(enrichment_observations, adapter=adapter, context=context, max_workers=8)
    enrichment_candidates = [
        replace(c, job=replace(c.job, canonical_identity=initial_by_ref[c.evidence_ref].stable_job_key), unresolved_reason=None, fit_reason="terminal_unresolved")
        if c.unresolved_reason and initial_by_ref[c.evidence_ref].stable_job_key
        else c
        for c in enrichment_candidates
    ]
    enrichment_results = ingest(enrichment_candidates, lane=lane, lane_priority=lane_priority, repository=repository, run_date=date.today(), context=context)
    # Merge: enrichment overrides; terminal-already-satisfied become UPDATED
    final_by_ref = dict(initial_by_ref)
    final_by_ref.update({c.evidence_ref: r for c, r in zip(enrichment_candidates, enrichment_results)})
    enriched_refs = {c.evidence_ref for c in enrichment_candidates}
    for obs in acquired.observations:
        ref = obs.evidence_ref
        initial = initial_by_ref.get(ref)
        if initial and initial.terminal_evidence_satisfied and ref not in enriched_refs:
            final_by_ref[ref] = replace(initial, disposition=Disposition.UPDATED)
    return acquired, [final_by_ref[obs.evidence_ref] for obs in acquired.observations]
