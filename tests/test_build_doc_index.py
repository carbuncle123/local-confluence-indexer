from __future__ import annotations

import json
import textwrap
from pathlib import Path

from build_doc_index import build_chunks, index_manifest, parse_frontmatter
from db import connect_index_db


def test_parse_frontmatter_extracts_body_and_offset() -> None:
    parsed = parse_frontmatter(
        textwrap.dedent(
            """\
            ---
            title: Test
            ---
            # Heading

            body
            """
        )
    )

    assert parsed.frontmatter["title"] == "Test"
    assert parsed.body.lstrip().startswith("# Heading")
    assert parsed.body_start_line >= 3


def test_build_chunks_splits_by_heading() -> None:
    parsed = parse_frontmatter(
        textwrap.dedent(
            """\
            ---
            title: Test
            ---
            # Root

            ## Child A

            text a

            ## Child B

            text b
            """
        )
    )

    chunks = build_chunks(
        parsed,
        doc_id="confluence:PROJECT_A:123",
        space_key="PROJECT_A",
        page_id="123",
        path="docs/confluence/PROJECT_A/pages/123__test.md",
        title="Test",
        labels_json='["official"]',
    )

    assert len(chunks) >= 2
    assert any("Child A" in (chunk.headings or "") for chunk in chunks)
    assert any("Child B" in (chunk.headings or "") for chunk in chunks)


def test_index_manifest_builds_documents_and_chunks(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs/confluence/PROJECT_A/pages"
    docs_dir.mkdir(parents=True)
    markdown_path = docs_dir / "123__auth-api.md"
    markdown_path.write_text(
        textwrap.dedent(
            """\
            ---
            source: confluence
            space_key: PROJECT_A
            space_id: "10"
            page_id: "123"
            title: 認証API仕様
            status: current
            url: https://example.atlassian.net/rest/api/content/123
            version_number: 2
            version_created_at: "2026-05-05T01:00:00.000Z"
            fetched_at: "2026-05-05T02:00:00+00:00"
            content_hash: sha256:test
            labels:
              - official
            ---
            # 認証API仕様

            ## Token更新

            refresh token rotation は access token を更新します。
            """
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "docs/confluence/PROJECT_A/manifest.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "page_id": "123",
                "path": str(markdown_path),
                "title": "認証API仕様",
                "space_key": "PROJECT_A",
                "space_id": "10",
                "version_number": 2,
                "version_created_at": "2026-05-05T01:00:00.000Z",
                "fetched_at": "2026-05-05T02:00:00+00:00",
                "labels": ["official"],
                "status": "current",
                "url": "https://example.atlassian.net/rest/api/content/123",
                "content_hash": "sha256:test",
                "include_in_index": True,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    index_db = tmp_path / ".local-doc-index/docs.db"
    count = index_manifest(manifest, index_db)

    assert count == 1
    with connect_index_db(index_db) as connection:
        document_count = connection.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"]
        chunk_count = connection.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
    assert document_count == 1
    assert chunk_count >= 1
