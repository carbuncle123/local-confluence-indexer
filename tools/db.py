"""Database helpers for sync state and local search indexes."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from utils import DEFAULT_INDEX_DB_PATH, DEFAULT_SYNC_DB_PATH, ensure_parent_directory, now_iso


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
  space_key TEXT PRIMARY KEY,
  space_id TEXT NOT NULL,
  last_successful_sync_at TEXT,
  last_started_at TEXT,
  last_completed_at TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
  run_id TEXT PRIMARY KEY,
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
class SyncStateRecord:
    """A row in the sync_state table."""

    space_key: str
    space_id: str
    updated_at: str
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


def json_dumps(data: Any | None) -> str | None:
    """Serialize arbitrary JSON-compatible data."""

    if data is None:
        return None
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


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


def initialize_state_db(connection: sqlite3.Connection) -> None:
    """Create the sync-state schema."""

    connection.executescript(STATE_DB_SCHEMA)
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


def upsert_sync_state(connection: sqlite3.Connection, record: SyncStateRecord) -> None:
    """Insert or update the sync state for a space."""

    connection.execute(
        """
        INSERT INTO sync_state (
          space_key, space_id, last_successful_sync_at, last_started_at,
          last_completed_at, last_error, updated_at
        ) VALUES (
          :space_key, :space_id, :last_successful_sync_at, :last_started_at,
          :last_completed_at, :last_error, :updated_at
        )
        ON CONFLICT(space_key) DO UPDATE SET
          space_id = excluded.space_id,
          last_successful_sync_at = excluded.last_successful_sync_at,
          last_started_at = excluded.last_started_at,
          last_completed_at = excluded.last_completed_at,
          last_error = excluded.last_error,
          updated_at = excluded.updated_at
        """,
        asdict(record),
    )
    connection.commit()


def get_sync_state(connection: sqlite3.Connection, space_key: str) -> dict[str, Any] | None:
    """Load sync state for a space."""

    row = connection.execute(
        "SELECT * FROM sync_state WHERE space_key = ?",
        (space_key,),
    ).fetchone()
    return row_to_dict(row)


def record_sync_started(
    connection: sqlite3.Connection,
    space_key: str,
    space_id: str,
    started_at: str | None = None,
) -> None:
    """Update sync_state when a run starts."""

    current = get_sync_state(connection, space_key)
    record = SyncStateRecord(
        space_key=space_key,
        space_id=space_id,
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
) -> None:
    """Update sync_state when a run completes."""

    current = get_sync_state(connection, space_key)
    record = SyncStateRecord(
        space_key=space_key,
        space_id=space_id,
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

    connection.execute(
        """
        INSERT INTO sync_runs (
          run_id, space_key, mode, started_at, completed_at, fetched_pages,
          updated_pages, skipped_pages, failed_pages, status, error_message
        ) VALUES (
          :run_id, :space_key, :mode, :started_at, :completed_at, :fetched_pages,
          :updated_pages, :skipped_pages, :failed_pages, :status, :error_message
        )
        """,
        asdict(record),
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

    connection.execute(
        """
        INSERT INTO sync_errors (
          run_id, space_key, page_id, operation, error_type, error_message, created_at
        ) VALUES (
          :run_id, :space_key, :page_id, :operation, :error_type, :error_message, :created_at
        )
        """,
        asdict(record),
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
