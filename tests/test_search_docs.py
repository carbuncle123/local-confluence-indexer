from __future__ import annotations

import json

from db import (
    ChunkRecord,
    DocumentRecord,
    PageTargetRecord,
    connect_index_db,
    connect_state_db,
    initialize_index_db,
    initialize_state_db,
    replace_chunks_for_document,
    replace_page_targets_for_target,
    upsert_document,
)
from search_docs import (
    build_match_query,
    group_results_by_page,
    highlight_excerpt,
    query_results,
    render_json,
    render_markdown,
    resolve_allowed_page_ids,
)


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
                    title="認証API仕様",
                    headings="認証API仕様 > Refresh token詳細",
                    body="refresh token の有効期限と rotation の詳細です。",
                    start_line=24,
                    end_line=26,
                    chunk_index=1,
                    labels_json=json.dumps(["official"], ensure_ascii=False),
                ),
                ChunkRecord(
                    chunk_id="confluence:PROJECT_A:123:2",
                    doc_id="confluence:PROJECT_A:123",
                    page_id="123",
                    space_key="PROJECT_A",
                    path="docs/confluence/PROJECT_A/pages/123__auth.md",
                    title="旧認証APIメモ",
                    headings="旧認証APIメモ",
                    body="deprecated な仕様です。",
                    start_line=30,
                    end_line=31,
                    chunk_index=2,
                    labels_json=json.dumps(["draft"], ensure_ascii=False),
                ),
            ],
        )
    return db_path


def prepare_state(tmp_path):
    state_db = tmp_path / "state.db"
    with connect_state_db(state_db) as connection:
        initialize_state_db(connection)
        replace_page_targets_for_target(
            connection,
            "page_tree:PROJECT_A:100",
            [
                PageTargetRecord(
                    target_key="page_tree:PROJECT_A:100",
                    page_id="123",
                    space_key="PROJECT_A",
                    last_seen_at="2026-05-05T02:00:00+00:00",
                )
            ],
        )
    return state_db


def test_build_match_query_quotes_each_token() -> None:
    assert build_match_query("refresh token") == '"refresh" OR "token"'


def test_query_results_filters_draft_and_returns_best_match(tmp_path) -> None:
    db_path = prepare_index(tmp_path)
    with connect_index_db(db_path) as connection:
        results = query_results(
            connection,
            query="refresh token",
            space_key="PROJECT_A",
            allowed_page_ids=None,
            top_k=5,
            include_draft=False,
        )

    assert len(results) == 2
    assert all(result.title == "認証API仕様" for result in results)
    assert any("Token更新" in result.headings for result in results)


def test_render_json_contains_line_range(tmp_path) -> None:
    db_path = prepare_index(tmp_path)
    with connect_index_db(db_path) as connection:
        results = query_results(
            connection,
            query="refresh token",
            space_key="PROJECT_A",
            allowed_page_ids=None,
            top_k=5,
            include_draft=False,
        )

    payload = json.loads(render_json(results))
    assert payload[0]["line_range"] in {"20-22", "24-26"}


def test_query_results_can_filter_by_allowed_page_ids(tmp_path) -> None:
    db_path = prepare_index(tmp_path)
    with connect_index_db(db_path) as connection:
        results = query_results(
            connection,
            query="refresh token",
            space_key="PROJECT_A",
            allowed_page_ids={"999"},
            top_k=5,
            include_draft=False,
        )

    assert results == []


def test_group_results_by_page_merges_same_page_hits(tmp_path) -> None:
    db_path = prepare_index(tmp_path)
    with connect_index_db(db_path) as connection:
        results = query_results(
            connection,
            query="refresh token",
            space_key="PROJECT_A",
            allowed_page_ids=None,
            top_k=5,
            include_draft=False,
        )

    page_groups = group_results_by_page(results)

    assert len(results) == 2
    assert len(page_groups) == 1
    assert page_groups[0].page_id == "123"
    assert len(page_groups[0].results) == 2


def test_highlight_excerpt_marks_query_terms() -> None:
    excerpt = highlight_excerpt("refresh token rotation を利用します。", "refresh token")

    assert "[[refresh]]" in excerpt
    assert "[[token]]" in excerpt


def test_render_markdown_includes_target_filter_and_grouped_matches(tmp_path) -> None:
    db_path = prepare_index(tmp_path)
    with connect_index_db(db_path) as connection:
        results = query_results(
            connection,
            query="refresh token",
            space_key="PROJECT_A",
            allowed_page_ids={"123"},
            top_k=5,
            include_draft=False,
        )

    markdown = render_markdown(results, "refresh token", "PROJECT_A", "100", 5)

    assert "Target Filter: page_tree:PROJECT_A:100" in markdown
    assert "Matching Chunks: 2" in markdown
    assert "### Match 1:" in markdown
    assert "[[refresh]]" in markdown


def test_resolve_allowed_page_ids_for_page_tree(tmp_path) -> None:
    state_db = prepare_state(tmp_path)

    allowed = resolve_allowed_page_ids(
        state_db_path=state_db,
        space_key="PROJECT_A",
        root_page_id="100",
    )

    assert allowed == {"123"}
