"""Search the local SQLite FTS5 / FAISS / hybrid index."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from db import (
    build_page_tree_target_key,
    connect_index_db,
    connect_state_db,
    count_vector_chunks,
    get_vector_chunk_by_vector_id,
    list_page_targets_for_target,
)
from utils import DEFAULT_INDEX_DB_PATH, DEFAULT_SYNC_DB_PATH
from vector_index import (
    SUPPORTED_VECTOR_BACKENDS,
    VECTOR_BACKEND_FAISS,
    VectorBackendConfig,
    VectorBackendUnavailableError,
    VectorIndexError,
    VectorMetaMismatchError,
    embed_query,
    load_vector_backend_config,
    read_faiss_index,
    read_vector_meta,
    search_faiss,
    verify_meta_compatibility,
)


DEFAULT_TOP_K = 10
SEARCH_EXPANSION_FACTOR = 5
DEFAULT_FTS_K = 30
DEFAULT_VECTOR_K = 30
RRF_K = 60
NEGATIVE_TITLE_KEYWORDS = ("旧", "廃止", "コピー", "メモ", "検討中", "draft", "old", "deprecated", "wip")
NEGATIVE_LABELS = {"draft", "wip", "deprecated", "old"}
POSITIVE_LABELS = {"official", "current", "approved"}

MODE_FTS = "fts"
MODE_VECTOR = "vector"
MODE_HYBRID = "hybrid"
SEARCH_MODES = (MODE_FTS, MODE_VECTOR, MODE_HYBRID)

POSITIVE_BOOSTS = {
    "official": 0.030,
    "current": 0.020,
    "approved": 0.020,
}
NEGATIVE_BOOSTS = {
    "draft": -0.030,
    "wip": -0.020,
    "deprecated": -0.050,
    "old": -0.040,
}
TITLE_QUERY_BOOST = 0.015
TITLE_RISKY_PENALTY = -0.030


@dataclass(slots=True)
class SearchResult:
    """Normalized search result returned by the CLI."""

    chunk_id: str
    page_id: str
    path: str
    title: str
    headings: list[str]
    start_line: int | None
    end_line: int | None
    body: str
    url: str | None
    version_number: int | None
    version_created_at: str | None
    fetched_at: str | None
    labels: list[str]
    rank: float
    score: float
    fts_rank: int | None = None
    vector_rank: int | None = None
    fts_score: float | None = None
    vector_score: float | None = None
    metadata_boost: float = 0.0
    final_score: float | None = None
    match_reason: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchPageGroup:
    """Grouped search results for a single document page."""

    page_id: str
    path: str
    title: str
    url: str | None
    version_number: int | None
    version_created_at: str | None
    fetched_at: str | None
    labels: list[str]
    score: float
    results: list[SearchResult]


def build_parser() -> argparse.ArgumentParser:
    """Build the search CLI parser."""

    parser = argparse.ArgumentParser(
        description="ローカル検索インデックスを検索します。"
    )
    parser.add_argument("query", help="検索クエリ")
    parser.add_argument("--space", help="対象の Confluence space key")
    parser.add_argument("--root-page-id", help="page_tree target に絞る root page id")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="返却件数")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--include-draft", action="store_true")
    parser.add_argument("--path-only", action="store_true")
    parser.add_argument("--open", action="store_true", dest="open_result")
    parser.add_argument(
        "--mode",
        choices=SEARCH_MODES,
        default=MODE_FTS,
        help="検索モード (fts | vector | hybrid)。既定は fts。",
    )
    parser.add_argument(
        "--vector-backend",
        choices=sorted(SUPPORTED_VECTOR_BACKENDS),
        default=None,
        help="ベクトル検索バックエンド。未指定なら環境変数を使う。",
    )
    parser.add_argument("--fts-k", type=int, default=DEFAULT_FTS_K, help="hybrid で FTS 側から取る件数")
    parser.add_argument("--vector-k", type=int, default=DEFAULT_VECTOR_K, help="hybrid で vector 側から取る件数")
    parser.add_argument("--explain", action="store_true", help="スコア内訳を表示する")
    return parser


def resolve_allowed_page_ids(
    *,
    state_db_path: Path,
    space_key: str | None,
    root_page_id: str | None,
) -> set[str] | None:
    """Resolve allowed page ids for a page-tree target filter."""

    if not root_page_id:
        return None
    if not space_key:
        raise ValueError("--root-page-id を使う場合は --space が必要です。")

    target_key = build_page_tree_target_key(space_key, root_page_id)
    with connect_state_db(state_db_path) as connection:
        rows = list_page_targets_for_target(connection, target_key, included_only=True)
    return {row["page_id"] for row in rows}


def build_match_query(query: str) -> str:
    """Build a tolerant FTS5 MATCH query from user input."""

    tokens = [token.strip() for token in query.split() if token.strip()]
    if not tokens:
        return f'"{query}"'
    return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)


def query_terms(query: str) -> list[str]:
    """Return normalized query terms for highlighting."""

    return [token.strip() for token in query.split() if token.strip()]


def compute_metadata_boost(title: str, labels: list[str], query: str) -> float:
    """Apply a small metadata-based boost/penalty (FTS-only mode)."""

    boost = 0.0
    label_set = {label.lower() for label in labels}
    title_lower = title.lower()
    query_lower = query.lower()

    if label_set & POSITIVE_LABELS:
        boost += 0.15
    if "official" in label_set:
        boost += 0.10
    if query_lower and query_lower in title_lower:
        boost += 0.08
    if label_set & NEGATIVE_LABELS:
        boost -= 0.20
    if any(keyword in title_lower for keyword in NEGATIVE_TITLE_KEYWORDS):
        boost -= 0.15

    return boost


def compute_hybrid_metadata_boost(
    title: str,
    labels: list[str],
    query: str,
) -> tuple[float, list[str]]:
    """Compute the hybrid-mode metadata boost and human-readable reasons."""

    boost = 0.0
    reasons: list[str] = []
    label_set = {label.lower() for label in labels}
    title_lower = title.lower()
    query_terms_lower = [term.lower() for term in query_terms(query) if term]

    for label, value in POSITIVE_BOOSTS.items():
        if label in label_set:
            boost += value
            reasons.append(f"label: {label}")

    if query_terms_lower and any(term in title_lower for term in query_terms_lower):
        boost += TITLE_QUERY_BOOST
        reasons.append("title match")

    for label, value in NEGATIVE_BOOSTS.items():
        if label in label_set:
            boost += value
            reasons.append(f"penalty: {label}")

    if any(keyword in title_lower for keyword in NEGATIVE_TITLE_KEYWORDS):
        boost += TITLE_RISKY_PENALTY
        reasons.append("penalty: risky title")

    return boost, reasons


def should_exclude_result(title: str, labels: list[str], include_draft: bool) -> bool:
    """Filter out draft-like results unless explicitly included."""

    if include_draft:
        return False

    label_set = {label.lower() for label in labels}
    title_lower = title.lower()
    if label_set & NEGATIVE_LABELS:
        return True
    return any(keyword in title_lower for keyword in NEGATIVE_TITLE_KEYWORDS)


def query_results(
    connection: sqlite3.Connection,
    *,
    query: str,
    space_key: str | None,
    allowed_page_ids: set[str] | None,
    top_k: int,
    include_draft: bool,
) -> list[SearchResult]:
    """Execute FTS search and return normalized results."""

    match_query = build_match_query(query)
    limit = max(top_k * SEARCH_EXPANSION_FACTOR, top_k)
    sql = """
        SELECT
          c.chunk_id,
          c.page_id,
          c.path,
          c.title,
          c.headings,
          c.start_line,
          c.end_line,
          c.body,
          d.url,
          d.version_number,
          d.version_created_at,
          d.fetched_at,
          d.labels_json,
          bm25(chunks_fts) AS rank
        FROM chunks_fts
        JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
        JOIN documents d ON d.doc_id = c.doc_id
        WHERE chunks_fts MATCH ?
    """
    params: list[Any] = [match_query]
    if space_key:
        sql += " AND c.space_key = ?"
        params.append(space_key)
    sql += " ORDER BY rank ASC LIMIT ?"
    params.append(limit)

    try:
        rows = connection.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        fallback_match_query = f'"{query.replace(chr(34), "")}"'
        fallback_params: list[Any] = [fallback_match_query]
        if space_key:
            fallback_params.append(space_key)
        fallback_params.append(limit)
        rows = connection.execute(sql, fallback_params).fetchall()

    results: list[SearchResult] = []
    for row in rows:
        if allowed_page_ids is not None and row["page_id"] not in allowed_page_ids:
            continue
        labels = json.loads(row["labels_json"]) if row["labels_json"] else []
        if should_exclude_result(row["title"], labels, include_draft):
            continue

        rank = float(row["rank"])
        score = (-rank) + compute_metadata_boost(row["title"], labels, query)
        headings = [part.strip() for part in (row["headings"] or "").split(" > ") if part.strip()]
        results.append(
            SearchResult(
                chunk_id=row["chunk_id"],
                page_id=row["page_id"],
                path=row["path"],
                title=row["title"],
                headings=headings,
                start_line=row["start_line"],
                end_line=row["end_line"],
                body=row["body"],
                url=row["url"],
                version_number=row["version_number"],
                version_created_at=row["version_created_at"],
                fetched_at=row["fetched_at"],
                labels=labels,
                rank=rank,
                score=score,
                fts_score=-rank,
                match_reason=["keyword match"],
            )
        )

    results.sort(key=lambda item: item.score, reverse=True)
    return results[:top_k]


def _row_for_chunk(connection: sqlite3.Connection, chunk_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT
          c.chunk_id,
          c.page_id,
          c.path,
          c.title,
          c.headings,
          c.start_line,
          c.end_line,
          c.body,
          d.url,
          d.version_number,
          d.version_created_at,
          d.fetched_at,
          d.labels_json
        FROM chunks c
        JOIN documents d ON d.doc_id = c.doc_id
        WHERE c.chunk_id = ?
        """,
        (chunk_id,),
    ).fetchone()


def _build_search_result_from_row(
    row: sqlite3.Row,
    *,
    rank: float,
    score: float,
) -> SearchResult:
    labels = json.loads(row["labels_json"]) if row["labels_json"] else []
    headings = [part.strip() for part in (row["headings"] or "").split(" > ") if part.strip()]
    return SearchResult(
        chunk_id=row["chunk_id"],
        page_id=row["page_id"],
        path=row["path"],
        title=row["title"],
        headings=headings,
        start_line=row["start_line"],
        end_line=row["end_line"],
        body=row["body"],
        url=row["url"],
        version_number=row["version_number"],
        version_created_at=row["version_created_at"],
        fetched_at=row["fetched_at"],
        labels=labels,
        rank=rank,
        score=score,
    )


def vector_search(
    connection: sqlite3.Connection,
    *,
    query: str,
    space_key: str | None,
    allowed_page_ids: set[str] | None,
    top_k: int,
    include_draft: bool,
    config: VectorBackendConfig,
    embedder_factory: Callable[[str], Any] | None = None,
    faiss_module: Any | None = None,
    numpy_module: Any | None = None,
) -> list[SearchResult]:
    """Run a FAISS-backed vector search and return SearchResult rows."""

    if config.backend != VECTOR_BACKEND_FAISS:
        raise VectorIndexError(
            f"Vector backend {config.backend!r} はサポートされていません。"
        )

    meta = read_vector_meta(config.vector_meta_path)
    verify_meta_compatibility(meta, expected_model=config.embedding_model)

    sqlite_count = count_vector_chunks(connection)
    if sqlite_count != meta.chunk_count:
        raise VectorIndexError(
            "FAISS index と vector_chunks の件数が一致しません ("
            f"index={meta.chunk_count}, sqlite={sqlite_count})。"
            " ベクトルインデックスを再構築してください。"
        )

    if meta.chunk_count == 0:
        return []

    index = read_faiss_index(config.faiss_index_path, faiss_module=faiss_module)
    expansion = max(top_k * SEARCH_EXPANSION_FACTOR, top_k)
    query_embedding = embed_query(
        query,
        config=config,
        embedder_factory=embedder_factory,
        numpy_module=numpy_module,
    )
    hits = search_faiss(index, query_embedding, expansion)

    results: list[SearchResult] = []
    for hit_rank, hit in enumerate(hits, start=1):
        vector_row = get_vector_chunk_by_vector_id(connection, hit.vector_id)
        if not vector_row:
            continue
        if space_key and vector_row["space_key"] != space_key:
            continue
        if allowed_page_ids is not None and vector_row["page_id"] not in allowed_page_ids:
            continue
        chunk_row = _row_for_chunk(connection, vector_row["chunk_id"])
        if chunk_row is None:
            continue
        labels = json.loads(chunk_row["labels_json"]) if chunk_row["labels_json"] else []
        if should_exclude_result(chunk_row["title"], labels, include_draft):
            continue
        result = _build_search_result_from_row(chunk_row, rank=float(hit_rank), score=hit.score)
        result.vector_score = hit.score
        result.vector_rank = hit_rank
        result.match_reason = ["semantic match"]
        results.append(result)
        if len(results) >= top_k:
            break
    return results


def hybrid_search(
    connection: sqlite3.Connection,
    *,
    query: str,
    space_key: str | None,
    allowed_page_ids: set[str] | None,
    top_k: int,
    fts_k: int,
    vector_k: int,
    include_draft: bool,
    config: VectorBackendConfig,
    embedder_factory: Callable[[str], Any] | None = None,
    faiss_module: Any | None = None,
    numpy_module: Any | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> list[SearchResult]:
    """Combine FTS and vector hits with Reciprocal Rank Fusion."""

    fts_results = query_results(
        connection,
        query=query,
        space_key=space_key,
        allowed_page_ids=allowed_page_ids,
        top_k=fts_k,
        include_draft=include_draft,
    )

    vector_results: list[SearchResult] = []
    try:
        vector_results = vector_search(
            connection,
            query=query,
            space_key=space_key,
            allowed_page_ids=allowed_page_ids,
            top_k=vector_k,
            include_draft=include_draft,
            config=config,
            embedder_factory=embedder_factory,
            faiss_module=faiss_module,
            numpy_module=numpy_module,
        )
    except (VectorBackendUnavailableError, VectorIndexError, VectorMetaMismatchError) as exc:
        message = (
            f"vector search を無効化して FTS のみで継続します: {exc}"
        )
        if on_warning:
            on_warning(message)

    merged: dict[str, SearchResult] = {}
    for index, result in enumerate(fts_results, start=1):
        merged[result.chunk_id] = result
        result.fts_rank = index

    for index, result in enumerate(vector_results, start=1):
        existing = merged.get(result.chunk_id)
        if existing is None:
            merged[result.chunk_id] = result
            result.vector_rank = index
        else:
            existing.vector_rank = index
            existing.vector_score = result.vector_score
            if "semantic match" not in existing.match_reason:
                existing.match_reason.append("semantic match")

    for result in merged.values():
        score = 0.0
        if result.fts_rank is not None:
            score += 1.0 / (RRF_K + result.fts_rank)
        if result.vector_rank is not None:
            score += 1.0 / (RRF_K + result.vector_rank)
        boost, boost_reasons = compute_hybrid_metadata_boost(result.title, result.labels, query)
        result.metadata_boost = boost
        result.match_reason.extend(boost_reasons)
        result.final_score = score + boost
        result.score = result.final_score

    ordered = sorted(merged.values(), key=lambda item: (item.final_score or 0.0), reverse=True)
    return ordered[:top_k]


def excerpt_from_body(body: str, limit: int = 240) -> str:
    """Create a short excerpt from the chunk body."""

    compact = " ".join(line.strip() for line in body.splitlines() if line.strip())
    return compact[:limit]


def highlight_excerpt(text: str, query: str) -> str:
    """Highlight query terms in a short excerpt."""

    highlighted = text
    for term in sorted(query_terms(query), key=len, reverse=True):
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        highlighted = pattern.sub(lambda match: f"[[{match.group(0)}]]", highlighted)
    return highlighted


def group_results_by_page(results: list[SearchResult]) -> list[SearchPageGroup]:
    """Group chunk-level results into page-level result groups."""

    grouped: dict[str, list[SearchResult]] = {}
    order: list[str] = []
    for result in results:
        if result.page_id not in grouped:
            grouped[result.page_id] = []
            order.append(result.page_id)
        grouped[result.page_id].append(result)

    page_groups: list[SearchPageGroup] = []
    for page_id in order:
        page_results = grouped[page_id]
        top_result = max(page_results, key=lambda item: item.score)
        page_groups.append(
            SearchPageGroup(
                page_id=page_id,
                path=top_result.path,
                title=top_result.title,
                url=top_result.url,
                version_number=top_result.version_number,
                version_created_at=top_result.version_created_at,
                fetched_at=top_result.fetched_at,
                labels=top_result.labels,
                score=top_result.score,
                results=sorted(page_results, key=lambda item: item.score, reverse=True),
            )
        )

    page_groups.sort(key=lambda item: item.score, reverse=True)
    return page_groups


def render_markdown(
    results: list[SearchResult],
    query: str,
    space_key: str | None,
    root_page_id: str | None,
    top_k: int,
    *,
    mode: str = MODE_FTS,
    explain: bool = False,
) -> str:
    """Render search results in Markdown format."""

    page_groups = group_results_by_page(results)
    target_filter = (
        f"page_tree:{space_key}:{root_page_id}"
        if space_key and root_page_id
        else "ALL"
    )

    lines = [
        "# Search Results",
        "",
        f"Query: {query}  ",
        f"Space: {space_key or 'ALL'}  ",
        f"Root Page: {root_page_id or 'ALL'}  ",
        f"Target Filter: {target_filter}  ",
        f"Mode: {mode}  ",
        f"Top K: {top_k}",
        "",
    ]

    for index, group in enumerate(page_groups, start=1):
        label_text = ", ".join(group.labels)
        lines.extend(
            [
                f"## {index}. {group.title}",
                "",
                f"- Score: {group.score:.3f}",
                f"- Path: {group.path}",
                f"- URL: {group.url or ''}",
                f"- Version: {group.version_number or ''}",
                f"- Updated: {group.version_created_at or ''}",
                f"- Fetched: {group.fetched_at or ''}",
                f"- Labels: {label_text}",
                f"- Matching Chunks: {len(group.results)}",
                "",
            ]
        )
        for match_index, result in enumerate(group.results, start=1):
            heading_text = " > ".join(result.headings) if result.headings else result.title
            line_range = (
                f"{result.start_line}-{result.end_line}"
                if result.start_line is not None and result.end_line is not None
                else ""
            )
            lines.extend(
                [
                    f"### Match {match_index}: {heading_text}",
                    "",
                    f"- Lines: {line_range}",
                    f"- Chunk Score: {result.score:.3f}",
                ]
            )
            if mode == MODE_HYBRID:
                lines.extend(
                    [
                        f"- FTS Rank: {result.fts_rank if result.fts_rank is not None else '-'}",
                        f"- Vector Rank: {result.vector_rank if result.vector_rank is not None else '-'}",
                        f"- Match: {', '.join(result.match_reason) or '-'}",
                    ]
                )
            elif mode == MODE_VECTOR:
                lines.append(
                    f"- Vector Score: {result.vector_score:.4f}"
                    if result.vector_score is not None
                    else "- Vector Score: -"
                )
            lines.extend(
                [
                    "",
                    f"> {highlight_excerpt(excerpt_from_body(result.body), query)}",
                    "",
                ]
            )
            if explain:
                lines.extend(_render_score_breakdown(result))

    return "\n".join(lines).rstrip() + "\n"


def _render_score_breakdown(result: SearchResult) -> list[str]:
    final_score = result.final_score if result.final_score is not None else result.score
    breakdown = [
        "### Score Breakdown",
        "",
        f"- final_score: {final_score:.4f}",
        f"- fts_rank: {result.fts_rank if result.fts_rank is not None else '-'}",
        f"- vector_rank: {result.vector_rank if result.vector_rank is not None else '-'}",
        f"- fts_score: {result.fts_score:.4f}" if result.fts_score is not None else "- fts_score: -",
        f"- vector_score: {result.vector_score:.4f}" if result.vector_score is not None else "- vector_score: -",
        f"- metadata_boost: {result.metadata_boost:.4f}",
        "- match_reason:",
    ]
    for reason in result.match_reason:
        breakdown.append(f"  - {reason}")
    if not result.match_reason:
        breakdown.append("  - -")
    breakdown.append("")
    return breakdown


def render_json(results: list[SearchResult]) -> str:
    """Render search results in JSON format."""

    payload = [
        {
            "score": result.score,
            "final_score": result.final_score,
            "chunk_id": result.chunk_id,
            "path": result.path,
            "line_range": (
                f"{result.start_line}-{result.end_line}"
                if result.start_line is not None and result.end_line is not None
                else None
            ),
            "title": result.title,
            "headings": result.headings,
            "url": result.url,
            "version_number": result.version_number,
            "version_created_at": result.version_created_at,
            "fetched_at": result.fetched_at,
            "labels": result.labels,
            "excerpt": excerpt_from_body(result.body),
            "fts_rank": result.fts_rank,
            "vector_rank": result.vector_rank,
            "fts_score": result.fts_score,
            "vector_score": result.vector_score,
            "metadata_boost": result.metadata_boost,
            "match_reason": result.match_reason,
        }
        for result in results
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def resolve_search_vector_config(cli_value: str | None) -> VectorBackendConfig:
    """Build a VectorBackendConfig respecting CLI overrides."""

    config = load_vector_backend_config()
    if cli_value is not None:
        config.backend = cli_value
    return config


def main() -> int:
    """Run the search CLI."""

    parser = build_parser()
    args = parser.parse_args()
    allowed_page_ids = resolve_allowed_page_ids(
        state_db_path=DEFAULT_SYNC_DB_PATH,
        space_key=args.space,
        root_page_id=args.root_page_id,
    )

    vector_config = resolve_search_vector_config(args.vector_backend)

    def warn(message: str) -> None:
        print(f"[search_docs] WARNING: {message}", file=sys.stderr)

    with connect_index_db(DEFAULT_INDEX_DB_PATH) as connection:
        if args.mode == MODE_VECTOR:
            if vector_config.backend != VECTOR_BACKEND_FAISS:
                parser.error(
                    "--mode vector を使うには DOC_VECTOR_BACKEND=faiss か --vector-backend faiss が必要です。"
                )
            try:
                results = vector_search(
                    connection,
                    query=args.query,
                    space_key=args.space,
                    allowed_page_ids=allowed_page_ids,
                    top_k=args.top_k,
                    include_draft=args.include_draft,
                    config=vector_config,
                )
            except (VectorBackendUnavailableError, VectorIndexError, VectorMetaMismatchError) as exc:
                print(f"[search_docs] ERROR: {exc}", file=sys.stderr)
                return 2
        elif args.mode == MODE_HYBRID:
            results = hybrid_search(
                connection,
                query=args.query,
                space_key=args.space,
                allowed_page_ids=allowed_page_ids,
                top_k=args.top_k,
                fts_k=args.fts_k,
                vector_k=args.vector_k,
                include_draft=args.include_draft,
                config=vector_config,
                on_warning=warn,
            )
        else:
            results = query_results(
                connection,
                query=args.query,
                space_key=args.space,
                allowed_page_ids=allowed_page_ids,
                top_k=args.top_k,
                include_draft=args.include_draft,
            )

    if args.path_only:
        for result in results:
            print(result.path)
        return 0

    if args.open_result and results:
        subprocess.run(["open", results[0].path], check=False)

    if args.as_json:
        print(render_json(results))
        return 0

    print(
        render_markdown(
            results,
            args.query,
            args.space,
            args.root_page_id,
            args.top_k,
            mode=args.mode,
            explain=args.explain,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
