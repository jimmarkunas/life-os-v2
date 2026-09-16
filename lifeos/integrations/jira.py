"""Thin Jira Cloud transport for bounded issue reads and mutations only."""
from __future__ import annotations

import base64
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlencode

from lifeos.core.config import RuntimeConfig
from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext

_READ_RETRY = RetryPolicy(max_attempts=2, backoff_seconds=0.1, max_backoff_seconds=1.0)
_NO_RETRY = RetryPolicy(max_attempts=1)
JIRA_BASE_URL_FIELD = "JIRA_BASE_URL"
JIRA_ACCESS_TOKEN_FIELD = "JIRA_API_TOKEN"
JIRA_USER_EMAIL_FIELD = "JIRA_USER_EMAIL"
DEFAULT_MAX_SEARCH_PAGES = 10
MAX_SEARCH_PAGES = 20
MAX_SEARCH_PAGE_SIZE = 100


class JiraTransportError(RuntimeError):
    """Safe Jira transport error; planning/business policy belongs to callers."""


class JiraTransport:
    def __init__(
        self,
        *,
        context: RunContext,
        http: HttpClient,
        base_url: str,
        access_token: str,
        user_email: str | None = None,
        max_search_pages: int = DEFAULT_MAX_SEARCH_PAGES,
    ) -> None:
        normalized_base = str(base_url).strip().rstrip("/")
        if not normalized_base:
            raise ValueError("Jira base URL is required")
        if not access_token:
            raise ValueError("Jira access token is required")
        pages = int(max_search_pages)
        if pages < 1 or pages > MAX_SEARCH_PAGES:
            raise ValueError(f"max_search_pages must be between 1 and {MAX_SEARCH_PAGES}")
        self._context = context
        self._http = http
        self._base_url = normalized_base
        self._token = access_token
        self._user_email = str(user_email).strip() if user_email else None
        self._max_search_pages = pages

    @classmethod
    def from_config(
        cls,
        *,
        context: RunContext,
        http: HttpClient,
        config: RuntimeConfig,
        base_url_field: str = JIRA_BASE_URL_FIELD,
        access_token_field: str = JIRA_ACCESS_TOKEN_FIELD,
        user_email_field: str = JIRA_USER_EMAIL_FIELD,
        max_search_pages: int = DEFAULT_MAX_SEARCH_PAGES,
    ) -> "JiraTransport":
        """Construct from already-validated private runtime configuration."""
        return cls(
            context=context,
            http=http,
            base_url=config.require(base_url_field),
            access_token=config.require(access_token_field),
            user_email=config.optional(user_email_field),
            max_search_pages=max_search_pages,
        )

    def get_issue(
        self,
        issue_id_or_key: str,
        *,
        fields: Sequence[str] = (),
    ) -> dict[str, Any]:
        issue_ref = _required_ref(issue_id_or_key, "issue_id_or_key")
        url = f"{self._base_url}/rest/api/3/issue/{quote(issue_ref, safe='')}"
        if fields:
            url = f"{url}?{urlencode({'fields': ','.join(str(field) for field in fields)})}"
        payload = self._http.request_json(
            self._context,
            "GET",
            url,
            headers=self._headers(),
            timeout_seconds=10.0,
            retry=_READ_RETRY,
        )
        return _require_object(payload, "Jira issue response")

    def search_issues(
        self,
        jql: str,
        *,
        fields: Sequence[str] = (),
        page_size: int = 50,
    ) -> tuple[dict[str, Any], ...]:
        if not str(jql).strip():
            raise ValueError("jql is required")
        size = max(1, min(int(page_size), MAX_SEARCH_PAGE_SIZE))
        url = f"{self._base_url}/rest/api/3/search/jql"
        next_token: str | None = None
        seen_tokens: set[str] = set()
        issues: list[dict[str, Any]] = []

        for _ in range(self._max_search_pages):
            body: dict[str, Any] = {"jql": jql, "maxResults": size}
            if fields:
                body["fields"] = [str(field) for field in fields]
            if next_token:
                body["nextPageToken"] = next_token
            payload = self._http.request_json(
                self._context,
                "POST",
                url,
                headers=self._headers(),
                json_body=body,
                timeout_seconds=10.0,
                retry=_READ_RETRY,
            )
            page = _require_object(payload, "Jira search response")
            for item in page.get("issues") or []:
                if isinstance(item, dict):
                    issues.append(item)
            token = page.get("nextPageToken")
            if page.get("isLast") is True or not token:
                return tuple(issues)
            next_token = str(token)
            if next_token in seen_tokens:
                raise JiraTransportError("Jira pagination token repeated")
            seen_tokens.add(next_token)

        raise JiraTransportError("Jira search exceeded configured page limit")

    def create_issue(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        if not fields:
            raise ValueError("issue fields are required")
        payload = self._http.request_json(
            self._context,
            "POST",
            f"{self._base_url}/rest/api/3/issue",
            headers=self._headers(),
            json_body={"fields": dict(fields)},
            timeout_seconds=10.0,
            retry=_NO_RETRY,
            expected_statuses=(200, 201),
        )
        created = _require_object(payload, "Jira create response")
        issue_ref = created.get("key") or created.get("id")
        if not issue_ref:
            raise JiraTransportError("Jira create response missing issue identity")
        return self.get_issue(str(issue_ref))

    def update_issue(self, issue_id_or_key: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        issue_ref = _required_ref(issue_id_or_key, "issue_id_or_key")
        if not fields:
            raise ValueError("issue fields are required")
        self._http.request_json(
            self._context,
            "PUT",
            f"{self._base_url}/rest/api/3/issue/{quote(issue_ref, safe='')}",
            headers=self._headers(),
            json_body={"fields": dict(fields)},
            timeout_seconds=10.0,
            retry=_NO_RETRY,
            expected_statuses=(200, 204),
        )
        return self.get_issue(issue_ref)

    def _headers(self) -> dict[str, str]:
        if self._user_email:
            raw = f"{self._user_email}:{self._token}".encode("utf-8")
            authorization = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            authorization = f"Bearer {self._token}"
        return {
            "Authorization": authorization,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }


def _required_ref(value: str, label: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _require_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise JiraTransportError(f"{label} was not an object")
    return payload
