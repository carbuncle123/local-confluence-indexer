from __future__ import annotations

import json

from db import ChunkRecord, DocumentRecord, connect_index_db, initialize_index_db, replace_chunks_for_document, upsert_document
from search_docs import build_match_query, query_results, render_json


def prepare_index(tmp_path):
    db_path = tmp_path / "docs.db"
    with connect_index_db(db_path) as connection:
        initialize_index_db(connection)
        upsert_document(
            connection,
            DocumentRecord(
                doc_id="confluence:PROJECT_A:123",
                source="confluence",
                space_key="PROJECT_A",
                space_id="10",
                page_id="123",
                path="docs/confluence/PROJECT_A/pages/123__auth.md",
                title="認証API仕様",
                url="https://example.atlassian.net/rest/api/content/123",
                version_number=2,
                version_created_at="2026-05-05T01:00:00.000Z",
                fetched_at="2026-05-05T02:00:00+00:00",
                labels_json=json.dumps(["official"], ensure_ascii=False),
            ),
        )
        replace_chunks_for_document(
            connection,
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
                    body="refresh token rotation は access token を更新します。",
                    start_line=20,
                    end_line=22,
                    chunk_index=0,
                    labels_json=json.dumps(["official"], ensure_ascii=False),
                ),
                ChunkRecord(
                    chunk_id="confluence:PROJECT_A:123:1",
                    doc_id="confluence:PROJECT_A:123",
                    page_id="123",
                    space_key="PROJECT_A",
                    path="docs/confluence/PROJECT_A/pages/123__auth.md",
                    title="旧認証APIメモ",
                    headings="旧認証APIメモ",
                    body="deprecated な仕様です。",
                    start_line=30,
                    end_line=31,
                    chunk_index=1,
                    labels_json=json.dumps(["draft"], ensure_ascii=False),
                ),
            ],
        )
    return db_path


def test_build_match_query_quotes_each_token() -> None:
    assert build_match_query("refresh token") == '"refresh" OR "token"'


def test_query_results_filters_draft_and_returns_best_match(tmp_path) -> None:
    db_path = prepare_index(tmp_path)
    with connect_index_db(db_path) as connection:
        results = query_results(
            connection,
            query="refresh token",
            space_key="PROJECT_A",
            top_k=5,
            include_draft=False,
        )

    assert len(results) == 1
    assert results[0].title == "認証API仕様"
    assert "Token更新" in results[0].headings


def test_render_json_contains_line_range(tmp_path) -> None:
    db_path = prepare_index(tmp_path)
    with connect_index_db(db_path) as connection:
        results = query_results(
            connection,
            query="refresh token",
            space_key="PROJECT_A",
            top_k=5,
            include_draft=False,
        )

    payload = json.loads(render_json(results))
    assert payload[0]["line_range"] == "20-22"
