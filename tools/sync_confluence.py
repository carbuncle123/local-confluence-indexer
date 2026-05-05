"""CLI for syncing Confluence pages to local Markdown."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from confluence_client import ConfluenceClient, ConfluenceConfig, format_cql_since, load_config
from db import (
    PageRecord,
    SpaceRecord,
    SyncErrorRecord,
    SyncRunRecord,
    connect_state_db,
    create_sync_run,
    get_page,
    get_space,
    get_sync_state,
    has_failed_pages,
    increment_sync_run_counter,
    initialize_state_db,
    list_pages_for_space,
    record_sync_completed,
    record_sync_error,
    record_sync_started,
    upsert_page,
    upsert_space,
    complete_sync_run,
)
from markdown_converter import convert_page_to_markdown, extract_labels, slugify
from utils import ensure_parent_directory, json_dumps, now_iso


def build_parser() -> argparse.ArgumentParser:
    """Build the sync CLI parser."""

    parser = argparse.ArgumentParser(
        description="Confluence ページをローカル Markdown に同期します。"
    )
    parser.add_argument("--base-url")
    parser.add_argument("--email")
    parser.add_argument("--api-token")

    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("full", "incremental", "page"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--space", help="Confluence space key")
        subparser.add_argument("--reindex", action="store_true")
        subparser.add_argument("--dry-run", action="store_true")
        subparser.add_argument("--force", action="store_true")
        if command == "page":
            subparser.add_argument("--page-id", required=True, help="Confluence page id")

    return parser


def build_runtime(args: argparse.Namespace) -> tuple[ConfluenceConfig, ConfluenceClient]:
    """Load config and create the API client."""

    config = load_config(
        space_key=args.space,
        base_url=args.base_url,
        email=args.email,
        api_token=args.api_token,
    )
    return config, ConfluenceClient(config)


def get_space_key(args: argparse.Namespace, config: ConfluenceConfig) -> str:
    """Resolve the target space key."""

    space_key = args.space or config.default_space
    if not space_key:
        raise ValueError("A space key is required via --space or CONFLUENCE_DEFAULT_SPACE.")
    return space_key


def new_run_id() -> str:
    """Create a stable run id string."""

    return str(uuid.uuid4())


def docs_space_dir(config: ConfluenceConfig, space_key: str) -> Path:
    return Path(config.docs_dir) / space_key


def raw_space_dir(config: ConfluenceConfig, space_key: str) -> Path:
    return Path(config.sync_dir) / "raw" / space_key


def save_raw_page(config: ConfluenceConfig, space_key: str, page: dict[str, Any]) -> dict[str, str]:
    """Persist raw page JSON and storage XHTML."""

    raw_dir = raw_space_dir(config, space_key)
    page_id = page["id"]
    json_path = ensure_parent_directory(raw_dir / f"{page_id}.page.json")
    html_path = ensure_parent_directory(raw_dir / f"{page_id}.storage.html")

    json_path.write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8")
    storage_html = page.get("body", {}).get("storage", {}).get("value", "")
    html_path.write_text(storage_html, encoding="utf-8")

    return {"raw_json_path": str(json_path), "raw_storage_path": str(html_path)}


def write_markdown(config: ConfluenceConfig, space_key: str, page: dict[str, Any], markdown: str) -> str:
    """Write a converted Markdown page to disk."""

    slug = slugify(page.get("title", ""))
    output_path = ensure_parent_directory(
        docs_space_dir(config, space_key) / "pages" / f"{page['id']}__{slug}.md"
    )
    output_path.write_text(markdown, encoding="utf-8")
    return str(output_path)


def page_record_from_payload(
    page: dict[str, Any],
    *,
    space_key: str,
    local_path: str,
    raw_paths: dict[str, str],
    content_hash: str,
    fetched_at: str,
) -> PageRecord:
    """Convert a page payload into a DB record."""

    version = page.get("version") or {}
    labels = extract_labels(page)
    source_url = ""
    base = page.get("_links", {}).get("base")
    webui = page.get("_links", {}).get("webui")
    if base and webui:
        source_url = f"{base}{webui}"

    return PageRecord(
        page_id=page["id"],
        space_key=space_key,
        space_id=page.get("spaceId", ""),
        title=page.get("title", ""),
        status=page.get("status"),
        parent_id=page.get("parentId"),
        author_id=page.get("authorId"),
        owner_id=page.get("ownerId"),
        created_at=page.get("createdAt"),
        version_number=int(version.get("number", 0)),
        version_created_at=version.get("createdAt"),
        version_message=version.get("message"),
        version_minor_edit=int(bool(version.get("minorEdit"))) if version.get("minorEdit") is not None else None,
        version_author_id=version.get("authorId"),
        source_url=source_url or None,
        webui_path=webui,
        local_path=local_path,
        raw_json_path=raw_paths["raw_json_path"],
        raw_storage_path=raw_paths["raw_storage_path"],
        labels_json=json_dumps(labels),
        content_hash=content_hash,
        fetched_at=fetched_at,
        last_seen_at=fetched_at,
        deleted_or_missing=0,
        metadata_json=json_dumps({"type": "confluence_page"}),
    )


def regenerate_manifest(config: ConfluenceConfig, state_connection: Any, space_key: str) -> None:
    """Regenerate manifest.jsonl for a space."""

    pages = sorted(
        list_pages_for_space(state_connection, space_key),
        key=lambda item: (item["space_key"], item["title"], item["page_id"]),
    )
    manifest_path = ensure_parent_directory(docs_space_dir(config, space_key) / "manifest.jsonl")
    lines: list[str] = []
    for page in pages:
        labels = json.loads(page["labels_json"]) if page.get("labels_json") else []
        line = {
            "page_id": page["page_id"],
            "path": page["local_path"],
            "title": page["title"],
            "space_key": page["space_key"],
            "space_id": page["space_id"],
            "version_number": page["version_number"],
            "version_created_at": page["version_created_at"],
            "fetched_at": page["fetched_at"],
            "labels": labels,
            "status": page["status"],
            "url": page["source_url"],
            "content_hash": page["content_hash"],
            "include_in_index": True,
        }
        lines.append(json.dumps(line, ensure_ascii=False))
    manifest_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def regenerate_index_markdown(config: ConfluenceConfig, state_connection: Any, space_key: str) -> None:
    """Regenerate index.md for a space."""

    pages = list_pages_for_space(state_connection, space_key)
    if pages:
        space_id = pages[0]["space_id"]
    else:
        space = get_space(state_connection, space_key)
        space_id = space["space_id"] if space else ""

    updated_at = now_iso()
    lines = [
        f"# Confluence Export: {space_key}",
        "",
        f"- Space key: {space_key}",
        f"- Space id: {space_id}",
        f"- Exported at: {updated_at}",
        f"- Pages: {len(pages)}",
        "- Manifest: ./manifest.jsonl",
        "",
        "## Usage for Codex CLI",
        "",
        f'社内仕様を調べる場合は、まずこのファイルを確認し、必要に応じて `uv run python tools/search_docs.py \"query\" --space {space_key}` を実行すること。',
        "",
        "## All Pages",
        "",
        "| Title | Path | Version | Updated | Labels |",
        "|---|---|---:|---|---|",
    ]

    for page in sorted(pages, key=lambda item: (item["title"], item["page_id"])):
        labels = ", ".join(json.loads(page["labels_json"])) if page.get("labels_json") else ""
        path = Path(page["local_path"])
        relative_path = path.relative_to(docs_space_dir(config, space_key))
        lines.append(
            f"| {page['title']} | {relative_path.as_posix()} | {page['version_number']} | {page['version_created_at'] or ''} | {labels} |"
        )

    index_path = ensure_parent_directory(docs_space_dir(config, space_key) / "index.md")
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def maybe_reindex(config: ConfluenceConfig, space_key: str) -> None:
    """Run the index builder after a successful sync."""

    subprocess.run(
        ["uv", "run", "python", "tools/build_doc_index.py", "--space", space_key],
        check=True,
        env={**os.environ, "UV_CACHE_DIR": ".uv-cache"},
    )


def extract_page_id_from_search_result(candidate: dict[str, Any]) -> str | None:
    """Extract a page id from a CQL search result."""

    content = candidate.get("content") or {}
    if content.get("id"):
        return str(content["id"])
    result_id = candidate.get("id")
    return str(result_id) if result_id else None


def sync_single_page(
    *,
    client: ConfluenceClient,
    config: ConfluenceConfig,
    state_connection: Any,
    run_id: str,
    space_key: str,
    page_id: str,
    force: bool,
) -> None:
    """Fetch, convert, persist, and record a single page."""

    page = client.get_page_detail(page_id, body_format="storage", include_labels=True, include_version=True)
    page["space_key"] = space_key
    local = get_page(state_connection, page_id)
    remote_version = int(page.get("version", {}).get("number", 0))

    increment_sync_run_counter(state_connection, run_id, "fetched_pages")

    if local and int(local["version_number"]) == remote_version and not force:
        increment_sync_run_counter(state_connection, run_id, "skipped_pages")
        return

    fetched_at = now_iso()
    raw_paths = save_raw_page(config, space_key, page)
    converted = convert_page_to_markdown(page, fetched_at)
    local_path = write_markdown(config, space_key, page, converted.markdown)
    record = page_record_from_payload(
        page,
        space_key=space_key,
        local_path=local_path,
        raw_paths=raw_paths,
        content_hash=converted.content_hash,
        fetched_at=fetched_at,
    )
    upsert_page(state_connection, record)
    increment_sync_run_counter(state_connection, run_id, "updated_pages")


def run_full_sync(
    *,
    client: ConfluenceClient,
    config: ConfluenceConfig,
    space_key: str,
    force: bool,
    reindex: bool,
) -> int:
    """Run a full sync for a space."""

    with connect_state_db() as state_connection:
        initialize_state_db(state_connection)
        run_id = new_run_id()
        started_at = now_iso()
        space = client.get_space_by_key(space_key)
        space_id = str(space["id"])

        create_sync_run(
            state_connection,
            SyncRunRecord(
                run_id=run_id,
                space_key=space_key,
                mode="full",
                started_at=started_at,
                status="running",
            ),
        )
        record_sync_started(state_connection, space_key, space_id, started_at)
        upsert_space(
            state_connection,
            SpaceRecord(
                space_key=space_key,
                space_id=space_id,
                name=space.get("name"),
                homepage_id=space.get("homepageId"),
                metadata_json=json_dumps(space),
            ),
        )

        try:
            summaries = client.list_pages_in_space(space_id=space_id, status="current", body_format="storage")
            for summary in summaries:
                page_id = str(summary["id"])
                try:
                    sync_single_page(
                        client=client,
                        config=config,
                        state_connection=state_connection,
                        run_id=run_id,
                        space_key=space_key,
                        page_id=page_id,
                        force=force,
                    )
                except Exception as exc:
                    record_sync_error(
                        state_connection,
                        SyncErrorRecord(
                            run_id=run_id,
                            space_key=space_key,
                            page_id=page_id,
                            operation="sync_page",
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                            created_at=now_iso(),
                        ),
                    )
                    increment_sync_run_counter(state_connection, run_id, "failed_pages")

            regenerate_manifest(config, state_connection, space_key)
            regenerate_index_markdown(config, state_connection, space_key)

            if has_failed_pages(state_connection, run_id):
                complete_sync_run(state_connection, run_id, status="partial_failed")
                record_sync_completed(
                    state_connection,
                    space_key,
                    space_id,
                    completed_at=now_iso(),
                    last_error="One or more pages failed during sync.",
                )
            else:
                complete_sync_run(state_connection, run_id, status="success")
                record_sync_completed(
                    state_connection,
                    space_key,
                    space_id,
                    completed_at=now_iso(),
                    success_at=started_at,
                )
                if reindex:
                    maybe_reindex(config, space_key)
            return 0
        except Exception as exc:
            record_sync_error(
                state_connection,
                SyncErrorRecord(
                    run_id=run_id,
                    space_key=space_key,
                    operation="full_sync",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    created_at=now_iso(),
                ),
            )
            complete_sync_run(state_connection, run_id, status="failed", error_message=str(exc))
            record_sync_completed(
                state_connection,
                space_key,
                space_id,
                completed_at=now_iso(),
                last_error=str(exc),
            )
            raise


def run_incremental_sync(
    *,
    client: ConfluenceClient,
    config: ConfluenceConfig,
    space_key: str,
    force: bool,
    reindex: bool,
    dry_run: bool,
) -> int:
    """Run an incremental sync."""

    with connect_state_db() as state_connection:
        initialize_state_db(state_connection)
        state = get_sync_state(state_connection, space_key)
        if not state or not state.get("last_successful_sync_at"):
            if dry_run:
                print("No previous sync. full sync is required.")
                return 0
            return run_full_sync(
                client=client,
                config=config,
                space_key=space_key,
                force=force,
                reindex=reindex,
            )

        run_id = new_run_id()
        started_at = now_iso()
        space = get_space(state_connection, space_key)
        if space:
            space_id = space["space_id"]
        else:
            remote_space = client.get_space_by_key(space_key)
            space_id = str(remote_space["id"])

        create_sync_run(
            state_connection,
            SyncRunRecord(
                run_id=run_id,
                space_key=space_key,
                mode="incremental",
                started_at=started_at,
                status="running",
            ),
        )
        record_sync_started(state_connection, space_key, space_id, started_at)

        since = format_cql_since(
            state["last_successful_sync_at"],
            config.incremental_overlap_minutes,
        )
        candidates = client.search_updated_pages_by_cql(space_key, since)

        if dry_run:
            print(json.dumps(candidates, ensure_ascii=False, indent=2))
            complete_sync_run(state_connection, run_id, status="dry_run")
            return 0

        seen_page_ids: set[str] = set()
        for candidate in candidates:
            page_id = extract_page_id_from_search_result(candidate)
            if not page_id or page_id in seen_page_ids:
                continue
            seen_page_ids.add(page_id)
            try:
                sync_single_page(
                    client=client,
                    config=config,
                    state_connection=state_connection,
                    run_id=run_id,
                    space_key=space_key,
                    page_id=page_id,
                    force=force,
                )
            except Exception as exc:
                record_sync_error(
                    state_connection,
                    SyncErrorRecord(
                        run_id=run_id,
                        space_key=space_key,
                        page_id=page_id,
                        operation="sync_page",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        created_at=now_iso(),
                    ),
                )
                increment_sync_run_counter(state_connection, run_id, "failed_pages")

        regenerate_manifest(config, state_connection, space_key)
        regenerate_index_markdown(config, state_connection, space_key)

        if has_failed_pages(state_connection, run_id):
            complete_sync_run(state_connection, run_id, status="partial_failed")
            record_sync_completed(
                state_connection,
                space_key,
                space_id,
                completed_at=now_iso(),
                last_error="One or more pages failed during sync.",
            )
        else:
            complete_sync_run(state_connection, run_id, status="success")
            record_sync_completed(
                state_connection,
                space_key,
                space_id,
                completed_at=now_iso(),
                success_at=started_at,
            )
            if reindex:
                maybe_reindex(config, space_key)
        return 0


def run_page_sync(
    *,
    client: ConfluenceClient,
    config: ConfluenceConfig,
    space_key: str,
    page_id: str,
    force: bool,
    reindex: bool,
) -> int:
    """Sync a single page by id."""

    with connect_state_db() as state_connection:
        initialize_state_db(state_connection)
        space = get_space(state_connection, space_key) or client.get_space_by_key(space_key)
        space_id = str(space["space_id"] if "space_id" in space else space["id"])
        run_id = new_run_id()
        started_at = now_iso()

        create_sync_run(
            state_connection,
            SyncRunRecord(
                run_id=run_id,
                space_key=space_key,
                mode="page",
                started_at=started_at,
                status="running",
            ),
        )
        record_sync_started(state_connection, space_key, space_id, started_at)

        sync_single_page(
            client=client,
            config=config,
            state_connection=state_connection,
            run_id=run_id,
            space_key=space_key,
            page_id=page_id,
            force=force,
        )
        regenerate_manifest(config, state_connection, space_key)
        regenerate_index_markdown(config, state_connection, space_key)
        complete_sync_run(state_connection, run_id, status="success")
        record_sync_completed(
            state_connection,
            space_key,
            space_id,
            completed_at=now_iso(),
            success_at=started_at,
        )
        if reindex:
            maybe_reindex(config, space_key)
        return 0


def main() -> int:
    """Run the sync CLI."""

    parser = build_parser()
    args = parser.parse_args()
    config, client = build_runtime(args)
    space_key = get_space_key(args, config)

    if args.command == "full":
        return run_full_sync(
            client=client,
            config=config,
            space_key=space_key,
            force=args.force,
            reindex=args.reindex,
        )
    if args.command == "incremental":
        return run_incremental_sync(
            client=client,
            config=config,
            space_key=space_key,
            force=args.force,
            reindex=args.reindex,
            dry_run=args.dry_run,
        )
    return run_page_sync(
        client=client,
        config=config,
        space_key=space_key,
        page_id=args.page_id,
        force=args.force,
        reindex=args.reindex,
    )


if __name__ == "__main__":
    raise SystemExit(main())
