"""Search the local SQLite FTS5 index."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from db import build_page_tree_target_key, connect_index_db, connect_state_db, list_page_targets_for_target
from utils import DEFAULT_INDEX_DB_PATH, DEFAULT_SYNC_DB_PATH


DEFAULT_TOP_K = 10
SEARCH_EXPANSION_FACTOR = 5
NEGATIVE_TITLE_KEYWORDS = ("旧", "廃止", "コピー", "メモ", "検討中", "draft", "old", "deprecated", "wip")
NEGATIVE_LABELS = {"draft", "wip", "deprecated", "old"}
POSITIVE_LABELS = {"official", "current", "approved"}


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
    """Apply a small metadata-based boost/penalty."""

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
            )
        )

    results.sort(key=lambda item: item.score, reverse=True)
    return results[:top_k]


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
                    "",
                    f"> {highlight_excerpt(excerpt_from_body(result.body), query)}",
                    "",
                ]
            )

    return "\n".join(lines).rstrip() + "\n"


def render_json(results: list[SearchResult]) -> str:
    """Render search results in JSON format."""

    payload = [
        {
            "score": result.score,
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
        }
        for result in results
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> int:
    """Run the search CLI."""

    parser = build_parser()
    args = parser.parse_args()
    allowed_page_ids = resolve_allowed_page_ids(
        state_db_path=DEFAULT_SYNC_DB_PATH,
        space_key=args.space,
        root_page_id=args.root_page_id,
    )

    with connect_index_db(DEFAULT_INDEX_DB_PATH) as connection:
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

    print(render_markdown(results, args.query, args.space, args.root_page_id, args.top_k))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
