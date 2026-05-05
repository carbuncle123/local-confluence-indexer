"""Resolve scheduled sync targets as TSV rows."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from target_config import DEFAULT_TARGETS_FILE, load_targets_file, parse_target_spec_string


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="定期更新の sync target 一覧を解決します。")
    parser.add_argument("targets", nargs="*", help="space:PROJECT_A や page_tree:PROJECT_A:123456")
    parser.add_argument("--config", help="target 設定 YAML のパス")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config or os.getenv("CONFLUENCE_TARGETS_FILE", DEFAULT_TARGETS_FILE))

    if args.targets:
        targets = [parse_target_spec_string(item) for item in args.targets]
    elif config_path.exists():
        targets = load_targets_file(config_path)
    else:
        default_space = os.getenv("CONFLUENCE_DEFAULT_SPACE")
        if not default_space:
            raise ValueError(
                f"target が未指定です。引数、{config_path}、または CONFLUENCE_DEFAULT_SPACE を設定してください。"
            )
        targets = [parse_target_spec_string(f"space:{default_space}")]

    for target in targets:
        print(f"{target.target_type}\t{target.space_key}\t{target.root_page_id or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
