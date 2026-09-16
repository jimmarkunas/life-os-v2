"""Lean source-specific Newsletter vacancy parsing.

Selectively harvested from v1 provider-card parsing. Deliberately excludes
posting-date interpretation, final URL resolution, qualification, persistence,
manifests, checkpoints, recovery, and Continuity.
"""
from __future__ import annotations
import re
from urllib.parse import urlsplit
from .models import MessageParseResult, ParseIssue, ParseState, RoutedNewsletterMessage, SourceVacancyObservation

MARKDOWN_LINK = re.compile(r"\[([^\]]{2,1200})\]\((https?://[^)\s]+)\)", re.S)
ROLE_RE = re.compile(r"(?i)\b(?:senior |sr\.? |technical |digital |engineering |operations |implementation |product |program |project |delivery |scrum )*(?:technical program manager|program manager|project manager|product manager|product owner|scrum master|delivery manager|implementation manager|solutions architect)\b[^|$\n]{0,80}")

def _clean(value: str) -> str: return re.sub(r"\s+", " ", value.replace("\u200b", " ")).strip()
def _lines(value: str) -> list[str]: return [x.strip() for x in re.split(r"[\r\n]+", value) if x.strip()]

def detect_source(message: RoutedNewsletterMessage) -> str | None:
    sender = message.sender.casefold(); subject = message.subject.casefold()
    if "jobright" in sender or "jobright" in subject: return "Jobright"
    if "lensa" in sender or "lensa" in subject: return "Lensa"
    if "linkedin" in sender and "job" in (sender + subject): return "LinkedIn Jobs"
    adapter = str(message.headers.get("X-LifeOS-Source-Adapter") or message.headers.get("x-lifeos-source-adapter") or "").strip()
    return adapter or None

def parse_message(message: RoutedNewsletterMessage) -> MessageParseResult:
    provider = detect_source(message); message_ref = f"{message.mailbox}:{message.message_id}"
    if not provider:
        issue = ParseIssue("source-unrecognized", message_ref)
        return MessageParseResult(message_ref, None, ParseState.DEGRADED, (), (issue,))
    if not message.body_text:
        issue = ParseIssue("body-missing", message_ref)
        return MessageParseResult(message_ref, provider, ParseState.DEGRADED, (), (issue,))
    observations: list[SourceVacancyObservation] = []
    for index, match in enumerate(MARKDOWN_LINK.finditer(message.body_text), start=1):
        text, url = match.group(1), match.group(2)
        if not _is_candidate(provider, text, url):
            continue
        observations.append(_parse_candidate(provider, text, url, message, index))
    issues: list[ParseIssue] = []
    if not observations:
        issues.append(ParseIssue("no-vacancy-cards-parsed", message_ref))
    for obs in observations:
        for code in obs.issues:
            issues.append(ParseIssue(code, obs.evidence_ref))
    state = ParseState.PASS if observations and not issues else ParseState.DEGRADED
    return MessageParseResult(message_ref, provider, state, tuple(observations), tuple(issues))

def _is_candidate(provider: str, text: str, url: str) -> bool:
    low_url = url.casefold(); low_text = text.casefold()
    if any(token in low_text or token in low_url for token in ("unsubscribe", "privacy policy", "email preferences")):
        return False
    if provider == "Jobright": return "jobright.ai/jobs/info/" in low_url
    if provider == "Lensa": return "lensa.com" in urlsplit(url).netloc.casefold()
    if provider == "LinkedIn Jobs": return "linkedin.com/jobs/view/" in low_url or "linkedin.com/comm/jobs/view/" in low_url
    return ROLE_RE.search(_clean(text)) is not None

def _parse_candidate(provider: str, text: str, url: str, message: RoutedNewsletterMessage, index: int) -> SourceVacancyObservation:
    evidence_ref = f"{message.mailbox}:{message.message_id}:card:{index}"
    company = role = location = compensation = provider_job_id = None; provider_score = None; issues: list[str] = []
    if provider == "Jobright":
        lines = [x for x in _lines(text) if x not in {"Jobright.ai Job Icon", "APPLY NOW"}]
        score_i = next((i for i,x in enumerate(lines) if re.fullmatch(r"\d{2,3}%?", x)), None)
        if score_i is not None and score_i >= 1 and score_i + 1 < len(lines):
            company_candidates = [x for x in lines[:score_i] if "·" not in x and "public company" not in x.casefold() and "stage" not in x.casefold()]
            if company_candidates: company = company_candidates[0]
            role = lines[score_i+1]; provider_score = int(lines[score_i].rstrip("%"))
            tail = lines[score_i+2:]
            compensation = next((x for x in tail if "$" in x), None)
            location = next((x for x in tail if "$" not in x and "ago" not in x.casefold() and "applicant" not in x.casefold() and "referral" not in x.casefold()), None)
        if not company or not role: issues.append("unresolved-card-shape")
    elif provider == "Lensa":
        raw = _clean(text); before = re.split(r"\$\s*\d", raw, maxsplit=1)[0]; match = list(ROLE_RE.finditer(before))
        if match:
            role_match = match[-1]; role = role_match.group(0).strip(); company = before[:role_match.start()].strip(" -–·") or None
        compensation_match = re.search(r"\$[^|]{2,30}", raw); compensation = compensation_match.group(0).strip() if compensation_match else None
        location = "Remote" if "remote" in raw.casefold() else None
        if not company or not role: issues.append("unresolved-card-shape")
    elif provider == "LinkedIn Jobs":
        lines = _lines(text)
        if lines: role = lines[0]
        if len(lines) > 1:
            company = lines[1].split(" · ",1)[0].strip() or None
            location = lines[1].split(" · ",1)[1].strip() if " · " in lines[1] else None
        m = re.search(r"/jobs/view/(\d+)", url); provider_job_id = m.group(1) if m else None
        if not company or not role: issues.append("unresolved-card-shape")
    else:
        raw = _clean(text); match = ROLE_RE.search(raw)
        if match:
            role = match.group(0).strip(); company = raw[:match.start()].strip(" -–·|") or None
        if not role: issues.append("unresolved-card-shape")
    return SourceVacancyObservation(evidence_ref, provider, message.mailbox, message.message_id, message.subject, company, role, location, compensation, url, provider_job_id, provider_score, tuple(issues))
