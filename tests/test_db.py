from __future__ import annotations

from pathlib import Path

from db import (
    ChunkRecord,
    DocumentRecord,
    PageTargetRecord,
    PageRecord,
    SpaceRecord,
    SyncErrorRecord,
    SyncRunRecord,
    build_target_record,
    build_page_tree_target_key,
    build_space_target_key,
    connect_index_db,
    connect_state_db,
    create_sync_run,
    get_document,
    get_page,
    get_space,
    get_sync_run,
    get_sync_state,
    get_sync_target,
    initialize_all_databases,
    initialize_state_db,
    list_chunks_for_document,
    list_page_targets_for_target,
    list_sync_errors,
    list_sync_targets_for_space,
    replace_page_targets_for_target,
    replace_chunks_for_document,
    record_sync_started,
    upsert_document,
    upsert_page,
    upsert_space,
    upsert_sync_target,
    record_sync_error,
)


def test_state_and_index_db_roundtrip(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    index_db = tmp_path / "docs.db"
    initialize_all_databases(state_db, index_db)

    with connect_state_db(state_db) as state_conn:
        upsert_space(state_conn, SpaceRecord(space_key="PROJECT_A", space_id="10", name="Project A"))
        upsert_page(
            state_conn,
            PageRecord(
                page_id="123",
                space_key="PROJECT_A",
                space_id="10",
                title="認証API仕様",
                local_path="docs/confluence/PROJECT_A/pages/123__auth.md",
                version_number=2,
                content_hash="sha256:test",
                fetched_at="2026-05-05T02:00:00+00:00",
                last_seen_at="2026-05-05T02:00:00+00:00",
            ),
        )
        create_sync_run(
            state_conn,
            SyncRunRecord(
                run_id="run-1",
                space_key="PROJECT_A",
                mode="full",
                started_at="2026-05-05T00:00:00+00:00",
                status="running",
            ),
        )
        record_sync_error(
            state_conn,
            SyncErrorRecord(
                run_id="run-1",
                space_key="PROJECT_A",
                operation="sync_page",
                error_message="boom",
                created_at="2026-05-05T00:00:01+00:00",
                page_id="123",
                error_type="RuntimeError",
            ),
        )

        upsert_sync_target(
            state_conn,
            target_record := build_target_record(
                space_key="PROJECT_A",
                space_id="10",
            ),
        )
        record_sync_started(state_conn, "PROJECT_A", "10")
        replace_page_targets_for_target(
            state_conn,
            build_space_target_key("PROJECT_A"),
            [
                PageTargetRecord(
                    target_key=build_space_target_key("PROJECT_A"),
                    page_id="123",
                    space_key="PROJECT_A",
                    last_seen_at="2026-05-05T02:00:00+00:00",
                )
            ],
        )

        assert get_space(state_conn, "PROJECT_A")["space_id"] == "10"
        assert get_page(state_conn, "123")["title"] == "認証API仕様"
        assert get_sync_target(state_conn, target_record.target_key)["space_id"] == "10"
        assert get_sync_state(state_conn, "PROJECT_A")["target_key"] == "space:PROJECT_A"
        assert get_sync_run(state_conn, "run-1")["status"] == "running"
        assert get_sync_run(state_conn, "run-1")["target_key"] == "space:PROJECT_A"
        assert len(list_sync_errors(state_conn, "run-1")) == 1
        assert list_page_targets_for_target(state_conn, build_space_target_key("PROJECT_A"))[0]["page_id"] == "123"
        assert list_sync_targets_for_space(state_conn, "PROJECT_A")[0]["target_key"] == "space:PROJECT_A"

    with connect_index_db(index_db) as index_conn:
        upsert_document(
            index_conn,
            DocumentRecord(
                doc_id="confluence:PROJECT_A:123",
                source="confluence",
                space_key="PROJECT_A",
                space_id="10",
                page_id="123",
                path="docs/confluence/PROJECT_A/pages/123__auth.md",
                title="認証API仕様",
            ),
        )
        replace_chunks_for_document(
            index_conn,
            "confluence:PROJECT_A:123",
            [
                ChunkRecord(
                    chunk_id="confluence:PROJECT_A:123:0",
                    doc_id="confluence:PROJECT_A:123",
                    page_id="123",
                    space_key="PROJECT_A",
                    path="docs/confluence/PROJECT_A/pages/123__auth.md",
                    title="認証API仕様",
                    headings="認証API仕様 > Token更新",
                    body="refresh token rotation",
                    start_line=10,
                    end_line=12,
                    chunk_index=0,
                )
            ],
        )

        assert get_document(index_conn, "confluence:PROJECT_A:123")["page_id"] == "123"
        assert len(list_chunks_for_document(index_conn, "confluence:PROJECT_A:123")) == 1


def test_page_tree_target_roundtrip(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    initialize_all_databases(state_db, tmp_path / "docs.db")

    with connect_state_db(state_db) as state_conn:
        upsert_sync_target(
            state_conn,
            build_target_record(
                space_key="PROJECT_A",
                space_id="10",
                target_type="page_tree",
                root_page_id="9001",
                name="認証配下",
            ),
        )
        create_sync_run(
            state_conn,
            SyncRunRecord(
                run_id="run-tree",
                space_key="PROJECT_A",
                mode="full",
                started_at="2026-05-05T00:00:00+00:00",
                status="running",
                target_type="page_tree",
                root_page_id="9001",
            ),
        )
        record_sync_error(
            state_conn,
            SyncErrorRecord(
                run_id="run-tree",
                space_key="PROJECT_A",
                operation="sync_page",
                error_message="boom",
                created_at="2026-05-05T00:00:01+00:00",
                page_id="9002",
                error_type="RuntimeError",
                target_type="page_tree",
                root_page_id="9001",
            ),
        )
        replace_page_targets_for_target(
            state_conn,
            build_page_tree_target_key("PROJECT_A", "9001"),
            [
                PageTargetRecord(
                    target_key=build_page_tree_target_key("PROJECT_A", "9001"),
                    page_id="9001",
                    space_key="PROJECT_A",
                    last_seen_at="2026-05-05T02:00:00+00:00",
                ),
                PageTargetRecord(
                    target_key=build_page_tree_target_key("PROJECT_A", "9001"),
                    page_id="9002",
                    space_key="PROJECT_A",
                    last_seen_at="2026-05-05T02:00:00+00:00",
                ),
            ],
        )

        target = get_sync_target(state_conn, build_page_tree_target_key("PROJECT_A", "9001"))
        run = get_sync_run(state_conn, "run-tree")
        errors = list_sync_errors(state_conn, "run-tree")
        memberships = list_page_targets_for_target(
            state_conn,
            build_page_tree_target_key("PROJECT_A", "9001"),
        )

        assert target["target_type"] == "page_tree"
        assert target["root_page_id"] == "9001"
        assert run["target_key"] == "page_tree:PROJECT_A:9001"
        assert run["target_type"] == "page_tree"
        assert errors[0]["target_key"] == "page_tree:PROJECT_A:9001"
        assert len(memberships) == 2


def test_initialize_state_db_migrates_legacy_sync_state(tmp_path: Path) -> None:
    state_db = tmp_path / "legacy.db"
    with connect_state_db(state_db) as state_conn:
        state_conn.executescript(
            """
            CREATE TABLE sync_state (
              space_key TEXT PRIMARY KEY,
              space_id TEXT NOT NULL,
              last_successful_sync_at TEXT,
              last_started_at TEXT,
              last_completed_at TEXT,
              last_error TEXT,
              updated_at TEXT NOT NULL
            );
            INSERT INTO sync_state (
              space_key, space_id, last_successful_sync_at, last_started_at,
              last_completed_at, last_error, updated_at
            ) VALUES (
              'PROJECT_A', '10', '2026-05-05T00:00:00+00:00', '2026-05-05T00:00:00+00:00',
              '2026-05-05T00:10:00+00:00', NULL, '2026-05-05T00:10:00+00:00'
            );
            """
        )
        state_conn.commit()
        initialize_state_db(state_conn)

        migrated = get_sync_state(state_conn, "PROJECT_A")
        columns = {
            row["name"] for row in state_conn.execute("PRAGMA table_info(sync_state)").fetchall()
        }

        assert migrated is not None
        assert migrated["target_key"] == "space:PROJECT_A"
        assert migrated["target_type"] == "space"
        assert get_sync_target(state_conn, "space:PROJECT_A") is not None
        assert "target_key" in columns
