#!/usr/bin/env python3
"""Operator entry point for one bounded Scale-Up runtime."""
from __future__ import annotations
import argparse, json, os
from datetime import datetime, timezone
from lifeos.core.config import ConfigField, RuntimeConfig
from lifeos.core.http import HttpClient
from lifeos.core.runtime import RunContext
from lifeos.integrations.notion import NotionTransport, NotionIdentityQuery
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.qualification import LaneConfig, UNIVERSAL_FIT_FLOOR
from lifeos.jobs.scale_up_runtime import execute_scale_up
from lifeos.jobs.newsletter_contract import Disposition
from scripts.run_us_remote_production import _parse_private_policy

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
    _,_,profile,_,_= _parse_private_policy(policy)
    required={"google_web","linkedin_jobs"}; handoffs=evidence.get("handoffs",[]) if isinstance(evidence,dict) else []
    if evidence.get("scheduled_slot")!=slot or evidence.get("trigger_nonce")!=nonce or len(handoffs)!=12: raise ValueError("recovery evidence correlation or handoff count invalid")
    now=datetime.now(timezone.utc)
    for item in handoffs:
        if set(item.get("channels",[]))!=required or not item.get("searched_at"): raise ValueError("recovery evidence channel/timestamp coverage incomplete")
        observed=datetime.fromisoformat(str(item["searched_at"]).replace("Z","+00:00"))
        if (now-observed).total_seconds() < 0 or (now-observed).total_seconds() > 86400: raise ValueError("recovery evidence is stale")
    return profile,{x["company"]:{"channels":x["channels"],"state":x.get("state","INCOMPLETE"),"candidates":x.get("candidates",[])} for x in handoffs}
def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--timeout-seconds",type=float,default=300); parser.add_argument("--recovery-evidence-json", default=os.environ.get("SCALE_UP_RECOVERY_EVIDENCE_JSON")); args=parser.parse_args()
    config=RuntimeConfig.load(tuple(ConfigField(n) for n in ("NOTION_API_TOKEN", "NOTION_JOB_LEDGER_DATA_SOURCE_ID")))
    context=RunContext.start(now=datetime.now(timezone.utc), timeout_seconds=args.timeout_seconds); http=HttpClient()
    notion=NotionTransport(context=context,http=http,access_token=config.require("NOTION_API_TOKEN"))
    lane=LaneConfig("Scale-Up","UK",UNIVERSAL_FIT_FLOOR,None,"any",None,False,None,True)
    profile,evidence=_load_inputs(context,http,notion,config.require("NOTION_API_TOKEN"),os.environ.get("SCHEDULED_SLOT",""),os.environ.get("TRIGGER_NONCE",""))
    acquired, results=execute_scale_up(context=context,http=http,notion=notion,data_source_id=config.require("NOTION_JOB_LEDGER_DATA_SOURCE_ID"),lane=lane,lane_priority={"Scale-Up":0},fit_profile=profile,market="UK",source_lane="Scale-Up",recovery_evidence=evidence)
    degraded = sum(result.disposition is Disposition.REVIEW_DEGRADED for result in results)
    complete = acquired.complete and degraded == 0
    print(json.dumps({"sources":len(acquired.sources),"observations":len(acquired.observations),"complete":complete,"ingest_results":len(results),"degraded_ingest":degraded}))
    return 0 if complete else 1
if __name__ == "__main__": raise SystemExit(main())
