"""CLI for syncing Confluence pages to local Markdown."""

from __future__ import annotations

import argparse

from utils import phase1_placeholder


def build_parser() -> argparse.ArgumentParser:
    """Build the Phase 1 CLI parser."""

    parser = argparse.ArgumentParser(
        description="Confluence ページをローカル Markdown に同期します。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("full", "incremental", "page"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--space", required=True, help="Confluence space key")
        subparser.add_argument("--reindex", action="store_true")
        subparser.add_argument("--dry-run", action="store_true")
        subparser.add_argument("--force", action="store_true")
        if command == "page":
            subparser.add_argument("--page-id", required=True, help="Confluence page id")

    return parser


def main() -> int:
    """Run the Phase 1 placeholder CLI."""

    parser = build_parser()
    args = parser.parse_args()
    print(phase1_placeholder(f"sync_confluence:{args.command}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
