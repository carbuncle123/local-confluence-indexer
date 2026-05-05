"""Confluence API client."""

from __future__ import annotations

import base64
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

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
    email: str
    api_token: str
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
    email: str | None = None,
    api_token: str | None = None,
) -> ConfluenceConfig:
    """Load configuration from .env and environment variables."""

    import os

    load_dotenv()

    config = ConfluenceConfig(
        base_url=(base_url or os.getenv("CONFLUENCE_BASE_URL", "")).rstrip("/"),
        email=email or os.getenv("CONFLUENCE_EMAIL", ""),
        api_token=api_token or os.getenv("CONFLUENCE_API_TOKEN", ""),
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
    if not config.email:
        raise ValueError("CONFLUENCE_EMAIL is required.")
    if not config.api_token:
        raise ValueError("CONFLUENCE_API_TOKEN is required.")

    return config


def build_basic_auth_header(email: str, api_token: str) -> str:
    """Build a Basic auth header for Confluence Cloud."""

    token = base64.b64encode(f"{email}:{api_token}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


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
                "Authorization": build_basic_auth_header(config.email, config.api_token),
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
            message = "Confluence authentication failed. Confirm base URL, email, and API token."
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

        url = f"{self.config.base_url}/wiki/api/v2/spaces"
        payload = self._request_json(url, params={"keys": space_key, "limit": 25})
        for result in payload.get("results", []):
            if result.get("key") == space_key:
                return result
        raise ConfluenceClientError(f"Space not found: {space_key}")

    def list_pages_in_space(
        self,
        *,
        space_id: str,
        status: str = "current",
        body_format: str = "storage",
    ) -> list[dict[str, Any]]:
        """List page summaries for a space."""

        url = f"{self.config.base_url}/wiki/api/v2/pages"
        return self._paginate(
            url,
            params={
                "space-id": space_id,
                "status": status,
                "body-format": body_format,
                "limit": 100,
            },
        )

    def get_page_detail(
        self,
        page_id: str,
        *,
        body_format: str = "storage",
        include_labels: bool = True,
        include_version: bool = True,
    ) -> dict[str, Any]:
        """Fetch a page detail payload."""

        url = f"{self.config.base_url}/wiki/api/v2/pages/{page_id}"
        return self._request_json(
            url,
            params={
                "body-format": body_format,
                "include-labels": str(include_labels).lower(),
                "include-version": str(include_version).lower(),
            },
        )

    def search_updated_pages_by_cql(self, space_key: str, since: str) -> list[dict[str, Any]]:
        """Search updated pages using the legacy CQL endpoint."""

        cql = (
            f'space = "{space_key}" and type = page and '
            f'lastmodified >= "{since}" order by lastmodified asc'
        )
        url = f"{self.config.base_url}/wiki/rest/api/search"
        return self._paginate(url, params={"cql": cql, "limit": 25})
