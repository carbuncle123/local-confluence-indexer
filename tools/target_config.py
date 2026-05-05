"""Helpers for loading sync targets from config files or CLI strings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


DEFAULT_TARGETS_FILE = "confluence_targets.yaml"


@dataclass(slots=True)
class SyncTargetSpec:
    """A sync target definition for scheduled runs."""

    target_type: str
    space_key: str
    root_page_id: str | None = None
    name: str | None = None


def parse_target_spec_string(value: str) -> SyncTargetSpec:
    """Parse a compact target string."""

    parts = [part.strip() for part in value.split(":")]
    if len(parts) == 2 and parts[0] == "space":
        return SyncTargetSpec(target_type="space", space_key=parts[1])
    if len(parts) == 3 and parts[0] == "page_tree":
        return SyncTargetSpec(target_type="page_tree", space_key=parts[1], root_page_id=parts[2])
    raise ValueError(
        "Target must look like 'space:PROJECT_A' or 'page_tree:PROJECT_A:123456'."
    )


def load_targets_file(path: Path) -> list[SyncTargetSpec]:
    """Load sync targets from a YAML file."""

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = payload.get("targets")
    if not isinstance(items, list) or not items:
        raise ValueError("targets must be a non-empty list.")

    results: list[SyncTargetSpec] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each target must be an object.")
        target_type = str(item.get("type", "")).strip()
        space_key = str(item.get("space_key", "")).strip()
        root_page_id = item.get("root_page_id")
        name = item.get("name")

        if target_type not in {"space", "page_tree"}:
            raise ValueError("type must be 'space' or 'page_tree'.")
        if not space_key:
            raise ValueError("space_key is required.")
        if target_type == "page_tree" and not root_page_id:
            raise ValueError("root_page_id is required for page_tree targets.")

        results.append(
            SyncTargetSpec(
                target_type=target_type,
                space_key=space_key,
                root_page_id=str(root_page_id) if root_page_id is not None else None,
                name=str(name) if name is not None else None,
            )
        )

    return results


def sync_command_args(target: SyncTargetSpec, *, reindex: bool = True) -> list[str]:
    """Build sync_confluence.py arguments for a target."""

    args = ["incremental", "--space", target.space_key]
    if target.target_type == "page_tree":
        args.extend(["--root-page-id", target.root_page_id or ""])
    if reindex:
        args.append("--reindex")
    return args
