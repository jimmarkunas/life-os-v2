#!/usr/bin/env python3
"""Operator entry point for one bounded Scale-Up runtime."""
from __future__ import annotations
import argparse, json, os
from collections import Counter
from datetime import datetime, timezone
from dataclasses import replace
from lifeos.core.config import ConfigField, RuntimeConfig
from lifeos.core.http import HttpClient
from lifeos.core.runtime import RunContext
from lifeos.integrations.notion import NotionTransport, NotionIdentityQuery
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.qualification import LaneConfig, UNIVERSAL_FIT_FLOOR
from lifeos.jobs.scale_up_runtime import execute_scale_up
from lifeos.jobs.newsletter_contract import Disposition
from scripts.run_us_remote_production import _parse_private_policy
from lifeos.jobs.scale_up_acquisition import KINDS

def _failure_summary(acquired, results):
    degraded = [result for result in results if result.disposition is Disposition.REVIEW_DEGRADED]
    reasons = Counter(result.detail or result.disposition.value for result in degraded)
    return {
        "sources": len(acquired.sources),
        "observations": len(acquired.observations),
        "complete": acquired.complete and not degraded,
        "non_complete_sources": [
            {"company": source.company, "state": source.state,
             "candidate_count": source.candidate_count, "detail": source.detail, "diagnostic": getattr(source, "diagnostic", None)}
            for source in acquired.sources if source.state != "COMPLETE"
        ],
        "ingest_results": len(results),
        "degraded_ingest": len(degraded),
        "degraded_ingest_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reasons.items())
        ],
        "identity_failures": [result.diagnostic for result in degraded if getattr(result, "diagnostic", None) and "missing" in result.diagnostic],
        "fit_unresolved": [result.diagnostic for result in degraded if getattr(result, "diagnostic", None) and "fit_evidence" in result.diagnostic],
        "readback_mismatches": [result.diagnostic for result in degraded if getattr(result, "diagnostic", None) and "mismatches" in result.diagnostic],
    }

def _file_url(page, name):
    files=((page.get("properties") or {}).get(name) or {}).get("files") or []
    if len(files)!=1: raise ValueError(f"Scale-up {name} is missing or ambiguous")
    item=files[0]; payload=item.get("file") if item.get("type")=="file" else item.get("external")
    if not isinstance(payload,dict) or not payload.get("url"): raise ValueError(f"Scale-up {name} is unreadable")
    return str(payload["url"])

def _load_inputs(context,http,notion,token,slot,nonce):
    search=http.request_json(context,"POST","https://api.notion.com/v1/search",headers={"Authorization":f"Bearer {token}","Notion-Version":"2026-03-11","Content-Type":"application/json"},json_body={"query":"Job Lane Configuration","filter":{"property":"object","value":"data_source"},"page_size":20},timeout_seconds=10)
    matches=[x for x in search.get("results",[]) if "Job Lane Configuration"=="".join(p.get("plain_text","") for p in x.get("title",[]))]
    if len(matches)!=1: raise ValueError("Job Lane Configuration is not uniquely resolvable")
    rows=notion.query_data_source(matches[0]["id"],NotionIdentityQuery(property_name="Lane",property_type="title",values=("Scale-up",)),page_size=10)
    if len(rows)!=1: raise ValueError("Scale-up lane row is not uniquely resolvable")
    row=rows[0]
    policy=json.loads(http.request(context,"GET",_file_url(row,"Private Policy File"),timeout_seconds=10).body)
    evidence=json.loads(http.request(context,"GET",_file_url(row,"Current Recovery Evidence"),timeout_seconds=10).body)
    lane,priority,profile,market,source_lane = _parse_private_policy(policy)
    if (lane.name != "Scale-Up" or lane.market != "UK" or lane.fit_floor != UNIVERSAL_FIT_FLOOR or lane.compensation_floor is not None or lane.freshness_gate or market != "UK" or source_lane != "Scale-Up" or "Scale-Up" not in priority): raise ValueError("private Scale-up policy is incompatible")
    dimensions=("role_seniority","functional","technical_platform","delivery_complexity","competitive_advantage")
    evidence_classes=("DIRECT","ADJACENT","METHOD_EQUIVALENT","UNSUPPORTED")
    if (profile.model_version != "V3" or not any(profile.title_patterns.values()) or not profile.direct_specialization_patterns or any(not profile.dimension_patterns.get(key) for key in dimensions) or any(not profile.evidence_patterns.get(key) for key in evidence_classes) or not profile.hard_family_patterns): raise ValueError("private Fit policy is non-scoring or incompatible")
    required={"google_web","linkedin_jobs"}; handoffs=evidence.get("handoffs",[]) if isinstance(evidence,dict) else []
    expected={x["company"] for x in json.loads((__import__("pathlib").Path(__file__).resolve().parents[1]/"contracts/scale_up_sources.json").read_text())["sources"] if x["source_type"] in {"provider_html","generic_html"}}
    companies=[x.get("company") for x in handoffs]
    if evidence.get("scheduled_slot")!=slot or evidence.get("trigger_nonce")!=nonce or len(handoffs)!=12 or set(companies)!=expected or len(set(companies))!=12: raise ValueError("recovery evidence correlation or company universe invalid")
    now=datetime.now(timezone.utc)
    for item in handoffs:
        if set(item.get("channels",[]))!=required or not item.get("searched_at"): raise ValueError("recovery evidence channel/timestamp coverage incomplete")
        observed=datetime.fromisoformat(str(item["searched_at"]).replace("Z","+00:00"))
        if (now-observed).total_seconds() < 0 or (now-observed).total_seconds() > 86400: raise ValueError("recovery evidence is stale")
    lane = replace(lane, visa_route="Scale-up", visa_route_gate=True, geography_gate=True)
    return lane,priority,profile,market,source_lane,{x["company"]:{"channels":x["channels"],"state":x.get("state","INCOMPLETE"),"candidates":x.get("candidates",[]),"authoritative_zero":x.get("authoritative_zero",False)} for x in handoffs}
def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--timeout-seconds",type=float,default=300); args=parser.parse_args()
    config=RuntimeConfig.load(tuple(ConfigField(n) for n in ("NOTION_API_TOKEN", "NOTION_JOB_LEDGER_DATA_SOURCE_ID")))
    context=RunContext.start(now=datetime.now(timezone.utc), timeout_seconds=args.timeout_seconds); http=HttpClient()
    notion=NotionTransport(context=context,http=http,access_token=config.require("NOTION_API_TOKEN"))
    lane,priority,profile,market,source_lane,evidence=_load_inputs(context,http,notion,config.require("NOTION_API_TOKEN"),os.environ.get("SCHEDULED_SLOT",""),os.environ.get("TRIGGER_NONCE",""))
    acquired, results=execute_scale_up(context=context,http=http,notion=notion,data_source_id=config.require("NOTION_JOB_LEDGER_DATA_SOURCE_ID"),lane=lane,lane_priority=priority,fit_profile=profile,market=market,source_lane=source_lane,recovery_evidence=evidence)
    summary = _failure_summary(acquired, results)
    if not summary["complete"]:
        print(json.dumps(summary, separators=(",", ":")))
    else:
        print(json.dumps({key: summary[key] for key in ("sources", "observations", "complete", "ingest_results", "degraded_ingest")}))
    return 0 if summary["complete"] else 1
if __name__ == "__main__": raise SystemExit(main())
