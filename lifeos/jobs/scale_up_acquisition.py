from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext
from lifeos.newsletter.models import SourceVacancyObservation

KINDS = frozenset({"workable_public", "ashby", "workday_public", "greenhouse", "pinpoint_json", "lever_public", "teamtailor_html", "static_complete_html", "wttj_html", "rippling_html", "join_html", "stream_html", "popsa_html", "bluestonex_html"})

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
        return len(self.sources) == 30 and len({s.company for s in self.sources}) == 30 and all(s.state == "COMPLETE" for s in self.sources)

def _text(value):
    return None if value is None else str(value)

def _obs(source, job_id, title, location, url, compensation=None, received_at=None):
    identity = str(job_id or url or title)
    ref = hashlib.sha256(f"{source['company']}|{identity}|{url or ''}".encode()).hexdigest()[:20]
    return SourceVacancyObservation(
        evidence_ref=f"scale-up:{ref}", source_provider=source["source_type"], source_mailbox="public-web",
        source_message_id=identity, source_subject=_text(title) or "", company=source["company"], role=_text(title),
        location_text=_text(location), compensation_text=_text(compensation), source_apply_url=_text(url),
        provider_job_id=identity, source_received_at=received_at,
    )

class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.scripts=[]; self._href=None; self._parts=[]; self._script=False; self._script_parts=[]
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag == "a" and attrs.get("href"): self._href, self._parts = attrs["href"], []
        if tag == "script": self._script, self._script_parts = True, []
    def handle_data(self, data):
        if self._href: self._parts.append(data)
        if self._script: self._script_parts.append(data)
    def handle_endtag(self, tag):
        if tag == "a" and self._href: self.links.append((self._href, " ".join(" ".join(self._parts).split()))); self._href=None
        if tag == "script" and self._script: self.scripts.append("".join(self._script_parts)); self._script=False

def _html_jobs(text, source, now, pattern=r"/jobs/[^/]+", *, all_links=False):
    parser=_PageParser(); parser.feed(text); rows=[]; seen=set()
    for raw in parser.scripts:
        try:
            data=json.loads(raw); items=data if isinstance(data,list) else [data]
            for item in items:
                if isinstance(item,dict) and item.get("@type") == "JobPosting":
                    url=item.get("url") or source["canonical_endpoint"]; rows.append(_obs(source,item.get("identifier"),item.get("title"),None,url,received_at=now)); seen.add(url)
        except (TypeError, ValueError, json.JSONDecodeError): pass
    base_path=urlparse(source["canonical_endpoint"]).path.rstrip("/")
    for href,label in parser.links:
        url=urljoin(source["canonical_endpoint"],href); path=urlparse(url).path.rstrip("/")
        if (all_links and path.startswith(base_path + "/")) or (not all_links and re.fullmatch(pattern,path,re.I)):
            if len(label) >= 3 and label.casefold() not in {"apply","apply now","careers","jobs"} and url not in seen:
                rows.append(_obs(source,hashlib.sha1(url.encode()).hexdigest()[:12],label,None,url,received_at=now)); seen.add(url)
    return rows

class ScaleUpAcquirer:
    def __init__(self, *, context: RunContext, http: HttpClient, max_workers: int = 4):
        self.context, self.http = context, http

    def _json(self, method, url, body=None):
        self.context.require_time()
        return self.http.request_json(self.context, method, url, json_body=body, timeout_seconds=10, retry=RetryPolicy(max_attempts=2))

    def _html(self, url):
        self.context.require_time()
        return self.http.request(self.context, "GET", url, timeout_seconds=10, retry=RetryPolicy(max_attempts=2)).body.decode("utf-8", "replace")

    def _jobs(self, source):
        kind, jobs = source["source_type"], None
        if kind in {"teamtailor_html", "wttj_html", "rippling_html", "stream_html", "popsa_html", "bluestonex_html", "join_html", "static_complete_html"}:
            text=self._html(source["canonical_endpoint"]); marker=source.get("zero_marker")
            patterns={"teamtailor_html":r"/jobs/[^/]+","wttj_html":r"/jobs/[^/]+","rippling_html":r"/jobs/[^/]+","stream_html":r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?careers/[^/]+","popsa_html":r"/careers/[^/]+"}
            rows=_html_jobs(text,source,self.now,patterns.get(kind,r".*"),all_links=kind in {"join_html","static_complete_html","bluestonex_html"})
            if kind == "bluestonex_html": rows=[r for r in rows if "full-time more information" in (r.source_subject or "").casefold()]
            if rows: return rows
            if marker and marker.casefold() in unescape(re.sub(r"<[^>]+>", " ", text)).casefold(): return []
            raise ValueError("ambiguous empty first-party inventory")
        if kind == "workday_public":
            parsed = urlparse(source["canonical_endpoint"]); tenant, site = source["source_key"].split(":", 1)
            api = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/jobs"; rows=[]; offset=0; total=None
            while total is None or offset < total:
                data = self._json("POST", api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""})
                if not isinstance(data, dict) or not isinstance(data.get("jobPostings"), list): raise ValueError("invalid Workday inventory")
                page=data["jobPostings"]; total=int(data.get("total", len(page)))
                for j in page:
                    path=j.get("externalPath"); bullet=j.get("bulletFields") or []
                    base=f"{parsed.scheme}://{parsed.netloc}/en-US/{site}"
                    url=base + path if str(path or "").startswith("/") else urljoin(base + "/", path or "")
                    rows.append(_obs(source, bullet[0] if bullet else None, j.get("title"), j.get("locationsText"), url, received_at=self.now))
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
                rows.append(_obs(source,j.get("id"),j.get("title"),loc,j.get("absolute_url") or source.get("careers_url"),received_at=self.now))
            elif kind == "ashby":
                comp=j.get("compensation"); comp=json.dumps(comp,separators=(",",":")) if isinstance(comp,dict) else comp
                job_url=j.get("jobUrl") or source.get("careers_url")
                rows.append(_obs(source,j.get("id"),j.get("title"),j.get("location"),j.get("applyUrl") or job_url,comp,self.now))
            elif kind == "workable_public":
                loc=", ".join(x for x in (j.get("city"),j.get("state"),j.get("country")) if x)
                url=j.get("url") or j.get("shortlink") or j.get("application_url") or source.get("careers_url")
                rows.append(_obs(source,j.get("shortcode") or j.get("code"),j.get("title"),loc,j.get("application_url") or j.get("shortlink") or url,received_at=self.now))
            elif kind == "pinpoint_json":
                loc=j.get("location") or {}; dep=(j.get("job") or {}).get("department") or j.get("department") or {}
                rows.append(_obs(source,j.get("id") or (j.get("job") or {}).get("id"),j.get("title"),loc.get("name") if isinstance(loc,dict) else loc,j.get("url") or urljoin(source.get("careers_url",""),j.get("path","")),j.get("compensation") if j.get("compensation_visible",True) else None,self.now))
            else:
                cat=j.get("categories") or {}; created=j.get("createdAt"); posted=datetime.fromtimestamp(created/1000,tz=timezone.utc).isoformat() if isinstance(created,(int,float)) else None
                rows.append(_obs(source,j.get("id"),j.get("text"),cat.get("location"),j.get("applyUrl") or j.get("hostedUrl") or source.get("careers_url"),received_at=self.now))
        return rows

    def acquire(self, registry, *, now=None):
        observations=[]; health=[]; self.now=now or datetime.now(timezone.utc)
        for source in registry.get("sources", []):
            try:
                if source.get("source_type") not in KINDS: raise TypeError("unsupported source type")
                rows=self._jobs(source); observations.extend(rows); health.append(ScaleUpSourceHealth(source["company"],"COMPLETE",len(rows)))
            except Exception as exc:
                state="DEGRADED" if isinstance(exc, ValueError) else "BLOCKED"
                health.append(ScaleUpSourceHealth(source.get("company",""),state,0,type(exc).__name__))
        return ScaleUpAcquisitionResult(tuple(observations),tuple(health))
