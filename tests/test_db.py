from __future__ import annotations

from pathlib import Path

from db import (
    ChunkRecord,
    DocumentRecord,
    PageRecord,
    SpaceRecord,
    SyncErrorRecord,
    SyncRunRecord,
    connect_index_db,
    connect_state_db,
    create_sync_run,
    get_document,
    get_page,
    get_space,
    get_sync_run,
    initialize_all_databases,
    list_chunks_for_document,
    list_sync_errors,
    replace_chunks_for_document,
    upsert_document,
    upsert_page,
    upsert_space,
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

        assert get_space(state_conn, "PROJECT_A")["space_id"] == "10"
        assert get_page(state_conn, "123")["title"] == "認証API仕様"
        assert get_sync_run(state_conn, "run-1")["status"] == "running"
        assert len(list_sync_errors(state_conn, "run-1")) == 1

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
