from __future__ import annotations

import json
from pathlib import Path

import pytest

from db import (
    ChunkRecord,
    DocumentRecord,
    connect_index_db,
    count_vector_chunks,
    get_vector_chunk_by_vector_id,
    initialize_index_db,
    list_vector_chunks,
    replace_chunks_for_document,
    upsert_document,
)
from vector_index import (
    VECTOR_BACKEND_FAISS,
    VectorBackendConfig,
    VectorMeta,
    VectorMetaMismatchError,
    chunk_content_hash,
    load_vector_backend_config,
    make_embedding_text,
    read_vector_meta,
    verify_meta_compatibility,
    write_vector_meta,
)


def test_load_vector_backend_config_defaults() -> None:
    config = load_vector_backend_config({})
    assert config.backend == "none"
    assert config.embedding_model.startswith("sentence-transformers/")
    assert config.normalize is True
    assert config.embedding_batch_size == 32


def test_load_vector_backend_config_rejects_unknown_backend() -> None:
    with pytest.raises(Exception):
        load_vector_backend_config({"DOC_VECTOR_BACKEND": "bogus"})


def test_load_vector_backend_config_disables_normalize() -> None:
    config = load_vector_backend_config(
        {
            "DOC_VECTOR_BACKEND": "faiss",
            "DOC_EMBEDDING_NORMALIZE": "false",
            "DOC_EMBEDDING_BATCH_SIZE": "8",
        }
    )
    assert config.backend == VECTOR_BACKEND_FAISS
    assert config.normalize is False
    assert config.embedding_batch_size == 8


def test_make_embedding_text_includes_title_headings_labels_body() -> None:
    chunk = {
        "title": "認証API仕様",
        "headings": "認証API仕様 > Token更新",
        "body": "refresh token rotation",
        "labels_json": json.dumps(["official", "auth"], ensure_ascii=False),
    }
    text = make_embedding_text(chunk)
    assert "Title: 認証API仕様" in text
    assert "Headings: 認証API仕様 > Token更新" in text
    assert "Labels: official, auth" in text
    assert "refresh token rotation" in text


def test_chunk_content_hash_stable_across_calls() -> None:
    chunk = {
        "title": "T",
        "headings": "H",
        "body": "B",
        "labels_json": json.dumps(["x"], ensure_ascii=False),
    }
    assert chunk_content_hash(chunk) == chunk_content_hash(chunk)
    assert chunk_content_hash(chunk).startswith("sha256:")


def test_vector_meta_roundtrip(tmp_path: Path) -> None:
    meta = VectorMeta(
        backend="faiss",
        embedding_model="model-A",
        embedding_dim=4,
        normalized=True,
        metric="inner_product",
        index_type="IndexFlatIP",
        created_at="2026-05-05T00:00:00+00:00",
        chunk_count=3,
    )
    target = tmp_path / "vector_meta.json"
    write_vector_meta(target, meta)
    loaded = read_vector_meta(target)
    assert loaded == meta


def test_verify_meta_compatibility_detects_model_mismatch() -> None:
    meta = VectorMeta(
        backend="faiss",
        embedding_model="model-A",
        embedding_dim=4,
        normalized=True,
        metric="inner_product",
        index_type="IndexFlatIP",
        created_at="2026-05-05T00:00:00+00:00",
        chunk_count=1,
    )
    with pytest.raises(VectorMetaMismatchError):
        verify_meta_compatibility(meta, expected_model="model-B")


def test_verify_meta_compatibility_detects_dim_mismatch() -> None:
    meta = VectorMeta(
        backend="faiss",
        embedding_model="model-A",
        embedding_dim=4,
        normalized=True,
        metric="inner_product",
        index_type="IndexFlatIP",
        created_at="2026-05-05T00:00:00+00:00",
        chunk_count=1,
    )
    with pytest.raises(VectorMetaMismatchError):
        verify_meta_compatibility(meta, expected_model="model-A", expected_dim=8)


def _make_index_with_chunks(tmp_path: Path) -> Path:
    db_path = tmp_path / "docs.db"
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
                path="docs/confluence/PROJECT_A/pages/1__a.md",
                title="認証API仕様",
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
                    path="docs/confluence/PROJECT_A/pages/1__a.md",
                    title="認証API仕様",
                    headings="認証API仕様 > Token更新",
                    body="refresh token rotation で access token を更新",
                    chunk_index=0,
                    labels_json=json.dumps(["official"], ensure_ascii=False),
                )
            ],
        )
    return db_path


def test_vector_chunks_table_roundtrip(tmp_path: Path) -> None:
    db_path = _make_index_with_chunks(tmp_path)
    from db import VectorChunkRecord, insert_vector_chunks

    with connect_index_db(db_path) as connection:
        insert_vector_chunks(
            connection,
            [
                VectorChunkRecord(
                    vector_id=0,
                    chunk_id="confluence:PROJECT_A:1:0",
                    doc_id="confluence:PROJECT_A:1",
                    space_key="PROJECT_A",
                    page_id="1",
                    embedding_model="fake-model",
                    embedding_dim=4,
                    content_hash="sha256:abc",
                    created_at="2026-05-05T00:00:00+00:00",
                )
            ],
        )

        rows = list_vector_chunks(connection)
        assert count_vector_chunks(connection) == 1
        assert rows[0]["chunk_id"] == "confluence:PROJECT_A:1:0"
        looked_up = get_vector_chunk_by_vector_id(connection, 0)
        assert looked_up is not None
        assert looked_up["embedding_model"] == "fake-model"
