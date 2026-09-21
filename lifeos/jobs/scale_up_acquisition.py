from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext
from lifeos.newsletter.models import SourceVacancyObservation

KINDS = frozenset({"workable_public", "ashby", "workday_public", "greenhouse", "pinpoint_json", "lever_public"})

@dataclass(frozen=True, slots=True)
class ScaleUpSourceHealth:
    company: str
    state: str
    candidate_count: int
    detail: str | None = None

@dataclass(frozen=True, slots=True)
class ScaleUpAcquisitionResult:
    observations: tuple[SourceVacancyObservation, ...]
    sources: tuple[ScaleUpSourceHealth, ...]

    @property
    def complete(self) -> bool:
        return len(self.sources) > 0 and all(s.state == "COMPLETE" for s in self.sources)

def _text(value):
    return None if value is None else str(value)

def _obs(source, job_id, title, location, department, url, compensation=None, posted=None):
    identity = str(job_id or url or title)
    ref = hashlib.sha256(f"{source['company']}|{identity}|{url or ''}".encode()).hexdigest()[:20]
    received = None
    if posted:
        try:
            received = datetime.fromisoformat(str(posted).replace("Z", "+00:00"))
        except ValueError:
            pass
    return SourceVacancyObservation(
        evidence_ref=f"scale-up:{ref}", source_provider=source["source_type"], source_mailbox="public-web",
        source_message_id=identity, source_subject=_text(title) or "", company=source["company"], role=_text(title),
        location_text=_text(location), compensation_text=_text(compensation), source_apply_url=_text(url),
        provider_job_id=identity, source_description_text=_text(department), source_received_at=received,
    )

class ScaleUpAcquirer:
    def __init__(self, *, context: RunContext, http: HttpClient, max_workers: int = 4):
        self.context, self.http = context, http

    def _json(self, method, url, body=None):
        self.context.require_time()
        return self.http.request_json(self.context, method, url, json_body=body, timeout_seconds=10, retry=RetryPolicy(max_attempts=2))

    def _jobs(self, source):
        kind, jobs = source["source_type"], None
        if kind == "workday_public":
            parsed = urlparse(source["canonical_endpoint"]); tenant, site = source["source_key"].split(":", 1)
            api = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/jobs"; rows=[]; offset=0; total=None
            while total is None or offset < total:
                data = self._json("POST", api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""})
                if not isinstance(data, dict) or not isinstance(data.get("jobPostings"), list): raise ValueError("invalid Workday inventory")
                page=data["jobPostings"]; total=int(data.get("total", len(page)))
                for j in page:
                    path=j.get("externalPath"); bullet=j.get("bulletFields") or []
                    url=urljoin(f"{parsed.scheme}://{parsed.netloc}/en-US/{site}/", path or "")
                    rows.append(_obs(source, bullet[0] if bullet else None, j.get("title"), j.get("locationsText"), None, url, posted=j.get("postedOn")))
                if not page or len(page) < 20: break
                offset += len(page)
            return rows
        data=self._json("GET", source["canonical_endpoint"])
        if kind == "lever_public":
            if not isinstance(data, list): raise ValueError("invalid Lever inventory")
            jobs=data
        else:
            key="jobs" if kind in {"greenhouse","ashby","workable_public"} else "data"
            if not isinstance(data, dict) or not isinstance(data.get(key), list): raise ValueError("invalid inventory")
            jobs=data[key]
        rows=[]
        for j in jobs:
            if not isinstance(j, dict): raise ValueError("invalid vacancy")
            if kind == "greenhouse":
                loc=(j.get("location") or {}).get("name"); dep=", ".join(d.get("name","") for d in j.get("departments",[]) if isinstance(d,dict))
                rows.append(_obs(source,j.get("id"),j.get("title"),loc,dep,j.get("absolute_url") or source.get("careers_url"),posted=j.get("updated_at")))
            elif kind == "ashby":
                comp=j.get("compensation"); comp=json.dumps(comp,separators=(",",":")) if isinstance(comp,dict) else comp
                rows.append(_obs(source,j.get("id"),j.get("title"),j.get("location"),j.get("department") or j.get("team"),j.get("jobUrl") or source.get("careers_url"),comp,j.get("publishedAt") or j.get("publishedDate")))
            elif kind == "workable_public":
                loc=", ".join(x for x in (j.get("city"),j.get("state"),j.get("country")) if x)
                url=j.get("url") or j.get("shortlink") or j.get("application_url") or source.get("careers_url")
                rows.append(_obs(source,j.get("shortcode") or j.get("code"),j.get("title"),loc,j.get("department"),j.get("application_url") or url,posted=j.get("published_on") or j.get("created_at")))
            elif kind == "pinpoint_json":
                loc=j.get("location") or {}; dep=(j.get("job") or {}).get("department") or j.get("department") or {}
                rows.append(_obs(source,j.get("id") or (j.get("job") or {}).get("id"),j.get("title"),loc.get("name") if isinstance(loc,dict) else loc,dep.get("name") if isinstance(dep,dict) else dep,j.get("url") or urljoin(source.get("careers_url",""),j.get("path","")),j.get("compensation") if j.get("compensation_visible",True) else None,j.get("published_at") or j.get("created_at")))
            else:
                cat=j.get("categories") or {}; created=j.get("createdAt"); posted=datetime.fromtimestamp(created/1000,tz=timezone.utc).isoformat() if isinstance(created,(int,float)) else None
                rows.append(_obs(source,j.get("id"),j.get("text"),cat.get("location"),cat.get("department") or cat.get("team"),j.get("applyUrl") or j.get("hostedUrl") or source.get("careers_url"),posted=posted))
        return rows

    def acquire(self, registry, *, now=None):
        observations=[]; health=[]
        for source in registry.get("sources", []):
            try:
                if source.get("source_type") not in KINDS: raise ValueError("unsupported source type")
                rows=self._jobs(source); observations.extend(rows); health.append(ScaleUpSourceHealth(source["company"],"COMPLETE",len(rows)))
            except Exception as exc:
                health.append(ScaleUpSourceHealth(source.get("company",""),"BLOCKED",0,type(exc).__name__))
        return ScaleUpAcquisitionResult(tuple(observations),tuple(health))
