"""Shared helpers used across sync, indexing, and search scripts."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PHASE1_NOT_IMPLEMENTED_MESSAGE = (
    "Phase 1 では CLI とモジュールの骨格のみを提供しています。"
    " 詳細実装は後続フェーズで追加します。"
)

DEFAULT_SYNC_DIR = Path(".local-confluence-sync")
DEFAULT_SYNC_DB_PATH = DEFAULT_SYNC_DIR / "state.db"
DEFAULT_INDEX_DIR = Path(".local-doc-index")
DEFAULT_INDEX_DB_PATH = DEFAULT_INDEX_DIR / "docs.db"


def phase1_placeholder(name: str) -> str:
    """Return a consistent placeholder message for Phase 1 commands."""

    return f"{name}: {PHASE1_NOT_IMPLEMENTED_MESSAGE}"


def json_dumps(data: Any | None) -> str | None:
    """Serialize arbitrary JSON-compatible data."""

    if data is None:
        return None
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""

    return datetime.now(timezone.utc).isoformat()


def ensure_directory(path: Path) -> Path:
    """Create a directory if it does not exist and return the path."""

    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_parent_directory(path: Path) -> Path:
    """Create the parent directory for a file path and return the file path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> Path:
    """Atomically write a text file via a temporary sibling path."""

    ensure_parent_directory(path)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(content, encoding=encoding)
    os.replace(temp_path, path)
    return path
