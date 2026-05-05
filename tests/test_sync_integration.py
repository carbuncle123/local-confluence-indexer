from __future__ import annotations

import os
import tempfile
from pathlib import Path

from confluence_client import ConfluenceConfig
from sync_confluence import run_full_sync


class FakeClient:
    def get_space_by_key(self, space_key: str) -> dict:
        return {"id": "10", "key": space_key, "name": "Project A"}

    def list_pages_in_space(self, **kwargs) -> list[dict]:
        return [{"id": "123"}]

    def get_page_detail(self, page_id: str, **kwargs) -> dict:
        return {
            "id": "123",
            "space_key": "PROJECT_A",
            "spaceId": "10",
            "title": "認証API仕様",
            "status": "current",
            "parentId": "1",
            "authorId": "u1",
            "ownerId": "u1",
            "createdAt": "2026-05-05T00:00:00.000Z",
            "version": {
                "number": 2,
                "createdAt": "2026-05-05T01:00:00.000Z",
                "message": "msg",
                "minorEdit": False,
                "authorId": "u1",
            },
            "body": {"storage": {"value": "<h1>見出し</h1><p>Hello</p>"}},
            "labels": {"results": [{"name": "official"}]},
            "_links": {
                "base": "https://example.atlassian.net",
                "webui": "/wiki/spaces/PROJECT_A/pages/123",
            },
        }


def test_run_full_sync_generates_expected_artifacts(monkeypatch) -> None:
    base = Path(tempfile.mkdtemp(prefix="phase5-sync-"))
    monkeypatch.chdir(base)

    config = ConfluenceConfig(
        base_url="https://example.atlassian.net",
        bearer_token="token",
        default_space="PROJECT_A",
        docs_dir="docs/confluence",
        sync_dir=".local-confluence-sync",
        index_dir=".local-doc-index",
        incremental_overlap_minutes=30,
        request_timeout_seconds=30,
        max_retries=1,
    )

    rc = run_full_sync(
        client=FakeClient(),
        config=config,
        space_key="PROJECT_A",
        force=False,
        reindex=False,
    )

    assert rc == 0
    assert (base / "docs/confluence/PROJECT_A/index.md").exists()
    assert (base / "docs/confluence/PROJECT_A/manifest.jsonl").exists()
    assert (base / ".local-confluence-sync/raw/PROJECT_A/123.page.json").exists()
