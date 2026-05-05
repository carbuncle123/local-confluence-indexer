"""Shared helpers used across sync, indexing, and search scripts."""

from __future__ import annotations


PHASE1_NOT_IMPLEMENTED_MESSAGE = (
    "Phase 1 では CLI とモジュールの骨格のみを提供しています。"
    " 詳細実装は後続フェーズで追加します。"
)


def phase1_placeholder(name: str) -> str:
    """Return a consistent placeholder message for Phase 1 commands."""

    return f"{name}: {PHASE1_NOT_IMPLEMENTED_MESSAGE}"
