"""Database helpers for sync state and local search indexes."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from utils import DEFAULT_INDEX_DB_PATH, DEFAULT_SYNC_DB_PATH, ensure_parent_directory, now_iso


SYNC_TARGETS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_targets (
  target_key TEXT PRIMARY KEY,
  target_type TEXT NOT NULL,
  space_key TEXT NOT NULL,
  space_id TEXT NOT NULL,
  root_page_id TEXT,
  name TEXT,
  last_resolved_at TEXT NOT NULL,
  metadata_json TEXT
);
"""

PAGE_TARGETS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS page_targets (
  target_key TEXT NOT NULL,
  page_id TEXT NOT NULL,
  space_key TEXT NOT NULL,
  included INTEGER NOT NULL DEFAULT 1,
  last_seen_at TEXT NOT NULL,
  metadata_json TEXT,
  PRIMARY KEY (target_key, page_id)
);
"""

STATE_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS spaces (
  space_key TEXT PRIMARY KEY,
  space_id TEXT NOT NULL,
  name TEXT,
  homepage_id TEXT,
  last_resolved_at TEXT NOT NULL,
  metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS pages (
  page_id TEXT PRIMARY KEY,
  space_key TEXT NOT NULL,
  space_id TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT,
  parent_id TEXT,
  author_id TEXT,
  owner_id TEXT,
  created_at TEXT,
  version_number INTEGER NOT NULL,
  version_created_at TEXT,
  version_message TEXT,
  version_minor_edit INTEGER,
  version_author_id TEXT,
  source_url TEXT,
  webui_path TEXT,
  local_path TEXT NOT NULL,
  raw_json_path TEXT,
  raw_storage_path TEXT,
  labels_json TEXT,
  content_hash TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  deleted_or_missing INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
  target_key TEXT PRIMARY KEY,
  target_type TEXT NOT NULL,
  root_page_id TEXT,
  space_key TEXT NOT NULL,
  space_id TEXT NOT NULL,
  last_successful_sync_at TEXT,
  last_started_at TEXT,
  last_completed_at TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
  run_id TEXT PRIMARY KEY,
  target_key TEXT NOT NULL,
  target_type TEXT NOT NULL,
  root_page_id TEXT,
  space_key TEXT NOT NULL,
  mode TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  fetched_pages INTEGER NOT NULL DEFAULT 0,
  updated_pages INTEGER NOT NULL DEFAULT 0,
  skipped_pages INTEGER NOT NULL DEFAULT 0,
  failed_pages INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  error_message TEXT
);

CREATE TABLE IF NOT EXISTS sync_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  target_key TEXT NOT NULL,
  target_type TEXT NOT NULL,
  root_page_id TEXT,
  space_key TEXT NOT NULL,
  page_id TEXT,
  operation TEXT NOT NULL,
  error_type TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL
);
"""

INDEX_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  space_key TEXT NOT NULL,
  space_id TEXT,
  page_id TEXT NOT NULL,
  path TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT,
  status TEXT,
  parent_id TEXT,
  version_number INTEGER,
  version_created_at TEXT,
  fetched_at TEXT,
  labels_json TEXT,
  content_hash TEXT,
  metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL,
  page_id TEXT NOT NULL,
  space_key TEXT NOT NULL,
  path TEXT NOT NULL,
  title TEXT NOT NULL,
  headings TEXT,
  body TEXT NOT NULL,
  start_line INTEGER,
  end_line INTEGER,
  chunk_index INTEGER NOT NULL,
  token_count INTEGER,
  labels_json TEXT,
  metadata_json TEXT,
  FOREIGN KEY(doc_id) REFERENCES documents(doc_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
USING fts5(
  chunk_id UNINDEXED,
  title,
  headings,
  body,
  tokenize = 'trigram'
);
"""


@dataclass(slots=True)
class SpaceRecord:
    """A row in the spaces table."""

    space_key: str
    space_id: str
    name: str | None = None
    homepage_id: str | None = None
    last_resolved_at: str | None = None
    metadata_json: str | None = None


@dataclass(slots=True)
class PageRecord:
    """A row in the pages table."""

    page_id: str
    space_key: str
    space_id: str
    title: str
    local_path: str
    version_number: int
    content_hash: str
    fetched_at: str
    last_seen_at: str
    status: str | None = None
    parent_id: str | None = None
    author_id: str | None = None
    owner_id: str | None = None
    created_at: str | None = None
    version_created_at: str | None = None
    version_message: str | None = None
    version_minor_edit: int | None = None
    version_author_id: str | None = None
    source_url: str | None = None
    webui_path: str | None = None
    raw_json_path: str | None = None
    raw_storage_path: str | None = None
    labels_json: str | None = None
    deleted_or_missing: int = 0
    metadata_json: str | None = None


@dataclass(slots=True)
class SyncTargetRecord:
    """A row in the sync_targets table."""

    target_key: str
    target_type: str
    space_key: str
    space_id: str
    root_page_id: str | None = None
    name: str | None = None
    last_resolved_at: str | None = None
    metadata_json: str | None = None


@dataclass(slots=True)
class PageTargetRecord:
    """A row in the page_targets table."""

    target_key: str
    page_id: str
    space_key: str
    last_seen_at: str
    included: int = 1
    metadata_json: str | None = None


@dataclass(slots=True)
class SyncStateRecord:
    """A row in the sync_state table."""

    space_key: str
    space_id: str
    updated_at: str
    target_key: str | None = None
    target_type: str = "space"
    root_page_id: str | None = None
    last_successful_sync_at: str | None = None
    last_started_at: str | None = None
    last_completed_at: str | None = None
    last_error: str | None = None


@dataclass(slots=True)
class SyncRunRecord:
    """A row in the sync_runs table."""

    run_id: str
    space_key: str
    mode: str
    started_at: str
    status: str
    target_key: str | None = None
    target_type: str = "space"
    root_page_id: str | None = None
    completed_at: str | None = None
    fetched_pages: int = 0
    updated_pages: int = 0
    skipped_pages: int = 0
    failed_pages: int = 0
    error_message: str | None = None


@dataclass(slots=True)
class SyncErrorRecord:
    """A row in the sync_errors table."""

    run_id: str
    space_key: str
    operation: str
    error_message: str
    created_at: str
    target_key: str | None = None
    target_type: str = "space"
    root_page_id: str | None = None
    page_id: str | None = None
    error_type: str | None = None


@dataclass(slots=True)
class DocumentRecord:
    """A row in the documents table."""

    doc_id: str
    source: str
    space_key: str
    page_id: str
    path: str
    title: str
    space_id: str | None = None
    url: str | None = None
    status: str | None = None
    parent_id: str | None = None
    version_number: int | None = None
    version_created_at: str | None = None
    fetched_at: str | None = None
    labels_json: str | None = None
    content_hash: str | None = None
    metadata_json: str | None = None


@dataclass(slots=True)
class ChunkRecord:
    """A row in the chunks table and chunks_fts table."""

    chunk_id: str
    doc_id: str
    page_id: str
    space_key: str
    path: str
    title: str
    body: str
    chunk_index: int
    headings: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    token_count: int | None = None
    labels_json: str | None = None
    metadata_json: str | None = None


def build_space_target_key(space_key: str) -> str:
    """Build a stable target key for a whole-space sync target."""

    return f"space:{space_key}"


def build_page_tree_target_key(space_key: str, root_page_id: str) -> str:
    """Build a stable target key for a page-tree sync target."""

    return f"page_tree:{space_key}:{root_page_id}"


def _connect(path: Path) -> sqlite3.Connection:
    ensure_parent_directory(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def connect_state_db(path: Path = DEFAULT_SYNC_DB_PATH) -> sqlite3.Connection:
    """Open the sync state database."""

    return _connect(path)


def connect_index_db(path: Path = DEFAULT_INDEX_DB_PATH) -> sqlite3.Connection:
    """Open the local search index database."""

    return _connect(path)


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    """Return whether a SQLite table exists."""

    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def list_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    """Return the set of columns defined on a table."""

    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    """Add a missing column to an existing table."""

    if column_name in list_columns(connection, table_name):
        return
    connection.execute(
        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
    )


def migrate_sync_state_table(connection: sqlite3.Connection) -> None:
    """Upgrade sync_state to use target_key as the primary key."""

    if not table_exists(connection, "sync_state"):
        return

    columns = list_columns(connection, "sync_state")
    if "target_key" in columns and "target_type" in columns and "root_page_id" in columns:
        return

    connection.execute("ALTER TABLE sync_state RENAME TO sync_state_legacy")
    connection.execute(
        """
        CREATE TABLE sync_state (
          target_key TEXT PRIMARY KEY,
          target_type TEXT NOT NULL,
          root_page_id TEXT,
          space_key TEXT NOT NULL,
          space_id TEXT NOT NULL,
          last_successful_sync_at TEXT,
          last_started_at TEXT,
          last_completed_at TEXT,
          last_error TEXT,
          updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO sync_state (
          target_key, target_type, root_page_id, space_key, space_id,
          last_successful_sync_at, last_started_at, last_completed_at, last_error, updated_at
        )
        SELECT
          'space:' || space_key,
          'space',
          NULL,
          space_key,
          space_id,
          last_successful_sync_at,
          last_started_at,
          last_completed_at,
          last_error,
          updated_at
        FROM sync_state_legacy
        """
    )
    connection.execute("DROP TABLE sync_state_legacy")


def migrate_state_db(connection: sqlite3.Connection) -> None:
    """Apply additive schema migrations for the sync state database."""

    connection.executescript(SYNC_TARGETS_TABLE_SCHEMA)
    connection.executescript(PAGE_TARGETS_TABLE_SCHEMA)
    migrate_sync_state_table(connection)

    for column_name, definition in (
        ("target_key", "TEXT"),
        ("target_type", "TEXT"),
        ("root_page_id", "TEXT"),
    ):
        ensure_column(connection, "sync_runs", column_name, definition)
        ensure_column(connection, "sync_errors", column_name, definition)

    connection.execute(
        """
        UPDATE sync_runs
        SET target_key = COALESCE(target_key, 'space:' || space_key),
            target_type = COALESCE(target_type, 'space')
        """
    )
    connection.execute(
        """
        UPDATE sync_errors
        SET target_key = COALESCE(target_key, 'space:' || space_key),
            target_type = COALESCE(target_type, 'space')
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO sync_targets (
          target_key, target_type, space_key, space_id, root_page_id, name,
          last_resolved_at, metadata_json
        )
        SELECT
          'space:' || space_key,
          'space',
          space_key,
          space_id,
          NULL,
          name,
          last_resolved_at,
          metadata_json
        FROM spaces
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO sync_targets (
          target_key, target_type, space_key, space_id, root_page_id, name,
          last_resolved_at, metadata_json
        )
        SELECT
          target_key,
          target_type,
          space_key,
          space_id,
          root_page_id,
          NULL,
          updated_at,
          NULL
        FROM sync_state
        """
    )


def initialize_state_db(connection: sqlite3.Connection) -> None:
    """Create the sync-state schema."""

    connection.executescript(STATE_DB_SCHEMA)
    migrate_state_db(connection)
    connection.commit()


def initialize_index_db(connection: sqlite3.Connection) -> None:
    """Create the local index schema."""

    connection.executescript(INDEX_DB_SCHEMA)
    connection.commit()


def initialize_all_databases(
    state_db_path: Path = DEFAULT_SYNC_DB_PATH,
    index_db_path: Path = DEFAULT_INDEX_DB_PATH,
) -> None:
    """Create both databases and their schema."""

    with connect_state_db(state_db_path) as state_connection:
        initialize_state_db(state_connection)
    with connect_index_db(index_db_path) as index_connection:
        initialize_index_db(index_connection)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a SQLite row to a plain dict."""

    if row is None:
        return None
    return dict(row)


def upsert_space(connection: sqlite3.Connection, record: SpaceRecord) -> None:
    """Insert or update a space row."""

    payload = asdict(record)
    if payload["last_resolved_at"] is None:
        payload["last_resolved_at"] = now_iso()

    connection.execute(
        """
        INSERT INTO spaces (
          space_key, space_id, name, homepage_id, last_resolved_at, metadata_json
        ) VALUES (
          :space_key, :space_id, :name, :homepage_id, :last_resolved_at, :metadata_json
        )
        ON CONFLICT(space_key) DO UPDATE SET
          space_id = excluded.space_id,
          name = excluded.name,
          homepage_id = excluded.homepage_id,
          last_resolved_at = excluded.last_resolved_at,
          metadata_json = excluded.metadata_json
        """,
        payload,
    )
    connection.commit()


def get_space(connection: sqlite3.Connection, space_key: str) -> dict[str, Any] | None:
    """Load a single space row."""

    row = connection.execute(
        "SELECT * FROM spaces WHERE space_key = ?",
        (space_key,),
    ).fetchone()
    return row_to_dict(row)


def build_target_record(
    *,
    space_key: str,
    space_id: str,
    target_key: str | None = None,
    target_type: str = "space",
    root_page_id: str | None = None,
    name: str | None = None,
    metadata_json: str | None = None,
) -> SyncTargetRecord:
    """Create a sync target record with sensible defaults."""

    resolved_target_key = target_key
    if resolved_target_key is None:
        if target_type == "page_tree":
            if not root_page_id:
                raise ValueError("root_page_id is required for page_tree targets.")
            resolved_target_key = build_page_tree_target_key(space_key, root_page_id)
        else:
            resolved_target_key = build_space_target_key(space_key)

    return SyncTargetRecord(
        target_key=resolved_target_key,
        target_type=target_type,
        space_key=space_key,
        space_id=space_id,
        root_page_id=root_page_id,
        name=name,
        last_resolved_at=now_iso(),
        metadata_json=metadata_json,
    )


def upsert_sync_target(connection: sqlite3.Connection, record: SyncTargetRecord) -> None:
    """Insert or update a sync target row."""

    payload = asdict(record)
    if payload["last_resolved_at"] is None:
        payload["last_resolved_at"] = now_iso()

    connection.execute(
        """
        INSERT INTO sync_targets (
          target_key, target_type, space_key, space_id, root_page_id, name,
          last_resolved_at, metadata_json
        ) VALUES (
          :target_key, :target_type, :space_key, :space_id, :root_page_id, :name,
          :last_resolved_at, :metadata_json
        )
        ON CONFLICT(target_key) DO UPDATE SET
          target_type = excluded.target_type,
          space_key = excluded.space_key,
          space_id = excluded.space_id,
          root_page_id = excluded.root_page_id,
          name = excluded.name,
          last_resolved_at = excluded.last_resolved_at,
          metadata_json = excluded.metadata_json
        """,
        payload,
    )
    connection.commit()


def get_sync_target(connection: sqlite3.Connection, target_key: str) -> dict[str, Any] | None:
    """Load a sync target row."""

    row = connection.execute(
        "SELECT * FROM sync_targets WHERE target_key = ?",
        (target_key,),
    ).fetchone()
    return row_to_dict(row)


def list_sync_targets_for_space(
    connection: sqlite3.Connection,
    space_key: str,
) -> list[dict[str, Any]]:
    """Load all sync targets for a space."""

    rows = connection.execute(
        """
        SELECT * FROM sync_targets
        WHERE space_key = ?
        ORDER BY target_type ASC, target_key ASC
        """,
        (space_key,),
    ).fetchall()
    return [dict(row) for row in rows]


def upsert_page_target(connection: sqlite3.Connection, record: PageTargetRecord) -> None:
    """Insert or update a page membership row for a sync target."""

    connection.execute(
        """
        INSERT INTO page_targets (
          target_key, page_id, space_key, included, last_seen_at, metadata_json
        ) VALUES (
          :target_key, :page_id, :space_key, :included, :last_seen_at, :metadata_json
        )
        ON CONFLICT(target_key, page_id) DO UPDATE SET
          space_key = excluded.space_key,
          included = excluded.included,
          last_seen_at = excluded.last_seen_at,
          metadata_json = excluded.metadata_json
        """,
        asdict(record),
    )
    connection.commit()


def list_page_targets_for_target(
    connection: sqlite3.Connection,
    target_key: str,
    *,
    included_only: bool = False,
) -> list[dict[str, Any]]:
    """Load page membership rows for a sync target."""

    where_clause = "WHERE target_key = ?"
    parameters: list[Any] = [target_key]
    if included_only:
        where_clause += " AND included = 1"
    rows = connection.execute(
        f"""
        SELECT * FROM page_targets
        {where_clause}
        ORDER BY page_id ASC
        """,
        parameters,
    ).fetchall()
    return [dict(row) for row in rows]


def replace_page_targets_for_target(
    connection: sqlite3.Connection,
    target_key: str,
    records: list[PageTargetRecord],
) -> None:
    """Replace all page memberships for a target."""

    connection.execute("DELETE FROM page_targets WHERE target_key = ?", (target_key,))
    for record in records:
        connection.execute(
            """
            INSERT INTO page_targets (
              target_key, page_id, space_key, included, last_seen_at, metadata_json
            ) VALUES (
              :target_key, :page_id, :space_key, :included, :last_seen_at, :metadata_json
            )
            """,
            asdict(record),
        )
    connection.commit()


def upsert_page(connection: sqlite3.Connection, record: PageRecord) -> None:
    """Insert or update a page row."""

    connection.execute(
        """
        INSERT INTO pages (
          page_id, space_key, space_id, title, status, parent_id, author_id, owner_id,
          created_at, version_number, version_created_at, version_message,
          version_minor_edit, version_author_id, source_url, webui_path, local_path,
          raw_json_path, raw_storage_path, labels_json, content_hash, fetched_at,
          last_seen_at, deleted_or_missing, metadata_json
        ) VALUES (
          :page_id, :space_key, :space_id, :title, :status, :parent_id, :author_id, :owner_id,
          :created_at, :version_number, :version_created_at, :version_message,
          :version_minor_edit, :version_author_id, :source_url, :webui_path, :local_path,
          :raw_json_path, :raw_storage_path, :labels_json, :content_hash, :fetched_at,
          :last_seen_at, :deleted_or_missing, :metadata_json
        )
        ON CONFLICT(page_id) DO UPDATE SET
          space_key = excluded.space_key,
          space_id = excluded.space_id,
          title = excluded.title,
          status = excluded.status,
          parent_id = excluded.parent_id,
          author_id = excluded.author_id,
          owner_id = excluded.owner_id,
          created_at = excluded.created_at,
          version_number = excluded.version_number,
          version_created_at = excluded.version_created_at,
          version_message = excluded.version_message,
          version_minor_edit = excluded.version_minor_edit,
          version_author_id = excluded.version_author_id,
          source_url = excluded.source_url,
          webui_path = excluded.webui_path,
          local_path = excluded.local_path,
          raw_json_path = excluded.raw_json_path,
          raw_storage_path = excluded.raw_storage_path,
          labels_json = excluded.labels_json,
          content_hash = excluded.content_hash,
          fetched_at = excluded.fetched_at,
          last_seen_at = excluded.last_seen_at,
          deleted_or_missing = excluded.deleted_or_missing,
          metadata_json = excluded.metadata_json
        """,
        asdict(record),
    )
    connection.commit()


def get_page(connection: sqlite3.Connection, page_id: str) -> dict[str, Any] | None:
    """Load a single page row."""

    row = connection.execute(
        "SELECT * FROM pages WHERE page_id = ?",
        (page_id,),
    ).fetchone()
    return row_to_dict(row)


def list_pages_for_space(connection: sqlite3.Connection, space_key: str) -> list[dict[str, Any]]:
    """Load all pages for a space."""

    rows = connection.execute(
        """
        SELECT * FROM pages
        WHERE space_key = ?
        ORDER BY title ASC, page_id ASC
        """,
        (space_key,),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_page_deleted_or_missing(
    connection: sqlite3.Connection,
    page_id: str,
    deleted_or_missing: int = 1,
    last_seen_at: str | None = None,
) -> None:
    """Flag a page as deleted or missing."""

    connection.execute(
        """
        UPDATE pages
        SET deleted_or_missing = ?,
            last_seen_at = COALESCE(?, last_seen_at)
        WHERE page_id = ?
        """,
        (deleted_or_missing, last_seen_at, page_id),
    )
    connection.commit()


def resolve_target_identity(
    *,
    space_key: str,
    target_key: str | None = None,
    target_type: str | None = None,
    root_page_id: str | None = None,
) -> tuple[str, str]:
    """Resolve a compatible target key/type pair."""

    if target_key:
        resolved_type = target_type or (
            "page_tree" if target_key.startswith("page_tree:") else "space"
        )
        return target_key, resolved_type

    if target_type == "page_tree":
        if not root_page_id:
            raise ValueError("root_page_id is required for page_tree targets.")
        return build_page_tree_target_key(space_key, root_page_id), "page_tree"

    return build_space_target_key(space_key), target_type or "space"


def upsert_sync_state(connection: sqlite3.Connection, record: SyncStateRecord) -> None:
    """Insert or update the sync state for a target."""

    payload = asdict(record)
    payload["target_key"], payload["target_type"] = resolve_target_identity(
        space_key=payload["space_key"],
        target_key=payload["target_key"],
        target_type=payload["target_type"],
        root_page_id=payload["root_page_id"],
    )

    connection.execute(
        """
        INSERT INTO sync_state (
          target_key, target_type, root_page_id, space_key, space_id,
          last_successful_sync_at, last_started_at, last_completed_at, last_error, updated_at
        ) VALUES (
          :target_key, :target_type, :root_page_id, :space_key, :space_id,
          :last_successful_sync_at, :last_started_at, :last_completed_at, :last_error, :updated_at
        )
        ON CONFLICT(target_key) DO UPDATE SET
          target_type = excluded.target_type,
          root_page_id = excluded.root_page_id,
          space_id = excluded.space_id,
          space_key = excluded.space_key,
          last_successful_sync_at = excluded.last_successful_sync_at,
          last_started_at = excluded.last_started_at,
          last_completed_at = excluded.last_completed_at,
          last_error = excluded.last_error,
          updated_at = excluded.updated_at
        """,
        payload,
    )
    connection.commit()


def get_sync_state(
    connection: sqlite3.Connection,
    space_key: str,
    *,
    target_key: str | None = None,
    target_type: str | None = None,
    root_page_id: str | None = None,
) -> dict[str, Any] | None:
    """Load sync state for a target."""

    resolved_target_key, _ = resolve_target_identity(
        space_key=space_key,
        target_key=target_key,
        target_type=target_type,
        root_page_id=root_page_id,
    )

    row = connection.execute(
        "SELECT * FROM sync_state WHERE target_key = ?",
        (resolved_target_key,),
    ).fetchone()
    return row_to_dict(row)


def record_sync_started(
    connection: sqlite3.Connection,
    space_key: str,
    space_id: str,
    started_at: str | None = None,
    *,
    target_key: str | None = None,
    target_type: str | None = None,
    root_page_id: str | None = None,
) -> None:
    """Update sync_state when a run starts."""

    resolved_target_key, resolved_target_type = resolve_target_identity(
        space_key=space_key,
        target_key=target_key,
        target_type=target_type,
        root_page_id=root_page_id,
    )
    current = get_sync_state(connection, space_key, target_key=resolved_target_key)
    record = SyncStateRecord(
        target_key=resolved_target_key,
        target_type=resolved_target_type,
        space_key=space_key,
        space_id=space_id,
        root_page_id=root_page_id,
        last_successful_sync_at=current["last_successful_sync_at"] if current else None,
        last_started_at=started_at or now_iso(),
        last_completed_at=current["last_completed_at"] if current else None,
        last_error=None,
        updated_at=now_iso(),
    )
    upsert_sync_state(connection, record)


def record_sync_completed(
    connection: sqlite3.Connection,
    space_key: str,
    space_id: str,
    completed_at: str | None = None,
    success_at: str | None = None,
    last_error: str | None = None,
    *,
    target_key: str | None = None,
    target_type: str | None = None,
    root_page_id: str | None = None,
) -> None:
    """Update sync_state when a run completes."""

    resolved_target_key, resolved_target_type = resolve_target_identity(
        space_key=space_key,
        target_key=target_key,
        target_type=target_type,
        root_page_id=root_page_id,
    )
    current = get_sync_state(connection, space_key, target_key=resolved_target_key)
    record = SyncStateRecord(
        target_key=resolved_target_key,
        target_type=resolved_target_type,
        space_key=space_key,
        space_id=space_id,
        root_page_id=root_page_id,
        last_successful_sync_at=success_at or (
            current["last_successful_sync_at"] if current else None
        ),
        last_started_at=current["last_started_at"] if current else None,
        last_completed_at=completed_at or now_iso(),
        last_error=last_error,
        updated_at=now_iso(),
    )
    upsert_sync_state(connection, record)


def create_sync_run(connection: sqlite3.Connection, record: SyncRunRecord) -> None:
    """Insert a new sync run row."""

    payload = asdict(record)
    payload["target_key"], payload["target_type"] = resolve_target_identity(
        space_key=payload["space_key"],
        target_key=payload["target_key"],
        target_type=payload["target_type"],
        root_page_id=payload["root_page_id"],
    )

    connection.execute(
        """
        INSERT INTO sync_runs (
          run_id, target_key, target_type, root_page_id, space_key, mode, started_at,
          completed_at, fetched_pages, updated_pages, skipped_pages, failed_pages,
          status, error_message
        ) VALUES (
          :run_id, :target_key, :target_type, :root_page_id, :space_key, :mode, :started_at,
          :completed_at, :fetched_pages, :updated_pages, :skipped_pages, :failed_pages,
          :status, :error_message
        )
        """,
        payload,
    )
    connection.commit()


def get_sync_run(connection: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """Load a sync run row."""

    row = connection.execute(
        "SELECT * FROM sync_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return row_to_dict(row)


def increment_sync_run_counter(
    connection: sqlite3.Connection,
    run_id: str,
    column_name: str,
    amount: int = 1,
) -> None:
    """Increment a counter column on sync_runs."""

    if column_name not in {"fetched_pages", "updated_pages", "skipped_pages", "failed_pages"}:
        raise ValueError(f"Unsupported sync run counter: {column_name}")

    connection.execute(
        f"UPDATE sync_runs SET {column_name} = {column_name} + ? WHERE run_id = ?",
        (amount, run_id),
    )
    connection.commit()


def complete_sync_run(
    connection: sqlite3.Connection,
    run_id: str,
    status: str,
    completed_at: str | None = None,
    error_message: str | None = None,
) -> None:
    """Mark a sync run as completed."""

    connection.execute(
        """
        UPDATE sync_runs
        SET status = ?,
            completed_at = ?,
            error_message = ?
        WHERE run_id = ?
        """,
        (status, completed_at or now_iso(), error_message, run_id),
    )
    connection.commit()


def has_failed_pages(connection: sqlite3.Connection, run_id: str) -> bool:
    """Return whether a sync run has any failed pages."""

    row = connection.execute(
        "SELECT failed_pages FROM sync_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return bool(row and row["failed_pages"] > 0)


def record_sync_error(connection: sqlite3.Connection, record: SyncErrorRecord) -> None:
    """Insert a sync error row."""

    payload = asdict(record)
    payload["target_key"], payload["target_type"] = resolve_target_identity(
        space_key=payload["space_key"],
        target_key=payload["target_key"],
        target_type=payload["target_type"],
        root_page_id=payload["root_page_id"],
    )

    connection.execute(
        """
        INSERT INTO sync_errors (
          run_id, target_key, target_type, root_page_id, space_key, page_id,
          operation, error_type, error_message, created_at
        ) VALUES (
          :run_id, :target_key, :target_type, :root_page_id, :space_key, :page_id,
          :operation, :error_type, :error_message, :created_at
        )
        """,
        payload,
    )
    connection.commit()


def list_sync_errors(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """Load sync errors for a run."""

    rows = connection.execute(
        """
        SELECT * FROM sync_errors
        WHERE run_id = ?
        ORDER BY id ASC
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def upsert_document(connection: sqlite3.Connection, record: DocumentRecord) -> None:
    """Insert or update a document row."""

    connection.execute(
        """
        INSERT INTO documents (
          doc_id, source, space_key, space_id, page_id, path, title, url, status,
          parent_id, version_number, version_created_at, fetched_at, labels_json,
          content_hash, metadata_json
        ) VALUES (
          :doc_id, :source, :space_key, :space_id, :page_id, :path, :title, :url, :status,
          :parent_id, :version_number, :version_created_at, :fetched_at, :labels_json,
          :content_hash, :metadata_json
        )
        ON CONFLICT(doc_id) DO UPDATE SET
          source = excluded.source,
          space_key = excluded.space_key,
          space_id = excluded.space_id,
          page_id = excluded.page_id,
          path = excluded.path,
          title = excluded.title,
          url = excluded.url,
          status = excluded.status,
          parent_id = excluded.parent_id,
          version_number = excluded.version_number,
          version_created_at = excluded.version_created_at,
          fetched_at = excluded.fetched_at,
          labels_json = excluded.labels_json,
          content_hash = excluded.content_hash,
          metadata_json = excluded.metadata_json
        """,
        asdict(record),
    )
    connection.commit()


def get_document(connection: sqlite3.Connection, doc_id: str) -> dict[str, Any] | None:
    """Load a document row."""

    row = connection.execute(
        "SELECT * FROM documents WHERE doc_id = ?",
        (doc_id,),
    ).fetchone()
    return row_to_dict(row)


def clear_documents_for_space(connection: sqlite3.Connection, space_key: str) -> None:
    """Delete documents and chunks for a space before rebuilding its index."""

    chunk_rows = connection.execute(
        "SELECT chunk_id FROM chunks WHERE space_key = ?",
        (space_key,),
    ).fetchall()
    if chunk_rows:
        connection.executemany(
            "DELETE FROM chunks_fts WHERE chunk_id = ?",
            [(row["chunk_id"],) for row in chunk_rows],
        )

    connection.execute("DELETE FROM chunks WHERE space_key = ?", (space_key,))
    connection.execute("DELETE FROM documents WHERE space_key = ?", (space_key,))
    connection.commit()


def replace_chunks_for_document(
    connection: sqlite3.Connection,
    doc_id: str,
    chunks: list[ChunkRecord],
) -> None:
    """Replace all chunks for a document and refresh FTS rows."""

    existing_rows = connection.execute(
        "SELECT chunk_id FROM chunks WHERE doc_id = ?",
        (doc_id,),
    ).fetchall()
    if existing_rows:
        connection.executemany(
            "DELETE FROM chunks_fts WHERE chunk_id = ?",
            [(row["chunk_id"],) for row in existing_rows],
        )

    connection.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    for chunk in chunks:
        payload = asdict(chunk)
        connection.execute(
            """
            INSERT INTO chunks (
              chunk_id, doc_id, page_id, space_key, path, title, headings, body,
              start_line, end_line, chunk_index, token_count, labels_json, metadata_json
            ) VALUES (
              :chunk_id, :doc_id, :page_id, :space_key, :path, :title, :headings, :body,
              :start_line, :end_line, :chunk_index, :token_count, :labels_json, :metadata_json
            )
            """,
            payload,
        )
        connection.execute(
            """
            INSERT INTO chunks_fts (chunk_id, title, headings, body)
            VALUES (:chunk_id, :title, :headings, :body)
            """,
            payload,
        )

    connection.commit()


def list_chunks_for_document(connection: sqlite3.Connection, doc_id: str) -> list[dict[str, Any]]:
    """Load chunks for a document."""

    rows = connection.execute(
        """
        SELECT * FROM chunks
        WHERE doc_id = ?
        ORDER BY chunk_index ASC
        """,
        (doc_id,),
    ).fetchall()
    return [dict(row) for row in rows]
