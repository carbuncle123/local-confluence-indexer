"""Build a local SQLite FTS5 index from synced Markdown files."""

from __future__ import annotations

import argparse

from utils import phase1_placeholder


def build_parser() -> argparse.ArgumentParser:
    """Build the Phase 1 index CLI parser."""

    parser = argparse.ArgumentParser(
        description="同期済み Markdown からローカル検索インデックスを構築します。"
    )
    parser.add_argument("--space", help="対象の Confluence space key")
    parser.add_argument("--all", action="store_true", help="全 space を対象に再構築する")
    return parser


def main() -> int:
    """Run the Phase 1 placeholder CLI."""

    parser = build_parser()
    parser.parse_args()
    print(phase1_placeholder("build_doc_index"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
