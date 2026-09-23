"""Source-specific Newsletter vacancy parsing.

Harvested from v1's proven Newsletter projection behavior, but kept inside
v2's source-observation boundary: this module extracts supported provider
vacancy cards only. Jobs still owns terminal URL resolution, Posting Date,
Stable Job identity, Fit, qualification, lifecycle, and persistence.
"""
from __future__ import annotations

import html
import re
from email import policy
from email.parser import Parser
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .models import MessageParseResult, ParseIssue, ParseState, RoutedNewsletterMessage, SourceVacancyObservation, fatal_issue_codes


MONEY = re.compile(r"\$\s*\d[\d,.]*\s*[kK]?(?:\s*[-–]\s*\$?\s*\d[\d,.]*\s*[kK]?)?\s*/?\s*(?:yr|year|hr|hour|wk|week)?", re.I)
JOB_URL = re.compile(r"https?://[^\s<>\"]+/jobs/view/(\d+)[^\s<>\"]*", re.I)
MARKDOWN_LINK = re.compile(r"\[([^\[\]]{2,2500})\]\((https?://[^)\s]+)\)", re.S)
NOISE = {
    "le",
    "your job alerts",
    "more jobs",
    "more remote jobs",
    "easy apply",
    "apply now",
    "apply with resume & profile",
    "view job",
    "view this job",
    "new",
    "remote",
}

_LENSA_VACANCY_PATH_PREFIXES = ("/ls/click", "/f/a/", "/c/", "/cgw/")
_CONTROL_LINK_TERMS = ("unsubscribe", "privacy", "preference", "settings", "gig jobs", "more jobs", "view all jobs")


def detect_source(message: RoutedNewsletterMessage) -> str | None:
    sender = message.sender.casefold()
    subject = message.subject.casefold()
    if "jobright" in sender or "jobright" in subject:
        return "Jobright"
    if "lensa" in sender or "lensa" in subject:
        return "Lensa"
    if "linkedin" in sender and "job" in (sender + subject):
        return "LinkedIn Jobs"
    if "dice" in sender or "dice" in subject:
        return "Dice"
    if "boostie.jobs" in sender or "bridgeview" in sender:
        return "Other"
    adapter = str(message.headers.get("X-LifeOS-Source-Adapter") or message.headers.get("x-lifeos-source-adapter") or "").strip()
    return adapter or None


def parse_message(message: RoutedNewsletterMessage) -> MessageParseResult:
    provider = detect_source(message)
    message_ref = f"{message.mailbox}:{message.message_id}"
    if not provider:
        issue = ParseIssue("source-unrecognized", message_ref)
        return MessageParseResult(message_ref, None, ParseState.DEGRADED, (), (issue,))

    issues: list[ParseIssue] = []
    best_observations: tuple[SourceVacancyObservation, ...] = ()
    linkedin_preheader = None
    if provider == "LinkedIn Jobs":
        for source_text, source_kind in _source_texts(message):
            if source_kind == "raw-mime-html":
                linkedin_preheader = _extract_preheader_text(source_text)
                break
    for source_text, source_kind in _source_texts(message):
        observations, terminal, parse_issues = _parse_provider(
            provider, source_text, message, source_kind, linkedin_preheader=linkedin_preheader
        )
        if observations and terminal and not fatal_issue_codes(tuple(parse_issues)):
            return MessageParseResult(
                message_ref,
                provider,
                ParseState.PASS,
                tuple(observations),
                tuple(_dedupe_issues([ParseIssue(code, message_ref) for code in parse_issues])),
            )
        if len(observations) > len(best_observations):
            best_observations = tuple(observations)
        issues.extend(ParseIssue(code, message_ref) for code in parse_issues)

    if best_observations:
        if issues and not fatal_issue_codes(tuple(issue.code for issue in issues)):
            return MessageParseResult(message_ref, provider, ParseState.PASS, best_observations, tuple(_dedupe_issues(issues)))
        if not issues:
            issues.append(ParseIssue("message-parse-degraded", message_ref))
        return MessageParseResult(message_ref, provider, ParseState.DEGRADED, best_observations, tuple(_dedupe_issues(issues)))
    if not issues:
        issues.append(ParseIssue("no-vacancy-cards-parsed", message_ref))
    return MessageParseResult(message_ref, provider, ParseState.DEGRADED, (), tuple(_dedupe_issues(issues)))


def _parse_provider(
    provider: str,
    text: str,
    message: RoutedNewsletterMessage,
    source_kind: str,
    *,
    linkedin_preheader: str | None = None,
) -> tuple[list[SourceVacancyObservation], str | None, list[str]]:
    if provider == "Lensa":
        cards, terminal = _parse_lensa(text)
    elif provider == "Jobright":
        cards, terminal = _parse_jobright(text)
    elif provider == "LinkedIn Jobs":
        raw_mime_html = text if source_kind == "raw-mime-html" else None
        cards, terminal = _parse_linkedin(
            text, raw_mime_html=raw_mime_html, preheader=linkedin_preheader
        )
    else:
        cards, terminal = _parse_markdown_generic(text)
    issues: list[str] = []
    if cards and not terminal:
        issues.append("terminal-marker-missing")
    observations = [_observation(provider, message, source_kind, i, card) for i, card in enumerate(cards, start=1)]
    for obs in observations:
        issues.extend(obs.issues)
    return observations, terminal, issues


def _observation(
    provider: str, message: RoutedNewsletterMessage, source_kind: str, index: int, card: dict[str, object]
) -> SourceVacancyObservation:
    issues: list[str] = []
    company = _present(card.get("company"))
    role = _present(card.get("role"))
    source_apply_url = _present(card.get("apply_url"))
    if not role:
        issues.append("unresolved-card-shape")
    elif not company:
        # A genuinely identifiable vacancy (role/location/compensation/apply
        # URL) whose source simply never supplies a company name is an
        # enrichment gap, not a parse failure -- do not fabricate one, and
        # do not fail the whole card closed; identity.stable_job_key()'s own
        # company+role+location fallback decides downstream.
        issues.append("source-company-missing")
    if not source_apply_url:
        issues.append("source-apply-url-missing")
    provider_score = card.get("provider_score")
    return SourceVacancyObservation(
        evidence_ref=f"{message.mailbox}:{message.message_id}:{source_kind}:card:{index}",
        source_provider=provider,
        source_mailbox=message.mailbox,
        source_message_id=message.message_id,
        source_subject=message.subject,
        company=company,
        role=role,
        location_text=_present(card.get("location")),
        compensation_text=_present(card.get("compensation")),
        source_apply_url=source_apply_url,
        provider_job_id=_present(card.get("provider_job_id")),
        provider_score=int(provider_score) if isinstance(provider_score, int) else None,
        source_description_text=_present(card.get("description")),
        issues=tuple(issues),
        source_received_at=message.received_at,
    )


def _present(value: object) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").replace("\u200b", " ")).strip()
    return text or None


def _dedupe_issues(issues: list[ParseIssue]) -> list[ParseIssue]:
    seen: set[tuple[str, str]] = set()
    out: list[ParseIssue] = []
    for issue in issues:
        key = (issue.code, issue.evidence_ref)
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out


def _source_texts(message: RoutedNewsletterMessage) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if message.raw_mime:
        candidates.extend(_raw_mime_texts(message.raw_mime))
    if message.html_text:
        candidates.append((message.html_text, "html"))
    if message.body_text:
        candidates.append((message.body_text, "body"))

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for text, kind in candidates:
        if not text or not text.strip():
            continue
        key = text.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append((text, kind))
    return out


def _raw_mime_texts(raw_mime: str) -> list[tuple[str, str]]:
    try:
        msg = Parser(policy=policy.default).parsestr(raw_mime)
    except Exception:
        return []
    html_parts: list[str] = []
    plain_parts: list[str] = []
    parts = msg.walk() if msg.is_multipart() else (msg,)
    for part in parts:
        content_type = part.get_content_type()
        try:
            content = part.get_content()
        except Exception:
            continue
        if content_type == "text/html":
            html_parts.append(str(content))
        elif content_type == "text/plain":
            plain_parts.append(str(content))
    out: list[tuple[str, str]] = []
    if html_parts:
        out.append(("\n".join(html_parts), "raw-mime-html"))
    if plain_parts:
        out.append(("\n".join(plain_parts), "raw-mime-plain"))
    return out


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = re.sub(r"[ \t]+", " ", html.unescape(data)).strip()
        if text:
            self.parts.extend(x.strip() for x in text.replace("\r", "\n").split("\n") if x.strip())


class _StructuredLinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, list[str]]] = []
        self._href: str | None = None
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "a":
            if self._depth == 0:
                self._href = (dict(attrs).get("href") or "").strip()
                self._parts = []
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._depth > 0:
            self._depth -= 1
            if self._depth == 0 and self._href:
                if self._parts:
                    self.links.append((self._href, self._parts[:]))
                self._href = None
                self._parts = []

    def handle_data(self, data: str) -> None:
        if self._depth > 0:
            text = re.sub(r"\s+", " ", html.unescape(data)).strip()
            if text:
                self._parts.append(text)


class _LensaLinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def _flush(self) -> None:
        if self._href:
            text = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
            if text:
                self.links.append((self._href, text))
        self._href = None
        self._parts = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "a":
            self._flush()
            self._href = (dict(attrs).get("href") or "").strip()
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._href:
            text = html.unescape(data).strip()
            if text:
                self._parts.append(text)

    def close(self) -> None:
        super().close()
        self._flush()


def _plain(text: str) -> str:
    if not re.search(r"<\s*[a-zA-Z][^>]*>", text):
        rendered = text
    else:
        parser = _TextExtractor()
        try:
            parser.feed(text)
            rendered = "\n".join(parser.parts)
        except Exception:
            rendered = re.sub(r"<[^>]+>", "\n", text)
    rendered = html.unescape(rendered).replace("\r", "\n")
    rendered = re.sub(r"[ \t]+", " ", rendered)
    return re.sub(r"\n{2,}", "\n", rendered).strip()


def _lines(text: str) -> list[str]:
    text = text.replace("\\n", "\n")
    return [re.sub(r"\s+", " ", x).strip(" -|\t") for x in text.splitlines() if re.sub(r"\s+", " ", x).strip()]


def _clean_candidate(text: str) -> str:
    return re.sub(r"^Job title\s*", "", text, flags=re.I).strip()


def _is_noise(text: str) -> bool:
    low = text.lower().strip()
    bare = low.lstrip("[")
    return bare in NOISE or low.startswith("http") or low.startswith("(http") or "edit settings" in low or "unsubscribe" in low


def _is_control_link(text: str, url: str) -> bool:
    low = f"{text} {url}".casefold()
    return any(term in low for term in _CONTROL_LINK_TERMS)


def _is_lensa_vacancy_redirect(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").casefold()
    if host != "lensa.com" and not host.endswith(".lensa.com"):
        return False
    if "unsubscribe" in (parts.path or "").casefold() or "settings" in (parts.path or "").casefold():
        return False
    if host.startswith("jobs."):
        return True
    return any((parts.path or "").startswith(prefix) for prefix in _LENSA_VACANCY_PATH_PREFIXES)


def _is_jobright_vacancy_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").casefold()
    return (host == "jobright.ai" or host.endswith(".jobright.ai")) and bool(re.match(r"^/jobs/info/[A-Za-z0-9_-]+/?$", parts.path or ""))


def _lensa_links(text: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    if re.search(r"<\s*[a-zA-Z]", text):
        parser = _LensaLinkExtractor()
        try:
            parser.feed(text)
            parser.close()
            links.extend(parser.links)
        except Exception:
            pass
    for label, href in MARKDOWN_LINK.findall(text):
        links.append((href, re.sub(r"\s+", " ", label).strip()))
    return [(href, label) for href, label in links if _is_lensa_vacancy_redirect(href) and not _is_control_link(label, href)]


def _match_card_link(company: str, role: str, links: list[tuple[str, str]]) -> str | None:
    for href, label in links:
        low = label.casefold()
        if company.casefold() in low and role.casefold() in low:
            return href
    return None


def _lensa_positional_urls(links: list[tuple[str, str]], vacancy_count: int) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for href, label in links:
        if href in seen or not MONEY.search(label):
            continue
        seen.add(href)
        urls.append(href)
    if vacancy_count < 1 or len(urls) != vacancy_count or len(set(urls)) != len(urls):
        return []
    return urls


def _dedupe_lensa_by_url(cards: list[dict[str, object]]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for card in cards:
        href = str(card.get("apply_url") or "").strip()
        if href:
            if href in seen:
                continue
            seen.add(href)
        out.append(card)
    return out


def _parse_lensa(text: str) -> tuple[list[dict[str, object]], str | None]:
    low = text.casefold()
    terminal = None
    cut = len(text)
    for token in ("gig jobs", "more jobs ➞", "more jobs", "this email has been sent to you by lensa", "unsubscribe"):
        pos = low.find(token)
        if pos >= 0 and pos < cut:
            cut = pos
            terminal = token
    primary = text[:cut]
    links = _lensa_links(primary)
    lines = _lines(_plain(primary))
    out: list[dict[str, object]] = []
    seen_shape: set[tuple[str, str]] = set()
    for i, line in enumerate(lines):
        if not MONEY.search(line):
            continue
        previous = [x for x in lines[max(0, i - 5):i] if not _is_noise(x) and not MONEY.search(x)]
        if len(previous) < 2:
            continue
        company = _clean_candidate(previous[-2])
        role = _clean_candidate(previous[-1])
        if len(company) < 2 or len(role) < 3:
            continue
        key = (company.casefold(), role.casefold())
        if key in seen_shape:
            continue
        seen_shape.add(key)
        tail = " ".join(lines[i:i + 3])
        out.append(
            {
                "company": company,
                "role": role,
                "location": "Remote · United States" if "remote" in tail.casefold() else "United States",
                "work_mode": "Remote" if "remote" in tail.casefold() else "Unknown",
                "compensation": line,
                "apply_url": _match_card_link(company, role, links) if links else None,
            }
        )
    if out and links and all(not item.get("apply_url") for item in out):
        positional = _lensa_positional_urls(links, len(out))
        if positional:
            for card, href in zip(out, positional):
                card["apply_url"] = href
    if not out:
        for href, label in links:
            compact = re.split(MONEY, label, maxsplit=1)[0]
            compact = re.sub(r"\bremote\b", "", compact, flags=re.I).strip(" -–·")
            words = compact.split()
            role_start = next((i for i in range(max(0, len(words) - 5), len(words)) if words[i].casefold() in {"senior", "sr.", "technical", "product", "project", "program", "marketing", "software", "director"}), None)
            if role_start is None or role_start == 0:
                continue
            company = " ".join(words[:role_start])
            role = " ".join(words[role_start:])
            if len(company) >= 2 and len(role) >= 3:
                out.append(
                    {
                        "company": company,
                        "role": role,
                        "location": "Remote · United States" if "remote" in label.casefold() else "United States",
                        "work_mode": "Remote" if "remote" in label.casefold() else "Unknown",
                        "compensation": (MONEY.search(label).group(0) if MONEY.search(label) else "Not disclosed"),
                        "apply_url": href,
                    }
                )
    return _dedupe_lensa_by_url(out), terminal


def _parse_jobright(text: str) -> tuple[list[dict[str, object]], str | None]:
    terminal_pos = text.casefold().find("view more opportunities")
    terminal = "view more opportunities" if terminal_pos >= 0 else None
    primary = text[:terminal_pos] if terminal_pos >= 0 else text
    cards: list[tuple[str, str]] = []
    if re.search(r"<\s*[a-zA-Z]", primary):
        parser = _StructuredLinkExtractor()
        try:
            parser.feed(primary)
            seen: set[str] = set()
            for href, parts in parser.links:
                if _is_jobright_vacancy_url(href) and href not in seen:
                    seen.add(href)
                    cards.append((href, "\n".join(parts)))
        except Exception:
            cards = []
    if not cards:
        seen: set[str] = set()
        for label, href in MARKDOWN_LINK.findall(primary):
            if _is_jobright_vacancy_url(href) and href not in seen:
                seen.add(href)
                cards.append((href, label))
    out: list[dict[str, object]] = []
    for href, label in cards:
        lines = [x for x in _lines(label) if x not in {"Jobright.ai Job Icon", "APPLY NOW"}]
        score_i = next((i for i, x in enumerate(lines) if re.fullmatch(r"\d{2,3}%?", x)), None)
        if score_i is None or score_i < 1:
            out.append({"company": None, "role": None, "location": None, "compensation": None, "apply_url": href})
            continue
        role_i = score_i + 1
        if role_i < len(lines) and lines[role_i] == "%":
            role_i += 1
        if role_i >= len(lines):
            out.append({"company": None, "role": None, "location": None, "compensation": None, "apply_url": href})
            continue
        # Company metadata (e.g. "Advertising · Growth Stage") uses the "·"
        # separator and is excluded structurally by it -- a bare substring
        # check for "stage" also discarded legitimate company names like
        # "Stage 4 Solutions", which is not metadata at all.
        company_candidates = [x for x in lines[:score_i] if "·" not in x and "public company" not in x.casefold()]
        if not company_candidates:
            out.append({"company": None, "role": None, "location": None, "compensation": None, "apply_url": href})
            continue
        tail = lines[role_i + 1:]
        compensation = next((x for x in tail if MONEY.search(x)), "Not disclosed")
        location = "Unknown"
        for item in tail:
            low = item.casefold()
            if MONEY.search(item) or "referral" in low or "ago" in low or "applicant" in low:
                continue
            if len(item) <= 120:
                location = item
                break
        out.append(
            {
                "company": company_candidates[0],
                "role": lines[role_i],
                "provider_score": int(lines[score_i].rstrip("%")),
                "location": location,
                "work_mode": "Remote" if "remote" in location.casefold() else "Unknown",
                "compensation": compensation,
                "apply_url": href,
            }
        )
    return out, terminal


def _extract_preheader_text(raw_mime_html: str) -> str | None:
    tag_match = re.search(r"<[^>]+data-email-preheader\s*=\s*['\"]true['\"][^>]*>", raw_mime_html, re.I)
    if not tag_match:
        return None
    rest = raw_mime_html[tag_match.end():]
    close_match = re.search(r"</[^>]+>", rest)
    if not close_match:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", rest[: close_match.start()]))
    return re.sub(r"\s+", " ", text).strip() or None


def _apply_preheader_description(preheader: str, cards: list[dict[str, object]]) -> None:
    colon_idx = preheader.find(": ")
    if colon_idx < 1:
        return
    prefix = re.sub(r"\s+", " ", preheader[:colon_idx]).strip()
    description = re.sub(r"\s+", " ", preheader[colon_idx + 2:]).strip()
    if not description:
        return
    matches = [
        c for c in cards
        if re.sub(r"\s+", " ", f"{c.get('company', '')} {c.get('role', '')}").strip().casefold() == prefix.casefold()
    ]
    if len(matches) == 1:
        matches[0]["description"] = description


def _linkedin_auxiliary_line(text: str) -> bool:
    low = text.casefold().strip()
    return (
        bool(MONEY.search(text))
        or low in {"this company is actively hiring", "actively recruiting"}
        or bool(re.fullmatch(r"\d+\s+connections?", low))
        or low.startswith("apply with")
        or low == "easy apply"
    )


def _parse_linkedin(
    text: str, *, raw_mime_html: str | None = None, preheader: str | None = None
) -> tuple[list[dict[str, object]], str | None]:
    card_text = re.sub(r"<[^>]+data-email-preheader\s*=\s*['\"]true['\"][^>]*>.*?</[^>]+>", "", text, flags=re.S | re.I) if raw_mime_html else text
    plain = _plain(card_text)
    low = plain.casefold()
    lines = _lines(plain)
    out: list[dict[str, object]] = []
    seen: set[str] = set()

    for i, line in enumerate(lines):
        if "view job:" not in line.casefold():
            continue
        match = JOB_URL.search(line + " " + (lines[i + 1] if i + 1 < len(lines) else ""))
        if not match:
            continue
        job_id = match.group(1)
        if job_id in seen:
            continue
        previous = [x for x in lines[max(0, i - 8):i] if not _is_noise(x)]
        compensation = next((x for x in reversed(previous) if MONEY.search(x)), "Not disclosed")
        while previous and _linkedin_auxiliary_line(previous[-1]):
            previous.pop()
        if len(previous) < 3:
            continue
        location, company, role = previous[-1], previous[-2], _clean_candidate(previous[-3])
        if len(company) < 2 or len(role) < 3:
            continue
        seen.add(job_id)
        out.append(
            {
                "company": company,
                "role": role,
                "provider_job_id": job_id,
                "apply_url": match.group(0),
                "location": location,
                "work_mode": "Remote" if "remote" in location.casefold() else "Unknown",
                "compensation": compensation,
            }
        )

    if not out:
        for label, href in MARKDOWN_LINK.findall(text):
            if "linkedin.com/jobs/view/" not in href.casefold() and "linkedin.com/comm/jobs/view/" not in href.casefold():
                continue
            job_id_match = re.search(r"/jobs/view/(\d+)", href)
            job_id = job_id_match.group(1) if job_id_match else href
            if job_id in seen:
                continue
            lines = [x for x in _lines(label) if not _linkedin_auxiliary_line(x)]
            if len(lines) < 2:
                continue
            role = _clean_candidate(lines[0])
            company_location = lines[1]
            company, _, location = company_location.partition(" · ")
            if not location and len(lines) > 2:
                location = lines[2]
            seen.add(job_id)
            out.append(
                {
                    "company": company.strip(),
                    "role": role,
                    "provider_job_id": job_id,
                    "apply_url": href,
                    "location": location.strip() or "Unknown",
                    "work_mode": "Remote" if "remote" in location.casefold() else "Unknown",
                    "compensation": "Not disclosed",
                }
            )
    if out and preheader:
        _apply_preheader_description(preheader, out)
    terminal = "unsubscribe" if "unsubscribe" in low else ("linkedin-card" if out else None)
    return out, terminal


def _parse_markdown_generic(text: str) -> tuple[list[dict[str, object]], str | None]:
    cards: list[dict[str, object]] = []
    for label, href in MARKDOWN_LINK.findall(text):
        if _is_control_link(label, href):
            continue
        lines = _lines(label)
        if len(lines) >= 3:
            company, role, location = lines[0], lines[1], lines[2]
        elif len(lines) >= 2:
            company, role, location = lines[0], lines[1], "Unknown"
        else:
            continue
        cards.append({"company": company, "role": role, "location": location, "compensation": "Not disclosed", "apply_url": href})
    if not cards and re.search(r"<\s*[a-zA-Z]", text):
        parser = _StructuredLinkExtractor()
        try:
            parser.feed(text)
        except Exception:
            return cards, None
        for href, parts in parser.links:
            label = "\n".join(parts)
            if _is_control_link(label, href):
                continue
            lines = _lines(label)
            if len(lines) >= 3:
                company, role, location = lines[0], lines[1], lines[2]
            elif len(lines) >= 2:
                company, role, location = lines[0], lines[1], "Unknown"
            else:
                continue
            compensation = next((line for line in lines if MONEY.search(line)), "Not disclosed")
            cards.append({"company": company, "role": role, "location": location, "compensation": compensation, "apply_url": href})
    if not cards:
        cards = _parse_preceding_context_linked_cards(text)
    return cards, "generic-links" if cards else None


# Proven by the BridgeView/Boostie production shape: role/location/compensation
# appear as plain lines immediately before a job-specific CTA link (unlike the
# other generic shape above, where the card content lives inside the anchor
# itself). Only this exact CTA text is recognized -- do not broaden beyond
# this evidence. Never fabricate a company name; the source genuinely does
# not supply one, and downstream Jobs identity already tolerates that.
_LINKED_CARD_CTA_LABELS = ("view this job",)


def _parse_preceding_context_linked_cards(text: str) -> list[dict[str, object]]:
    if not re.search(r"<\s*[a-zA-Z]", text):
        return []
    parser = _StructuredLinkExtractor()
    try:
        parser.feed(text)
    except Exception:
        return []
    job_links = [
        href
        for href, parts in parser.links
        if re.sub(r"\s+", " ", " ".join(parts)).strip().casefold() in _LINKED_CARD_CTA_LABELS
    ]
    if not job_links or len(set(job_links)) != len(job_links):
        return []
    plain_lines = _lines(_plain(text))
    marker_indices = [i for i, line in enumerate(plain_lines) if line.strip().casefold() in _LINKED_CARD_CTA_LABELS]
    if len(marker_indices) != len(job_links):
        return []
    cards: list[dict[str, object]] = []
    prev_marker_end = -1
    for idx, href in zip(marker_indices, job_links):
        context = [x for x in plain_lines[max(prev_marker_end + 1, idx - 4) : idx] if not _is_noise(x)]
        prev_marker_end = idx
        compensation = None
        if context and MONEY.search(context[-1]):
            compensation = context[-1]
            context = context[:-1]
        if len(context) < 2:
            continue
        location, role = context[-1], _clean_candidate(context[-2])
        if len(role) < 3 or len(location) < 2:
            continue
        cards.append(
            {
                "company": None,
                "role": role,
                "location": location,
                "compensation": compensation or "Not disclosed",
                "apply_url": href,
            }
        )
    return cards
