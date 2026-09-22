"""One bounded Scale-Up acquisition -> shared Jobs reconciliation."""
from __future__ import annotations
import json
from pathlib import Path
from datetime import date
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.newsletter_adapter import HttpClientFetcher, NewsletterAdapterConfig, NewsletterJobsAdapter, _adapt_all
from lifeos.jobs.newsletter_contract import ingest
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
    adapter = NewsletterJobsAdapter(NewsletterAdapterConfig(fetcher=HttpClientFetcher(http=http, context=context), fit_profile=fit_profile, market=market, source_lane=source_lane))
    candidates = _adapt_all(acquired.observations, adapter=adapter, context=context, max_workers=8)
    results = ingest(candidates, lane=lane, lane_priority=lane_priority, repository=repository, run_date=date.today(), context=context)
    return acquired, results
