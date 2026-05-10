from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from _fake_vector import FakeFaissModule, fake_embedder_factory  # noqa: E402

from db import (  # noqa: E402
    ChunkRecord,
    DocumentRecord,
    connect_index_db,
    count_vector_chunks,
    initialize_index_db,
    list_vector_chunks,
    replace_chunks_for_document,
    upsert_document,
)
from build_doc_index import (  # noqa: E402
    rebuild_vector_index_for_space,
)
from search_docs import (  # noqa: E402
    MODE_HYBRID,
    MODE_VECTOR,
    hybrid_search,
    render_json,
    render_markdown,
    vector_search,
)
from vector_index import (  # noqa: E402
    VECTOR_BACKEND_FAISS,
    VectorBackendConfig,
    read_vector_meta,
)
import vector_index as vector_index_module  # noqa: E402


@pytest.fixture(autouse=True)
def patch_faiss(monkeypatch):
    """Replace faiss with the fake module in tests."""

    monkeypatch.setattr(vector_index_module, "_import_faiss", lambda: FakeFaissModule())
    monkeypatch.setattr(vector_index_module, "_import_numpy", lambda: np)
    yield


def _seed_chunks(db_path: Path) -> None:
    with connect_index_db(db_path) as connection:
        initialize_index_db(connection)
        upsert_document(
            connection,
            DocumentRecord(
                doc_id="confluence:PROJECT_A:1",
                source="confluence",
                space_key="PROJECT_A",
                space_id="10",
                page_id="1",
                path="docs/confluence/PROJECT_A/pages/1__official.md",
                title="認証API仕様",
                url="https://example.atlassian.net/wiki/spaces/PROJECT_A/pages/1",
                version_number=3,
                version_created_at="2026-05-05T01:00:00.000Z",
                fetched_at="2026-05-05T02:00:00+00:00",
                labels_json=json.dumps(["official"], ensure_ascii=False),
            ),
        )
        replace_chunks_for_document(
            connection,
            "confluence:PROJECT_A:1",
            [
                ChunkRecord(
                    chunk_id="confluence:PROJECT_A:1:0",
                    doc_id="confluence:PROJECT_A:1",
                    page_id="1",
                    space_key="PROJECT_A",
                    path="docs/confluence/PROJECT_A/pages/1__official.md",
                    title="認証API仕様",
                    headings="認証API仕様 > Token更新",
                    body="refresh token rotation で access token を更新します。",
                    chunk_index=0,
                    labels_json=json.dumps(["official"], ensure_ascii=False),
                )
            ],
        )

        upsert_document(
            connection,
            DocumentRecord(
                doc_id="confluence:PROJECT_A:2",
                source="confluence",
                space_key="PROJECT_A",
                space_id="10",
                page_id="2",
                path="docs/confluence/PROJECT_A/pages/2__draft.md",
                title="セッション延長検討メモ",
                url="https://example.atlassian.net/wiki/spaces/PROJECT_A/pages/2",
                version_number=1,
                version_created_at="2026-05-04T01:00:00.000Z",
                fetched_at="2026-05-05T02:00:00+00:00",
                labels_json=json.dumps(["draft"], ensure_ascii=False),
            ),
        )
        replace_chunks_for_document(
            connection,
            "confluence:PROJECT_A:2",
            [
                ChunkRecord(
                    chunk_id="confluence:PROJECT_A:2:0",
                    doc_id="confluence:PROJECT_A:2",
                    page_id="2",
                    space_key="PROJECT_A",
                    path="docs/confluence/PROJECT_A/pages/2__draft.md",
                    title="セッション延長検討メモ",
                    headings="セッション延長検討メモ",
                    body="ログイン状態を延長する仕組みについての下書きメモ。",
                    chunk_index=0,
                    labels_json=json.dumps(["draft"], ensure_ascii=False),
                )
            ],
        )


def _vector_config(tmp_path: Path) -> VectorBackendConfig:
    return VectorBackendConfig(
        backend=VECTOR_BACKEND_FAISS,
        embedding_model="fake-model",
        embedding_batch_size=2,
        normalize=True,
        faiss_index_path=tmp_path / "faiss.index",
        vector_meta_path=tmp_path / "vector_meta.json",
    )


def test_rebuild_vector_index_for_space_writes_meta_and_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "docs.db"
    _seed_chunks(db_path)
    config = _vector_config(tmp_path)

    chunk_count = rebuild_vector_index_for_space(
        "PROJECT_A",
        config=config,
        index_db_path=db_path,
        embedder_factory=fake_embedder_factory,
    )

    meta = read_vector_meta(config.vector_meta_path)
    assert chunk_count == 2
    assert meta.chunk_count == 2
    assert meta.embedding_dim > 0
    assert meta.embedding_model == "fake-model"

    with connect_index_db(db_path) as connection:
        rows = list_vector_chunks(connection)
    assert [row["vector_id"] for row in rows] == [0, 1]
    assert {row["chunk_id"] for row in rows} == {
        "confluence:PROJECT_A:1:0",
        "confluence:PROJECT_A:2:0",
    }


def test_vector_search_returns_semantic_match(tmp_path: Path) -> None:
    db_path = tmp_path / "docs.db"
    _seed_chunks(db_path)
    config = _vector_config(tmp_path)
    rebuild_vector_index_for_space(
        "PROJECT_A",
        config=config,
        index_db_path=db_path,
        embedder_factory=fake_embedder_factory,
    )

    with connect_index_db(db_path) as connection:
        results = vector_search(
            connection,
            query="ログイン 状態 延長",
            space_key="PROJECT_A",
            allowed_page_ids=None,
            top_k=5,
            include_draft=True,
            config=config,
            embedder_factory=fake_embedder_factory,
        )

    assert results, "vector_search should return at least one result"
    assert results[0].chunk_id == "confluence:PROJECT_A:2:0"
    assert results[0].vector_rank == 1
    assert results[0].vector_score is not None
    assert "semantic match" in results[0].match_reason


def test_vector_search_filters_drafts_by_default(tmp_path: Path) -> None:
    db_path = tmp_path / "docs.db"
    _seed_chunks(db_path)
    config = _vector_config(tmp_path)
    rebuild_vector_index_for_space(
        "PROJECT_A",
        config=config,
        index_db_path=db_path,
        embedder_factory=fake_embedder_factory,
    )

    with connect_index_db(db_path) as connection:
        results = vector_search(
            connection,
            query="ログイン 状態 延長",
            space_key="PROJECT_A",
            allowed_page_ids=None,
            top_k=5,
            include_draft=False,
            config=config,
            embedder_factory=fake_embedder_factory,
        )

    assert all(result.chunk_id != "confluence:PROJECT_A:2:0" for result in results)


def test_hybrid_search_merges_fts_and_vector(tmp_path: Path) -> None:
    db_path = tmp_path / "docs.db"
    _seed_chunks(db_path)
    config = _vector_config(tmp_path)
    rebuild_vector_index_for_space(
        "PROJECT_A",
        config=config,
        index_db_path=db_path,
        embedder_factory=fake_embedder_factory,
    )

    with connect_index_db(db_path) as connection:
        results = hybrid_search(
            connection,
            query="refresh token",
            space_key="PROJECT_A",
            allowed_page_ids=None,
            top_k=5,
            fts_k=10,
            vector_k=10,
            include_draft=True,
            config=config,
            embedder_factory=fake_embedder_factory,
        )

    assert results
    top = results[0]
    assert top.chunk_id == "confluence:PROJECT_A:1:0"
    assert top.fts_rank == 1
    assert top.vector_rank is not None
    assert "keyword match" in top.match_reason
    assert "semantic match" in top.match_reason
    assert "label: official" in top.match_reason
    assert top.final_score is not None


def test_hybrid_search_falls_back_when_vector_index_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "docs.db"
    _seed_chunks(db_path)
    config = _vector_config(tmp_path)
    # Do not build the FAISS artifacts.

    warnings: list[str] = []
    with connect_index_db(db_path) as connection:
        results = hybrid_search(
            connection,
            query="refresh token",
            space_key="PROJECT_A",
            allowed_page_ids=None,
            top_k=5,
            fts_k=10,
            vector_k=10,
            include_draft=True,
            config=config,
            embedder_factory=fake_embedder_factory,
            on_warning=warnings.append,
        )

    assert results, "FTS-only fallback should still return matches"
    assert any("vector search" in message for message in warnings)
    assert all(result.vector_rank is None for result in results)


def test_render_markdown_includes_score_breakdown_when_explain(tmp_path: Path) -> None:
    db_path = tmp_path / "docs.db"
    _seed_chunks(db_path)
    config = _vector_config(tmp_path)
    rebuild_vector_index_for_space(
        "PROJECT_A",
        config=config,
        index_db_path=db_path,
        embedder_factory=fake_embedder_factory,
    )

    with connect_index_db(db_path) as connection:
        results = hybrid_search(
            connection,
            query="refresh token",
            space_key="PROJECT_A",
            allowed_page_ids=None,
            top_k=5,
            fts_k=10,
            vector_k=10,
            include_draft=True,
            config=config,
            embedder_factory=fake_embedder_factory,
        )

    rendered = render_markdown(
        results,
        "refresh token",
        "PROJECT_A",
        None,
        5,
        mode=MODE_HYBRID,
        explain=True,
    )

    assert "Mode: hybrid" in rendered
    assert "Score Breakdown" in rendered
    assert "fts_rank" in rendered
    assert "vector_rank" in rendered

    payload = json.loads(render_json(results))
    assert payload[0]["fts_rank"] == 1
    assert payload[0]["match_reason"]


def test_count_vector_chunks_matches_meta(tmp_path: Path) -> None:
    db_path = tmp_path / "docs.db"
    _seed_chunks(db_path)
    config = _vector_config(tmp_path)
    rebuild_vector_index_for_space(
        "PROJECT_A",
        config=config,
        index_db_path=db_path,
        embedder_factory=fake_embedder_factory,
    )

    with connect_index_db(db_path) as connection:
        sqlite_count = count_vector_chunks(connection)
    meta = read_vector_meta(config.vector_meta_path)
    assert sqlite_count == meta.chunk_count


def test_vector_mode_render_shows_vector_score(tmp_path: Path) -> None:
    db_path = tmp_path / "docs.db"
    _seed_chunks(db_path)
    config = _vector_config(tmp_path)
    rebuild_vector_index_for_space(
        "PROJECT_A",
        config=config,
        index_db_path=db_path,
        embedder_factory=fake_embedder_factory,
    )

    with connect_index_db(db_path) as connection:
        results = vector_search(
            connection,
            query="refresh token",
            space_key="PROJECT_A",
            allowed_page_ids=None,
            top_k=3,
            include_draft=True,
            config=config,
            embedder_factory=fake_embedder_factory,
        )

    rendered = render_markdown(
        results,
        "refresh token",
        "PROJECT_A",
        None,
        3,
        mode=MODE_VECTOR,
    )

    assert "Mode: vector" in rendered
    assert "Vector Score" in rendered
