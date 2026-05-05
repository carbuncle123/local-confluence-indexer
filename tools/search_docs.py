"""Search the local SQLite FTS5 index."""

from __future__ import annotations

import argparse

from utils import phase1_placeholder


def build_parser() -> argparse.ArgumentParser:
    """Build the Phase 1 search CLI parser."""

    parser = argparse.ArgumentParser(
        description="ローカル検索インデックスを検索します。"
    )
    parser.add_argument("query", help="検索クエリ")
    parser.add_argument("--space", help="対象の Confluence space key")
    parser.add_argument("--top-k", type=int, default=10, help="返却件数")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--include-draft", action="store_true")
    parser.add_argument("--path-only", action="store_true")
    return parser


def main() -> int:
    """Run the Phase 1 placeholder CLI."""

    parser = build_parser()
    parser.parse_args()
    print(phase1_placeholder("search_docs"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
