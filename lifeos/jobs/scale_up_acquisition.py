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

KINDS = frozenset({"workable_public", "ashby", "workday_public", "greenhouse", "pinpoint_json", "lever_public", "teamtailor_html", "static_complete_html", "wttj_html", "rippling_html", "join_html", "stream_html", "popsa_html", "bluestonex_html", "sixflow_html", "futuristic_html", "doubleword_bundle", "revolut_html", "tg0_html", "veramed_html", "provider_html", "generic_html"})
IGNORE = re.compile(r"(privacy|login|sign.?in|cookie|benefit|culture|people|about|contact|connect|alert|talent.?community)", re.I)
NON_JOB = re.compile(r"^(careers?|jobs?( explore jobs)?|current openings?|open positions?|see open positions?|view open roles?|view career openings?|view job|apply|apply now|join us|opportunities|get in touch\.?)$", re.I)
JOBISH = re.compile(r"(job|career|position|vacanc|opening|role|apply)", re.I)
ATS_HOSTS = ("ashbyhq.com", "greenhouse.io", "lever.co", "workdayjobs.com", "teamtailor.com", "join.com", "workable.com", "pinpointhq.com", "rippling.com")

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
        return len(self.sources) == 48 and len({s.company for s in self.sources}) == 48 and all(s.state == "COMPLETE" for s in self.sources)

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
        super().__init__(); self.links=[]; self.scripts=[]; self.headings=[]; self._href=None; self._parts=[]; self._script=False; self._script_type=""; self._script_parts=[]; self._heading=None; self._heading_parts=[]
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag == "a" and attrs.get("href"): self._href, self._parts = attrs["href"], []
        if tag == "script": self._script, self._script_type, self._script_parts = True, attrs.get("type", ""), []
        if tag in {"h1","h2","h3","h4","h5","h6"}: self._heading, self._heading_parts = tag, []
    def handle_data(self, data):
        if self._href: self._parts.append(data)
        if self._script: self._script_parts.append(data)
        if self._heading: self._heading_parts.append(data)
    def handle_endtag(self, tag):
        if tag == "a" and self._href: self.links.append((self._href, " ".join(" ".join(self._parts).split()))); self._href=None
        if tag == "script" and self._script:
            if self._script_type.casefold() == "application/ld+json": self.scripts.append("".join(self._script_parts))
            self._script=False; self._script_type=""; self._script_parts=[]
        if tag == self._heading:
            text=" ".join(" ".join(self._heading_parts).split())
            if text: self.headings.append((tag,text))
            self._heading=None; self._heading_parts=[]

def _parse_page(text):
    parser=_PageParser(); parser.feed(text); return parser

def _jsonld_jobs(parser, source, now):
    rows=[]; seen=set()
    def walk(value):
        if isinstance(value, dict):
            kind=value.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                url=value.get("url") or source["canonical_endpoint"]
                ident=value.get("identifier"); ident=ident.get("value") if isinstance(ident, dict) else ident
                if url not in seen:
                    rows.append(_obs(source,ident,value.get("title"),None,url,received_at=now)); seen.add(url)
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    for raw in parser.scripts:
        try: walk(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError): pass
    return rows

def _path_jobs(text, source, now, pattern, *, reject=None):
    parser=_parse_page(text); rows=_jsonld_jobs(parser,source,now); seen={r.source_apply_url for r in rows}; rx=re.compile(pattern,re.I)
    for href,label in parser.links:
        url=urljoin(source["canonical_endpoint"],href); path=urlparse(url).path.rstrip("/"); title=" ".join(label.split())
        if not rx.fullmatch(path) or len(title) < 3 or NON_JOB.fullmatch(title) or (reject and reject.search(title)) or url in seen: continue
        rows.append(_obs(source,hashlib.sha1(url.encode()).hexdigest()[:12],title,None,url,received_at=now)); seen.add(url)
    return rows

def _generic_html(text, source, now):
    parser=_parse_page(text); rows=_jsonld_jobs(parser,source,now); seen={r.source_apply_url for r in rows}
    base_host=urlparse(source["canonical_endpoint"]).netloc.replace("www.","")
    for href,label in parser.links:
        title=" ".join(label.split())
        if not JOBISH.search(f"{href} {title}") or IGNORE.search(title) or NON_JOB.fullmatch(title): continue
        url=urljoin(source["canonical_endpoint"],href); host=urlparse(url).netloc.replace("www.","")
        if host and base_host and host != base_host and not any(x in host for x in ATS_HOSTS): continue
        if len(title) >= 3 and url not in seen:
            rows.append(_obs(source,hashlib.sha1(url.encode()).hexdigest()[:12],title,None,url,received_at=now)); seen.add(url)
    return rows

def _join_jobs(text, source, now):
    parser=_parse_page(text); base_path=urlparse(source["canonical_endpoint"]).path.rstrip("/"); rows=_jsonld_jobs(parser,source,now); seen={r.source_apply_url for r in rows}
    for href,label in parser.links:
        url=urljoin(source["canonical_endpoint"],href); path=urlparse(url).path.rstrip("/"); title=" ".join(label.split())
        if path.startswith(base_path + "/") and len(title) >= 3 and not NON_JOB.fullmatch(title) and url not in seen:
            rows.append(_obs(source,hashlib.sha1(url.encode()).hexdigest()[:12],title,None,url,received_at=now)); seen.add(url)
    return rows

def _bluestonex(text, source, now):
    parser=_parse_page(text); rows=[]; seen=set()
    for href,label in parser.links:
        title=" ".join(label.split())
        if "full-time more information" not in title.casefold(): continue
        url=urljoin(source["canonical_endpoint"],href)
        if url not in seen:
            rows.append(_obs(source,hashlib.sha1(url.encode()).hexdigest()[:12],title,None,url,received_at=now)); seen.add(url)
    return rows

def _strip_html(value):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())

def _sixflow(text, source, now):
    parser=_parse_page(text); plain=_strip_html(text); zero=re.search(r"we don['’]?t have any live vacancies right now",plain,re.I); rows=[]; seen=set()
    for href,label in parser.links:
        url=urljoin(source["canonical_endpoint"],href); path=urlparse(url).path.rstrip("/"); title=" ".join(label.split())
        if not re.fullmatch(r"/careers/(?!future-opportunities$)[^/]+",path,re.I) or len(title)<3 or NON_JOB.fullmatch(title) or url in seen: continue
        rows.append(_obs(source,hashlib.sha1(url.encode()).hexdigest()[:12],title,None,url,received_at=now)); seen.add(url)
    if zero and rows: raise ValueError("Six & Flow contradictory zero and live vacancy evidence")
    if zero: return []
    if rows: return rows
    raise ValueError("Six & Flow inventory is ambiguous")

def _futuristic(text, source, now):
    if 'rjjobportal-careers-wrapper' not in text: raise ValueError("Futuristic careers portal wrapper not found")
    parts=text.split('<div class="rjjobportal-job-item"')[1:]
    if not parts: return []
    rows=[]; seen=set()
    for part in parts:
        title_m=re.search(r'rjjobportal-job-title">([^<]+)</span>',part,re.I)
        meta={_strip_html(k):_strip_html(v) for k,v in re.findall(r'rjjobportal-meta-label">([^<]+)</span>\s*<span class="rjjobportal-meta-val">(.*?)</span>',part,re.I|re.S)}
        job_id=meta.get("Job Reference Number")
        if not title_m or not job_id: raise ValueError("Futuristic vacancy item missing title or job reference")
        if job_id in seen: raise ValueError("Futuristic duplicate job reference")
        seen.add(job_id); title=re.sub(r'^\s*\d+\.\s*','',_strip_html(title_m.group(1))).strip()
        rows.append(_obs(source,job_id,title,meta.get("Location"),source["canonical_endpoint"],meta.get("Annual Salary"),now))
    return rows

def _balanced_js_array(text, start):
    depth=0; quote=None; escaped=False
    for i in range(start,len(text)):
        ch=text[i]
        if quote:
            if escaped: escaped=False
            elif ch=="\\": escaped=True
            elif ch==quote: quote=None
            continue
        if ch in {'"',"'",'`'}: quote=ch
        elif ch=='[': depth+=1
        elif ch==']':
            depth-=1
            if depth==0: return text[start:i+1]
    raise ValueError("unterminated Doubleword careers array")

def _doubleword(text, source, now, fetch_text):
    scripts=[urljoin(source["canonical_endpoint"],src) for src in re.findall(r'<script[^>]+src=["\']([^"\']+)',text,re.I) if '/assets/index-' in src and src.endswith('.js')]
    if len(scripts)!=1: raise ValueError("Doubleword expected one first-party app bundle")
    js=fetch_text(scripts[0]); m=re.search(r'([A-Za-z_$][\w$]*)=\[\{title:"(?:\\.|[^"\\])*",slug:"(?:\\.|[^"\\])*",department:"(?:\\.|[^"\\])*",type:"(?:\\.|[^"\\])*",seniority:"(?:\\.|[^"\\])*",location:"(?:\\.|[^"\\])*",compensation:',js)
    if not m or f'{m.group(1)}.map' not in js or 'Open Positions' not in js: raise ValueError("Doubleword Open Positions array not proven")
    raw=_balanced_js_array(js,js.find('[',m.start())); q=r'"(?:\\.|[^"\\])*"'; rx=re.compile(rf'\{{title:(?P<title>{q}),slug:(?P<slug>{q}),department:(?P<department>{q}),type:(?P<type>{q}),seniority:(?P<seniority>{q}),location:(?P<location>{q}),compensation:(?P<compensation>{q}),applyEmail:(?P<applyEmail>{q})')
    rows=[]; seen=set()
    for match in rx.finditer(raw):
        fields={k:json.loads(match.group(k)) for k in match.groupdict()}; slug=fields["slug"]
        if slug in seen: raise ValueError("Doubleword duplicate slug")
        seen.add(slug); url=urljoin(source["canonical_endpoint"],f"/careers/{slug}")
        rows.append(_obs(source,slug,fields["title"],fields["location"],url,fields["compensation"],now))
    if not rows: raise ValueError("Doubleword Open Positions parsed zero roles")
    return rows

def _revolut_slug(title):
    return re.sub(r"[^a-z0-9]+","-",str(title or "").casefold()).strip("-")

def _revolut(text, source, now):
    visible=re.search(r"We have\s+(\d+)\s+open positions",text,re.I)
    if not visible: raise ValueError("Revolut visible open-position count missing")
    script=re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',text,re.I|re.S)
    if not script: raise ValueError("Revolut __NEXT_DATA__ missing")
    payload=json.loads(unescape(script.group(1))); positions=payload.get("props",{}).get("pageProps",{}).get("positions")
    if not isinstance(positions,list): raise ValueError("Revolut positions is not a list")
    count=int(visible.group(1)); ids=[str(r.get("id") or "") for r in positions if isinstance(r,dict)]
    if len(positions)!=count or len(ids)!=count or any(not x for x in ids) or len(set(ids))!=count: raise ValueError("Revolut exhaustive inventory mismatch")
    rows=[]
    for p in positions:
        if not isinstance(p,dict): raise ValueError("Revolut malformed position")
        title=_strip_html(p.get("text")); locs=p.get("locations")
        if not title or not isinstance(locs,list): raise ValueError("Revolut position lacks title or locations")
        rendered=[]
        for loc in locs:
            if not isinstance(loc,dict) or not loc.get("name"): raise ValueError("Revolut malformed location")
            rendered.append(" · ".join(x for x in (_strip_html(loc.get("name")),_strip_html(loc.get("type")),_strip_html(loc.get("country"))) if x))
        job_id=str(p["id"]); url=f"https://www.revolut.com/en-GB/careers/position/{_revolut_slug(title)}-{job_id}/"
        rows.append(_obs(source,job_id,title," | ".join(rendered) or None,url,received_at=now))
    return rows

def _tg0(text, source, now):
    parser=_parse_page(text); start=next((i for i,(_,v) in enumerate(parser.headings) if v.strip().upper()=="JOIN US"),None)
    if start is None: raise ValueError("TG0 JOIN US section not found")
    offices={"LONDON HQ","BEIJING OFFICE","HONG KONG OFFICE","SINGAPORE OFFICE"}; roles=[v for tag,v in parser.headings[start+1:] if tag=="h3" and v.upper() not in offices and not NON_JOB.fullmatch(v)]
    apply_url=source["canonical_endpoint"]
    for href,label in parser.links:
        if "submit your application" in label.casefold(): apply_url=urljoin(source["canonical_endpoint"],href); break
    if not roles: raise ValueError("TG0 JOIN US exposed no roles")
    return [_obs(source,hashlib.sha1(v.encode()).hexdigest()[:12],v,"London / global offices",apply_url,received_at=now) for v in roles]

def _veramed_detail(text, source, url, now):
    parser=_parse_page(text); marker=next((i for i,(_,v) in enumerate(parser.headings) if v.casefold()=="apply for this role"),None)
    if marker is None: raise ValueError("Veramed detail missing apply marker")
    titles=[v for tag,v in parser.headings[:marker] if tag in {"h1","h2"} and v.casefold() not in {"careers","job openings"}]
    if not titles: raise ValueError("Veramed detail missing title")
    query=urlparse(url).query; m=re.search(r'(?:^|&)gh_jid=([^&]+)',query); job_id=m.group(1) if m else hashlib.sha1(url.encode()).hexdigest()[:12]
    return _obs(source,job_id,titles[-1],None,url,received_at=now)

def _veramed(text, source, now, fetch_text):
    parser=_parse_page(text); urls=[]; seen=set()
    for href,_ in parser.links:
        url=urljoin(source["canonical_endpoint"],href)
        if "gh_jid=" in url and url not in seen: seen.add(url); urls.append(url)
    started=False; titles=[]; stop={"ready to move your study forward?","biometrics built better"}; generic={"job openings","open positions","no jobs found matching your selected filters."}
    for tag,heading in parser.headings:
        value=_strip_html(heading); folded=value.casefold()
        if folded=="job openings": started=True; continue
        if not started: continue
        if folded in stop: break
        if tag=="h2" and value and folded not in generic and value not in titles: titles.append(value)
    if urls and titles:
        if len(urls)!=len(titles): raise ValueError("Veramed index mismatch")
        rows=[]
        for title,url in zip(titles,urls):
            m=re.search(r'[?&]gh_jid=([^&]+)',url)
            if not m: raise ValueError("Veramed link missing gh_jid")
            rows.append(_obs(source,m.group(1),title,None,url,received_at=now))
        return rows
    if urls: return sorted((_veramed_detail(fetch_text(url),source,url,now) for url in urls), key=lambda r:r.provider_job_id or "")
    if not titles: raise ValueError("Veramed inventory ambiguous")
    return [_obs(source,hashlib.sha1(v.encode()).hexdigest()[:12],v,None,source["canonical_endpoint"],received_at=now) for v in titles]

def _recovery(source, now):
    evidence=source.get("recovery_evidence") or {}
    if set(evidence.get("channels", [])) != {"google_web", "linkedin_jobs"}:
        raise ValueError("recovery evidence is missing a required channel")
    if evidence.get("state") != "COMPLETE":
        raise ValueError("recovery evidence does not prove complete inventory")
    rows=[]
    for item in evidence.get("candidates", []):
        if not item.get("title") or not item.get("url") or not item.get("employer_verified") or not item.get("vacancy_verified"):
            raise ValueError("recovery candidate is incomplete")
        rows.append(_obs(source,item.get("job_id"),item["title"],item.get("location"),item["url"],received_at=now))
    if not rows and not evidence.get("authoritative_zero"):
        raise ValueError("recovery evidence is ambiguous")
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
        if kind in {"provider_html", "generic_html"}:
            if source.get("recovery_evidence"):
                return _recovery(source,self.now)
            raise ValueError("primary recovery source requires approved recovery evidence")
        if kind in {"teamtailor_html", "wttj_html", "rippling_html", "stream_html", "popsa_html", "bluestonex_html", "join_html", "static_complete_html"}:
            text=self._html(source["canonical_endpoint"]); marker=source.get("zero_marker")
            if kind == "teamtailor_html": rows=_path_jobs(text,source,self.now,r"/jobs/[^/]+",reject=IGNORE)
            elif kind == "wttj_html": rows=_path_jobs(text,source,self.now,r"/jobs/[^/]+")
            elif kind == "stream_html": rows=_path_jobs(text,source,self.now,r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?careers/[^/]+")
            elif kind == "popsa_html": rows=_path_jobs(text,source,self.now,r"/careers/[^/]+")
            elif kind == "join_html": rows=_join_jobs(text,source,self.now)
            elif kind == "bluestonex_html": rows=_bluestonex(text,source,self.now)
            else: rows=_generic_html(text,source,self.now)
            if rows: return rows
            if marker and marker.casefold() in unescape(re.sub(r"<[^>]+>", " ", text)).casefold(): return []
            raise ValueError("ambiguous empty first-party inventory")
        if kind in {"sixflow_html","futuristic_html","doubleword_bundle","revolut_html","tg0_html","veramed_html"}:
            text=self._html(source["canonical_endpoint"])
            if kind == "sixflow_html": return _sixflow(text,source,self.now)
            if kind == "futuristic_html": return _futuristic(text,source,self.now)
            if kind == "doubleword_bundle": return _doubleword(text,source,self.now,self._html)
            if kind == "revolut_html": return _revolut(text,source,self.now)
            if kind == "tg0_html": return _tg0(text,source,self.now)
            return _veramed(text,source,self.now,self._html)
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
