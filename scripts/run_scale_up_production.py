#!/usr/bin/env python3
"""Operator entry point for one bounded Scale-Up runtime."""
from __future__ import annotations
import argparse, json, os
from datetime import datetime, timezone
from lifeos.core.config import ConfigField, RuntimeConfig
from lifeos.core.http import HttpClient
from lifeos.core.runtime import RunContext
from lifeos.integrations.notion import NotionTransport
from lifeos.jobs.fit_scoring import FitProfile
from lifeos.jobs.qualification import LaneConfig, UNIVERSAL_FIT_FLOOR
from lifeos.jobs.scale_up_runtime import execute_scale_up
def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--timeout-seconds",type=float,default=300); args=parser.parse_args()
    config=RuntimeConfig.load(tuple(ConfigField(n) for n in ("NOTION_API_TOKEN", "NOTION_JOB_LEDGER_DATA_SOURCE_ID")))
    context=RunContext.start(now=datetime.now(timezone.utc), max_seconds=args.timeout_seconds); http=HttpClient()
    notion=NotionTransport(context=context,http=http,access_token=config.require("NOTION_API_TOKEN"))
    lane=LaneConfig("Scale-Up","UK",UNIVERSAL_FIT_FLOOR,None,"any",None,False,None,True)
    empty={"DIRECT":(),"ADJACENT":(),"METHOD_EQUIVALENT":(),"UNSUPPORTED":()}; dims={k:() for k in ("role_seniority","functional","technical_platform","delivery_complexity","competitive_advantage")}
    profile=FitProfile("scale-up-runtime",empty,(),dims,empty,(),(),())
    acquired, results=execute_scale_up(context=context,http=http,notion=notion,data_source_id=config.require("NOTION_JOB_LEDGER_DATA_SOURCE_ID"),lane=lane,lane_priority={"Scale-Up":0},fit_profile=profile,market="UK",source_lane="Scale-Up")
    print(json.dumps({"sources":len(acquired.sources),"observations":len(acquired.observations),"complete":acquired.complete,"ingest_results":len(results)})); return 0
if __name__ == "__main__": raise SystemExit(main())
