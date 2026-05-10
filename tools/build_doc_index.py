"""Build a local SQLite FTS5 index from synced Markdown files."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from db import (
    ChunkRecord,
    DocumentRecord,
    VectorChunkRecord,
    clear_all_vector_chunks,
    clear_documents_for_space,
    clear_vector_chunks_for_space,
    connect_index_db,
    initialize_index_db,
    insert_vector_chunks,
    list_all_chunks,
    list_chunks_for_space,
    replace_chunks_for_document,
    upsert_document,
)
from utils import DEFAULT_INDEX_DB_PATH, now_iso
from vector_index import (
    SUPPORTED_VECTOR_BACKENDS,
    VECTOR_BACKEND_FAISS,
    VECTOR_BACKEND_NONE,
    VectorBackendConfig,
    build_faiss_artifacts,
    load_vector_backend_config,
)


MAX_CHUNK_CHARS = 3000


@dataclass(slots=True)
class ParsedMarkdown:
    """Markdown document with extracted frontmatter and body."""

    frontmatter: dict[str, Any]
    body: str
    body_start_line: int


@dataclass(slots=True)
class Section:
    """A heading-aware section before final chunk splitting."""

    headings: list[str]
    body: str
    start_line: int | None
    end_line: int | None


def build_parser() -> argparse.ArgumentParser:
    """Build the index CLI parser."""

    parser = argparse.ArgumentParser(
        description="同期済み Markdown からローカル検索インデックスを構築します。"
    )
    parser.add_argument("--space", help="対象の Confluence space key")
    parser.add_argument("--all", action="store_true", help="全 space を対象に再構築する")
    parser.add_argument(
        "--vector-backend",
        choices=sorted(SUPPORTED_VECTOR_BACKENDS),
        default=None,
        help="ベクトルインデックスのバックエンド (faiss または none)。未指定なら環境変数を使用。",
    )
    return parser


def parse_frontmatter(markdown_text: str) -> ParsedMarkdown:
    """Extract YAML frontmatter and the Markdown body."""

    if not markdown_text.startswith("---\n"):
        return ParsedMarkdown(frontmatter={}, body=markdown_text, body_start_line=1)

    parts = markdown_text.split("\n---\n", 1)
    if len(parts) != 2:
        return ParsedMarkdown(frontmatter={}, body=markdown_text, body_start_line=1)

    frontmatter_text = parts[0][4:]
    body = parts[1]
    frontmatter = yaml.safe_load(frontmatter_text) or {}
    body_start_line = len(parts[0].splitlines()) + 2
    return ParsedMarkdown(frontmatter=frontmatter, body=body, body_start_line=body_start_line)


def split_paragraphs(lines: list[str]) -> list[list[str]]:
    """Split a section into paragraph-like blocks while preserving blank lines."""

    blocks: list[list[str]] = []
    current: list[str] = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block

        if not in_code_block and stripped == "":
            if current:
                current.append(line)
                blocks.append(current)
                current = []
            else:
                blocks.append([line])
            continue

        current.append(line)

    if current:
        blocks.append(current)

    return blocks


def parse_sections(body: str, *, line_offset: int = 0) -> list[Section]:
    """Create heading-aware sections from Markdown text."""

    lines = body.splitlines()
    sections: list[Section] = []
    current_lines: list[str] = []
    current_headings: list[str] = []
    current_start_line: int | None = 1
    heading_stack: list[str] = []
    in_code_block = False

    for line_number, line in enumerate(lines, start=1 + line_offset):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block

        if not in_code_block and stripped.startswith("#"):
            marker_count = len(stripped) - len(stripped.lstrip("#"))
            if 1 <= marker_count <= 6 and stripped[marker_count : marker_count + 1] == " ":
                if current_lines:
                    sections.append(
                        Section(
                            headings=current_headings.copy(),
                            body="\n".join(current_lines).strip(),
                            start_line=current_start_line,
                            end_line=line_number - 1,
                        )
                    )
                    current_lines = []

                heading_title = stripped[marker_count + 1 :].strip()
                heading_stack = heading_stack[: marker_count - 1]
                heading_stack.append(heading_title)
                current_headings = heading_stack.copy()
                current_start_line = line_number

        current_lines.append(line)

    if current_lines:
        sections.append(
            Section(
                headings=current_headings.copy(),
                body="\n".join(current_lines).strip(),
                start_line=current_start_line,
                end_line=len(lines) + line_offset,
            )
        )

    return [section for section in sections if section.body.strip()]


def split_long_section(section: Section, max_chunk_chars: int = MAX_CHUNK_CHARS) -> list[Section]:
    """Split a long section into smaller chunks at paragraph boundaries."""

    if len(section.body) <= max_chunk_chars:
        return [section]

    blocks = split_paragraphs(section.body.splitlines())
    result: list[Section] = []
    buffer: list[str] = []
    current_start = section.start_line
    current_line = section.start_line or 1

    for block in blocks:
        block_text = "\n".join(block).strip("\n")
        tentative = "\n".join(buffer + [block_text]).strip()
        block_line_count = len(block)

        if buffer and len(tentative) > max_chunk_chars:
            result.append(
                Section(
                    headings=section.headings.copy(),
                    body="\n".join(buffer).strip(),
                    start_line=current_start,
                    end_line=current_line - 1,
                )
            )
            buffer = [block_text]
            current_start = current_line
        else:
            buffer.append(block_text)

        current_line += block_line_count

    if buffer:
        result.append(
            Section(
                headings=section.headings.copy(),
                body="\n".join(buffer).strip(),
                start_line=current_start,
                end_line=section.end_line,
            )
        )

    return result


def build_chunks(
    parsed: ParsedMarkdown,
    *,
    doc_id: str,
    space_key: str,
    page_id: str,
    path: str,
    title: str,
    labels_json: str | None,
) -> list[ChunkRecord]:
    """Convert parsed Markdown into chunk records."""

    sections = parse_sections(parsed.body, line_offset=parsed.body_start_line - 1)
    chunks: list[ChunkRecord] = []
    chunk_index = 0

    for section in sections:
        for split_section in split_long_section(section):
            chunks.append(
                ChunkRecord(
                    chunk_id=f"{doc_id}:{chunk_index}",
                    doc_id=doc_id,
                    page_id=page_id,
                    space_key=space_key,
                    path=path,
                    title=title,
                    headings=" > ".join(split_section.headings) if split_section.headings else title,
                    body=split_section.body,
                    start_line=split_section.start_line,
                    end_line=split_section.end_line,
                    chunk_index=chunk_index,
                    token_count=None,
                    labels_json=labels_json,
                    metadata_json=None,
                )
            )
            chunk_index += 1

    return chunks


def manifest_paths_for_all_spaces(docs_root: Path) -> list[Path]:
    """Collect all manifest.jsonl paths under the docs root."""

    return sorted(docs_root.glob("*/manifest.jsonl"))


def load_manifest_entries(manifest_path: Path) -> list[dict[str, Any]]:
    """Load JSONL manifest entries."""

    entries: list[dict[str, Any]] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(json.loads(line))
    return entries


def build_document_record(entry: dict[str, Any], frontmatter: dict[str, Any]) -> DocumentRecord:
    """Create a document record from manifest and frontmatter."""

    return DocumentRecord(
        doc_id=f"confluence:{entry['space_key']}:{entry['page_id']}",
        source=frontmatter.get("source", "confluence"),
        space_key=entry["space_key"],
        space_id=entry.get("space_id"),
        page_id=entry["page_id"],
        path=entry["path"],
        title=entry["title"],
        url=entry.get("url"),
        status=entry.get("status"),
        parent_id=frontmatter.get("parent_id"),
        version_number=entry.get("version_number"),
        version_created_at=entry.get("version_created_at"),
        fetched_at=entry.get("fetched_at"),
        labels_json=json.dumps(entry.get("labels", []), ensure_ascii=False),
        content_hash=entry.get("content_hash"),
        metadata_json=json.dumps(frontmatter, ensure_ascii=False, sort_keys=True, default=str),
    )


def index_manifest(manifest_path: Path, index_db_path: Path = DEFAULT_INDEX_DB_PATH) -> int:
    """Rebuild the index for a single manifest."""

    entries = load_manifest_entries(manifest_path)
    if not entries:
        return 0

    space_key = entries[0]["space_key"]
    with connect_index_db(index_db_path) as connection:
        initialize_index_db(connection)
        clear_documents_for_space(connection, space_key)

        for entry in entries:
            markdown_path = Path(entry["path"])
            parsed = parse_frontmatter(markdown_path.read_text(encoding="utf-8"))
            document = build_document_record(entry, parsed.frontmatter)
            upsert_document(connection, document)
            chunks = build_chunks(
                parsed,
                doc_id=document.doc_id,
                space_key=document.space_key,
                page_id=document.page_id,
                path=document.path,
                title=document.title,
                labels_json=document.labels_json,
            )
            replace_chunks_for_document(connection, document.doc_id, chunks)

    return len(entries)


def resolve_vector_backend_config(cli_value: str | None) -> VectorBackendConfig:
    """Build a VectorBackendConfig that respects CLI overrides."""

    config = load_vector_backend_config()
    if cli_value is not None:
        config.backend = cli_value
    return config


def rebuild_vector_index_for_space(
    space_key: str,
    *,
    config: VectorBackendConfig,
    index_db_path: Path = DEFAULT_INDEX_DB_PATH,
    embedder_factory=None,
) -> int:
    """Rebuild the FAISS vector index for a single space."""

    if config.backend != VECTOR_BACKEND_FAISS:
        return 0

    with connect_index_db(index_db_path) as connection:
        initialize_index_db(connection)
        chunks = list_chunks_for_space(connection, space_key)
        clear_vector_chunks_for_space(connection, space_key)

        if not chunks:
            return 0

        if _has_other_space_vectors(connection, space_key):
            raise SystemExit(
                "FAISS index は 1 space 単位の再構築のみ対応しています。"
                " 全 space を再構築するには `--all` を指定してください。"
            )

        result, vector_rows = build_faiss_artifacts(
            chunks,
            config=config,
            embedder_factory=embedder_factory,
        )
        _persist_vector_rows(connection, vector_rows, meta_model=config.embedding_model, embedding_dim=result.embedding_dim)
        return result.chunk_count


def rebuild_vector_index_for_all(
    *,
    config: VectorBackendConfig,
    index_db_path: Path = DEFAULT_INDEX_DB_PATH,
    embedder_factory=None,
) -> int:
    """Rebuild the FAISS vector index across every space."""

    if config.backend != VECTOR_BACKEND_FAISS:
        return 0

    with connect_index_db(index_db_path) as connection:
        initialize_index_db(connection)
        chunks = list_all_chunks(connection)
        clear_all_vector_chunks(connection)

        if not chunks:
            return 0

        result, vector_rows = build_faiss_artifacts(
            chunks,
            config=config,
            embedder_factory=embedder_factory,
        )
        _persist_vector_rows(connection, vector_rows, meta_model=config.embedding_model, embedding_dim=result.embedding_dim)
        return result.chunk_count


def _has_other_space_vectors(connection, space_key: str) -> bool:
    row = connection.execute(
        "SELECT COUNT(*) AS c FROM vector_chunks WHERE space_key != ?",
        (space_key,),
    ).fetchone()
    return bool(row and row["c"] > 0)


def _persist_vector_rows(
    connection,
    vector_rows: list[tuple[int, str, dict[str, Any]]],
    *,
    meta_model: str,
    embedding_dim: int,
) -> None:
    if not vector_rows:
        return
    created_at = now_iso()
    records = [
        VectorChunkRecord(
            vector_id=vector_id,
            chunk_id=chunk["chunk_id"],
            doc_id=chunk["doc_id"],
            space_key=chunk["space_key"],
            page_id=chunk["page_id"],
            embedding_model=meta_model,
            embedding_dim=embedding_dim,
            content_hash=content_hash,
            created_at=created_at,
            metadata_json=None,
        )
        for vector_id, content_hash, chunk in vector_rows
    ]
    insert_vector_chunks(connection, records)


def main() -> int:
    """Run the index build CLI."""

    parser = build_parser()
    args = parser.parse_args()

    docs_root = Path("docs/confluence")
    if args.all:
        manifest_paths = manifest_paths_for_all_spaces(docs_root)
    else:
        if not args.space:
            parser.error("--space か --all のどちらかが必要です。")
        manifest_paths = [docs_root / args.space / "manifest.jsonl"]

    indexed_count = 0
    for manifest_path in manifest_paths:
        if not manifest_path.exists():
            continue
        indexed_count += index_manifest(manifest_path)

    print(f"Indexed {indexed_count} documents.")

    vector_config = resolve_vector_backend_config(args.vector_backend)
    if vector_config.backend == VECTOR_BACKEND_FAISS:
        if args.all:
            vector_count = rebuild_vector_index_for_all(config=vector_config)
        else:
            vector_count = rebuild_vector_index_for_space(args.space, config=vector_config)
        print(
            f"Built FAISS vector index for {vector_count} chunks "
            f"(model={vector_config.embedding_model})."
        )
    elif vector_config.backend == VECTOR_BACKEND_NONE:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
