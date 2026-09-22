"""Generic narrow Notion transport used by domain-owned persistence policy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

from lifeos.core.config import RuntimeConfig
from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext

_NOTION_API = "https://api.notion.com/v1"
_DEFAULT_NOTION_VERSION = "2026-03-11"
_READ_RETRY = RetryPolicy(max_attempts=2, backoff_seconds=0.1, max_backoff_seconds=1.0)
_NO_RETRY = RetryPolicy(max_attempts=1)
_MAX_IDENTITY_VALUES = 50
_ALLOWED_PROPERTY_TYPES = frozenset({"title", "rich_text", "url", "select", "email", "phone_number"})
NOTION_ACCESS_TOKEN_FIELD = "NOTION_API_TOKEN"
DEFAULT_MAX_QUERY_PAGES = 10
MAX_QUERY_PAGES = 20


class NotionTransportError(RuntimeError):
    """Safe transport error; domain mapping/persistence policy lives elsewhere."""


@dataclass(frozen=True, slots=True)
class NotionIdentityQuery:
    property_name: str
    property_type: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.property_name:
            raise ValueError("property_name is required")
        if self.property_type not in _ALLOWED_PROPERTY_TYPES:
            raise ValueError("unsupported identity property type")
        if not self.values:
            raise ValueError("identity query requires at least one value")
        if len(self.values) > _MAX_IDENTITY_VALUES:
            raise ValueError(f"identity query exceeds {_MAX_IDENTITY_VALUES} values")
        if any(not str(value) for value in self.values):
            raise ValueError("identity values must be non-empty")

    def filter_payload(self) -> Mapping[str, Any]:
        filters = [
            {"property": self.property_name, self.property_type: {"equals": value}}
            for value in self.values
        ]
        return filters[0] if len(filters) == 1 else {"or": filters}


class NotionTransport:
    def __init__(
        self,
        *,
        context: RunContext,
        http: HttpClient,
        access_token: str,
        notion_version: str = _DEFAULT_NOTION_VERSION,
        max_query_pages: int = DEFAULT_MAX_QUERY_PAGES,
    ) -> None:
        if not access_token:
            raise ValueError("Notion access token is required")
        if not notion_version:
            raise ValueError("Notion API version is required")
        pages = int(max_query_pages)
        if pages < 1 or pages > MAX_QUERY_PAGES:
            raise ValueError(f"max_query_pages must be between 1 and {MAX_QUERY_PAGES}")
        self._context = context
        self._http = http
        self._token = access_token
        self._version = notion_version
        self._max_query_pages = pages

    @classmethod
    def from_config(
        cls,
        *,
        context: RunContext,
        http: HttpClient,
        config: RuntimeConfig,
        access_token_field: str = NOTION_ACCESS_TOKEN_FIELD,
        notion_version: str = _DEFAULT_NOTION_VERSION,
        max_query_pages: int = DEFAULT_MAX_QUERY_PAGES,
    ) -> "NotionTransport":
        """Construct from already-validated runtime configuration."""
        return cls(
            context=context,
            http=http,
            access_token=config.require(access_token_field),
            notion_version=notion_version,
            max_query_pages=max_query_pages,
        )

    def query_data_source(
        self,
        data_source_id: str,
        identity: NotionIdentityQuery,
        *,
        page_size: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        if not data_source_id:
            raise ValueError("data_source_id is required")
        size = max(1, min(int(page_size), 100))
        url = f"{_NOTION_API}/data_sources/{quote(data_source_id, safe='')}/query"
        cursor: str | None = None
        seen_cursors: set[str] = set()
        results: list[dict[str, Any]] = []
        for _page_number in range(1, self._max_query_pages + 1):
            body: dict[str, Any] = {"page_size": size, "filter": identity.filter_payload()}
            if cursor:
                if cursor in seen_cursors:
                    raise NotionTransportError("Notion pagination cursor repeated")
                seen_cursors.add(cursor)
                body["start_cursor"] = cursor
            payload = self._request_json("query_data_source", _READ_RETRY,
                "POST",
                url,
                headers=self._headers(),
                json_body=body,
                timeout_seconds=10.0,
            )
            if not isinstance(payload, dict):
                raise NotionTransportError("Notion query response was not an object")
            for result in payload.get("results") or []:
                if isinstance(result, dict):
                    results.append(result)
            if not payload.get("has_more"):
                return tuple(results)
            next_cursor = payload.get("next_cursor")
            if not next_cursor:
                raise NotionTransportError("Notion pagination missing next cursor")
            cursor = str(next_cursor)
        raise NotionTransportError("Notion query exceeded pagination limit")

    def create_page(self, data_source_id: str, properties: Mapping[str, Any]) -> dict[str, Any]:
        if not data_source_id:
            raise ValueError("data_source_id is required")
        payload = self._request_json("create_page", _NO_RETRY,
            "POST",
            f"{_NOTION_API}/pages",
            headers=self._headers(),
            json_body={"parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": dict(properties)},
            timeout_seconds=10.0,
        )
        return _require_object(payload, "Notion create response")

    def update_page(self, page_id: str, properties: Mapping[str, Any]) -> dict[str, Any]:
        if not page_id:
            raise ValueError("page_id is required")
        payload = self._request_json("update_page", _NO_RETRY,
            "PATCH",
            f"{_NOTION_API}/pages/{quote(page_id, safe='')}",
            headers=self._headers(),
            json_body={"properties": dict(properties)},
            timeout_seconds=10.0,
        )
        return _require_object(payload, "Notion update response")

    def get_page(self, page_id: str) -> dict[str, Any]:
        if not page_id:
            raise ValueError("page_id is required")
        payload = self._request_json("get_page", _READ_RETRY,
            "GET",
            f"{_NOTION_API}/pages/{quote(page_id, safe='')}",
            headers=self._headers(),
            timeout_seconds=10.0,
        )
        return _require_object(payload, "Notion read-back response")

    def _request_json(self, operation: str, retry: RetryPolicy, method: str, url: str, **kwargs: Any) -> Any:
        try:
            return self._http.request_json(self._context, method, url, retry=retry, **kwargs)
        except Exception as exc:
            if hasattr(exc, "kind"):
                exc.operation = operation
                exc.retry_limit = retry.max_attempts
                exc.endpoint_family = "notion"
            raise

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self._version,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "life-os-v2/1.0",
        }


def _require_object(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise NotionTransportError(f"{label} was not an object")
    return payload
