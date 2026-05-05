from __future__ import annotations

from pathlib import Path

import pytest

from target_config import load_targets_file, parse_target_spec_string, sync_command_args


def test_parse_target_spec_string_for_space() -> None:
    target = parse_target_spec_string("space:PROJECT_A")

    assert target.target_type == "space"
    assert target.space_key == "PROJECT_A"
    assert target.root_page_id is None


def test_parse_target_spec_string_for_page_tree() -> None:
    target = parse_target_spec_string("page_tree:PROJECT_B:123456")

    assert target.target_type == "page_tree"
    assert target.space_key == "PROJECT_B"
    assert target.root_page_id == "123456"


def test_load_targets_file(tmp_path: Path) -> None:
    config_path = tmp_path / "targets.yaml"
    config_path.write_text(
        """
targets:
  - type: space
    space_key: PROJECT_A
  - type: page_tree
    space_key: PROJECT_B
    root_page_id: "123456"
    name: 認証
""".strip()
        + "\n",
        encoding="utf-8",
    )

    targets = load_targets_file(config_path)

    assert [target.target_type for target in targets] == ["space", "page_tree"]
    assert sync_command_args(targets[0]) == ["incremental", "--space", "PROJECT_A", "--reindex"]
    assert sync_command_args(targets[1]) == [
        "incremental",
        "--space",
        "PROJECT_B",
        "--root-page-id",
        "123456",
        "--reindex",
    ]
    assert sync_command_args(targets[1], reindex=False) == [
        "incremental",
        "--space",
        "PROJECT_B",
        "--root-page-id",
        "123456",
    ]


def test_load_targets_file_requires_root_page_id_for_page_tree(tmp_path: Path) -> None:
    config_path = tmp_path / "targets.yaml"
    config_path.write_text(
        """
targets:
  - type: page_tree
    space_key: PROJECT_B
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_targets_file(config_path)
