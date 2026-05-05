from __future__ import annotations

import os
import tempfile
from pathlib import Path

from confluence_client import ConfluenceConfig
from db import connect_state_db, get_sync_state, list_page_targets_for_target
from sync_confluence import run_full_sync, run_incremental_sync


class FakeClient:
    def __init__(self) -> None:
        self.descendant_ids = ["123", "124"]
        self.updated_tree_ids = ["100", "124"]
        self.version_overrides: dict[str, int] = {}

    def get_space_by_key(self, space_key: str) -> dict:
        return {"id": "10", "key": space_key, "name": "Project A"}

    def list_pages_in_space(self, **kwargs) -> list[dict]:
        return [{"id": "123"}]

    def list_descendant_pages(self, root_page_id: str, *, space_key: str) -> list[dict]:
        assert root_page_id == "100"
        assert space_key == "PROJECT_A"
        return [{"id": page_id} for page_id in self.descendant_ids]

    def search_updated_pages_in_page_tree(
        self,
        *,
        space_key: str,
        root_page_id: str,
        since: str,
    ) -> list[dict]:
        assert space_key == "PROJECT_A"
        assert root_page_id == "100"
        assert since
        return [{"content": {"id": page_id}} for page_id in self.updated_tree_ids]

    def get_page_detail(self, page_id: str, **kwargs) -> dict:
        if page_id == "100":
            title = "認証仕様トップ"
            parent_id = None
        elif page_id == "124":
            title = "トークン更新"
            parent_id = "100"
        else:
            title = "認証API仕様"
            parent_id = "100" if page_id == "123" else "1"
        return {
            "id": page_id,
            "space_key": "PROJECT_A",
            "spaceId": "10",
            "title": title,
            "status": "current",
            "parentId": parent_id,
            "authorId": "u1",
            "ownerId": "u1",
            "createdAt": "2026-05-05T00:00:00.000Z",
            "version": {
                "number": self.version_overrides.get(page_id, 2),
                "createdAt": "2026-05-05T01:00:00.000Z",
                "message": "msg",
                "minorEdit": False,
                "authorId": "u1",
            },
            "body": {"storage": {"value": "<h1>見出し</h1><p>Hello</p>"}},
            "labels": {"results": [{"name": "official"}]},
            "_links": {
                "base": "https://example.atlassian.net",
                "webui": f"/wiki/spaces/PROJECT_A/pages/{page_id}",
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
        root_page_id=None,
        force=False,
        reindex=False,
    )

    assert rc == 0
    assert (base / "docs/confluence/PROJECT_A/index.md").exists()
    assert (base / "docs/confluence/PROJECT_A/manifest.jsonl").exists()
    assert (base / ".local-confluence-sync/raw/PROJECT_A/123.page.json").exists()


def test_run_full_sync_for_page_tree_generates_target_artifacts(monkeypatch) -> None:
    base = Path(tempfile.mkdtemp(prefix="phase8-page-tree-"))
    monkeypatch.chdir(base)
    client = FakeClient()

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
        client=client,
        config=config,
        space_key="PROJECT_A",
        root_page_id="100",
        force=False,
        reindex=False,
    )

    target_dir = base / "docs/confluence/PROJECT_A/targets/page-tree--100"

    assert rc == 0
    assert (target_dir / "index.md").exists()
    assert (target_dir / "manifest.jsonl").exists()
    assert len(list((base / "docs/confluence/PROJECT_A/pages").glob("100__*.md"))) == 1
    assert len(list((base / "docs/confluence/PROJECT_A/pages").glob("123__*.md"))) == 1
    assert len(list((base / "docs/confluence/PROJECT_A/pages").glob("124__*.md"))) == 1

    with connect_state_db(base / ".local-confluence-sync/state.db") as state_conn:
        state = get_sync_state(
            state_conn,
            "PROJECT_A",
            target_type="page_tree",
            root_page_id="100",
        )
        memberships = list_page_targets_for_target(state_conn, state["target_key"])

        assert state["target_key"] == "page_tree:PROJECT_A:100"
        assert len(memberships) == 3
        assert {item["page_id"] for item in memberships} == {"100", "123", "124"}


def test_run_incremental_sync_for_page_tree_refreshes_memberships(monkeypatch) -> None:
    base = Path(tempfile.mkdtemp(prefix="phase8-page-tree-incremental-"))
    monkeypatch.chdir(base)
    client = FakeClient()

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

    assert (
        run_full_sync(
            client=client,
            config=config,
            space_key="PROJECT_A",
            root_page_id="100",
            force=False,
            reindex=False,
        )
        == 0
    )

    client.descendant_ids = ["124"]
    client.updated_tree_ids = ["124"]
    client.version_overrides["124"] = 3

    rc = run_incremental_sync(
        client=client,
        config=config,
        space_key="PROJECT_A",
        root_page_id="100",
        force=False,
        reindex=False,
        dry_run=False,
    )

    target_dir = base / "docs/confluence/PROJECT_A/targets/page-tree--100"
    manifest_text = (target_dir / "manifest.jsonl").read_text(encoding="utf-8")

    assert rc == 0
    assert '"page_id": "123"' not in manifest_text
    assert '"page_id": "124"' in manifest_text

    with connect_state_db(base / ".local-confluence-sync/state.db") as state_conn:
        state = get_sync_state(
            state_conn,
            "PROJECT_A",
            target_type="page_tree",
            root_page_id="100",
        )
        memberships = list_page_targets_for_target(state_conn, state["target_key"])

        assert {item["page_id"] for item in memberships} == {"100", "124"}
