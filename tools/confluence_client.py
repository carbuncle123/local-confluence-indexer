"""Confluence API client."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv
from requests import Response, Session

from utils import DEFAULT_INDEX_DIR, DEFAULT_SYNC_DIR


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS_CODES = {401, 403}


@dataclass(slots=True)
class ConfluenceConfig:
    """Runtime configuration for Confluence sync."""

    base_url: str
    bearer_token: str
    default_space: str | None
    docs_dir: str
    sync_dir: str
    index_dir: str
    incremental_overlap_minutes: int
    request_timeout_seconds: int
    max_retries: int


class ConfluenceClientError(RuntimeError):
    """Raised when a Confluence API request fails."""


class ConfluenceAuthenticationError(ConfluenceClientError):
    """Raised on 401/403 responses."""


def load_config(
    *,
    space_key: str | None = None,
    base_url: str | None = None,
    bearer_token: str | None = None,
) -> ConfluenceConfig:
    """Load configuration from .env and environment variables."""

    import os

    load_dotenv()

    config = ConfluenceConfig(
        base_url=(base_url or os.getenv("CONFLUENCE_BASE_URL", "")).rstrip("/"),
        bearer_token=bearer_token or os.getenv("CONFLUENCE_BEARER_TOKEN", ""),
        default_space=space_key or os.getenv("CONFLUENCE_DEFAULT_SPACE"),
        docs_dir=os.getenv("CONFLUENCE_DOCS_DIR", "docs/confluence"),
        sync_dir=os.getenv("CONFLUENCE_SYNC_DIR", str(DEFAULT_SYNC_DIR)),
        index_dir=os.getenv("DOC_INDEX_DIR", str(DEFAULT_INDEX_DIR)),
        incremental_overlap_minutes=int(
            os.getenv("CONFLUENCE_INCREMENTAL_OVERLAP_MINUTES", "30")
        ),
        request_timeout_seconds=int(os.getenv("CONFLUENCE_REQUEST_TIMEOUT_SECONDS", "30")),
        max_retries=int(os.getenv("CONFLUENCE_MAX_RETRIES", "5")),
    )

    if not config.base_url:
        raise ValueError("CONFLUENCE_BASE_URL is required.")
    if not config.bearer_token:
        raise ValueError("CONFLUENCE_BEARER_TOKEN is required.")

    return config


def build_bearer_auth_header(token: str) -> str:
    """Build a Bearer auth header for Confluence API access."""

    return f"Bearer {token}"


def format_cql_since(timestamp: str, overlap_minutes: int) -> str:
    """Format a since timestamp for CQL with configured overlap."""

    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    adjusted = parsed - timedelta(minutes=overlap_minutes)
    return adjusted.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


class ConfluenceClient:
    """Thin API wrapper around the Confluence REST APIs used by sync."""

    def __init__(
        self,
        config: ConfluenceConfig,
        session: Session | None = None,
        sleep_func: Any = time.sleep,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.sleep_func = sleep_func
        self.session.headers.update(
            {
                "Authorization": build_bearer_auth_header(config.bearer_token),
                "Accept": "application/json",
                "User-Agent": "local-confluence-indexer/0.1.0",
            }
        )

    def _request_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.config.request_timeout_seconds,
                )
                if response.status_code in NON_RETRYABLE_STATUS_CODES:
                    self._raise_authentication_error(response)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    self._sleep_for_retry(response, attempt)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.Timeout as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                self._sleep_without_response(attempt)
            except requests.RequestException as exc:
                last_error = exc
                status_code = getattr(exc.response, "status_code", None)
                if status_code in NON_RETRYABLE_STATUS_CODES:
                    raise self._auth_exception_from_response(exc.response) from exc
                if status_code not in RETRYABLE_STATUS_CODES or attempt >= self.config.max_retries:
                    break
                self._sleep_without_response(attempt)

        raise ConfluenceClientError(f"Confluence API request failed: {url}") from last_error

    def _sleep_for_retry(self, response: Response, attempt: int) -> None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                self.sleep_func(max(float(retry_after), 0.0))
                return
            except ValueError:
                pass
        self._sleep_without_response(attempt)

    def _sleep_without_response(self, attempt: int) -> None:
        delay = min(2**attempt, 30) + random.uniform(0, 0.25)
        self.sleep_func(delay)

    def _raise_authentication_error(self, response: Response) -> None:
        raise self._auth_exception_from_response(response)

    def _auth_exception_from_response(self, response: Response | None) -> ConfluenceAuthenticationError:
        if response is None:
            return ConfluenceAuthenticationError("Authentication failed.")
        if response.status_code == 401:
            message = "Confluence authentication failed. Confirm base URL and bearer token."
        else:
            message = "Confluence access was denied. Confirm space/page permissions."
        return ConfluenceAuthenticationError(message)

    def _paginate(self, url: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params = params

        while next_url:
            payload = self._request_json(next_url, params=next_params)
            results.extend(payload.get("results", []))
            next_url = self._extract_next_url(payload)
            next_params = None

        return results

    def _extract_next_url(self, payload: dict[str, Any]) -> str | None:
        links = payload.get("_links") or {}
        next_link = links.get("next")
        if not next_link:
            return None
        parsed = urlparse(next_link)
        if parsed.scheme and parsed.netloc:
            return next_link
        return urljoin(self.config.base_url, next_link)

    def get_space_by_key(self, space_key: str) -> dict[str, Any]:
        """Resolve a Confluence space by key."""

        url = f"{self.config.base_url}/rest/api/space/{space_key}"
        payload = self._request_json(url, params={"expand": "homepage"})
        return self._normalize_space(payload)

    def list_pages_in_space(
        self,
        *,
        space_key: str,
        status: str = "current",
        body_format: str = "storage",
    ) -> list[dict[str, Any]]:
        """List page summaries for a space."""

        del body_format
        url = f"{self.config.base_url}/rest/api/space/{space_key}/content/page"
        results = self._paginate(
            url,
            params={
                "status": status,
                "expand": "version",
                "limit": 100,
            },
        )
        return [self._normalize_content_summary(item, space_key=space_key) for item in results]

    def get_page_detail(
        self,
        page_id: str,
        *,
        body_format: str = "storage",
        include_labels: bool = True,
        include_version: bool = True,
    ) -> dict[str, Any]:
        """Fetch a page detail payload."""

        del body_format, include_labels, include_version
        url = f"{self.config.base_url}/rest/api/content/{page_id}"
        payload = self._request_json(
            url,
            params={
                "expand": ",".join(
                    [
                        "body.storage",
                        "version",
                        "metadata.labels",
                        "space",
                        "history",
                    ]
                )
            },
        )
        return self._normalize_content(payload)

    def search_pages_by_cql(self, cql: str) -> list[dict[str, Any]]:
        """Search pages using the legacy CQL endpoint."""

        url = f"{self.config.base_url}/rest/api/content/search"
        return self._paginate(url, params={"cql": cql, "limit": 25})

    def search_updated_pages_by_cql(self, space_key: str, since: str) -> list[dict[str, Any]]:
        """Search updated pages using the legacy CQL endpoint."""

        cql = (
            f'space = "{space_key}" and type = page and '
            f'lastmodified >= "{since}" order by lastmodified asc'
        )
        return self.search_pages_by_cql(cql)

    def search_updated_pages_in_page_tree(
        self,
        *,
        space_key: str,
        root_page_id: str,
        since: str,
    ) -> list[dict[str, Any]]:
        """Search updated pages under a page-tree target, including the root page."""

        cql = (
            f'space = "{space_key}" and type = page and '
            f'(ancestor = {root_page_id} or id = {root_page_id}) and '
            f'lastmodified >= "{since}" order by lastmodified asc'
        )
        return self.search_pages_by_cql(cql)

    def list_descendant_pages(
        self,
        root_page_id: str,
        *,
        space_key: str,
    ) -> list[dict[str, Any]]:
        """List descendant page summaries for a root page."""

        url = f"{self.config.base_url}/rest/api/content/{root_page_id}/descendant/page"
        results = self._paginate(
            url,
            params={
                "expand": "version",
                "limit": 100,
            },
        )
        return [self._normalize_content_summary(item, space_key=space_key) for item in results]

    def _normalize_space(self, payload: dict[str, Any]) -> dict[str, Any]:
        homepage = payload.get("homepage") or {}
        return {
            "id": str(payload.get("id", "")),
            "key": payload.get("key"),
            "name": payload.get("name"),
            "homepageId": str(homepage.get("id")) if homepage.get("id") is not None else None,
            "_links": payload.get("_links", {}),
        }

    def _normalize_content_summary(
        self,
        payload: dict[str, Any],
        *,
        space_key: str,
    ) -> dict[str, Any]:
        version = payload.get("version") or {}
        return {
            "id": str(payload.get("id", "")),
            "title": payload.get("title"),
            "status": payload.get("status"),
            "space_key": space_key,
            "version": {
                "number": version.get("number"),
            },
        }

    def _normalize_content(self, payload: dict[str, Any]) -> dict[str, Any]:
        version = payload.get("version") or {}
        history = payload.get("history") or {}
        created_by = history.get("createdBy") or {}
        space = payload.get("space") or {}
        metadata = payload.get("metadata") or {}
        labels = metadata.get("labels") or {}
        links = payload.get("_links") or {}

        return {
            "id": str(payload.get("id", "")),
            "type": payload.get("type"),
            "status": payload.get("status"),
            "title": payload.get("title"),
            "spaceId": str(space.get("id", "")) if space.get("id") is not None else "",
            "space_key": space.get("key"),
            "parentId": None,
            "authorId": created_by.get("accountId"),
            "ownerId": created_by.get("accountId"),
            "createdAt": history.get("createdDate"),
            "version": {
                "number": version.get("number"),
                "createdAt": version.get("when"),
                "message": version.get("message"),
                "minorEdit": version.get("minorEdit"),
                "authorId": (version.get("by") or {}).get("accountId"),
            },
            "body": {
                "storage": {
                    "value": ((payload.get("body") or {}).get("storage") or {}).get("value", "")
                }
            },
            "labels": {
                "results": labels.get("results", []),
            },
            "_links": {
                "base": links.get("base") or self.config.base_url,
                "webui": links.get("webui"),
            },
        }
