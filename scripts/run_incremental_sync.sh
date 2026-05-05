#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

run_for_target() {
  local target_type="$1"
  local space_key="$2"
  local root_page_id="${3:-}"

  if [[ "${target_type}" == "page_tree" ]]; then
    echo "[sync] start page_tree:${space_key}:${root_page_id}"
    uv run python tools/sync_confluence.py incremental --space "${space_key}" --root-page-id "${root_page_id}"
    echo "[sync] done page_tree:${space_key}:${root_page_id}"
  else
    echo "[sync] start space:${space_key}"
    uv run python tools/sync_confluence.py incremental --space "${space_key}"
    echo "[sync] done space:${space_key}"
  fi
}

reindex_space() {
  local space_key="$1"
  echo "[reindex] start ${space_key}"
  uv run python tools/build_doc_index.py --space "${space_key}"
  echo "[reindex] done ${space_key}"
}

resolve_targets() {
  UV_CACHE_DIR=.uv-cache uv run python tools/resolve_sync_targets.py "$@"
}

mkdir -p .local-confluence-sync

mapfile -t TARGET_ROWS < <(resolve_targets "$@")

failed=0
declare -A SUCCEEDED_SPACES=()
for row in "${TARGET_ROWS[@]}"; do
  if [[ -z "${row}" ]]; then
    continue
  fi
  IFS=$'\t' read -r target_type space_key root_page_id <<< "${row}"
  if ! run_for_target "${target_type}" "${space_key}" "${root_page_id}"; then
    echo "[sync] failed ${target_type}:${space_key}:${root_page_id}" >&2
    failed=1
  else
    SUCCEEDED_SPACES["${space_key}"]=1
  fi
done

for space_key in "${!SUCCEEDED_SPACES[@]}"; do
  if ! reindex_space "${space_key}"; then
    echo "[reindex] failed ${space_key}" >&2
    failed=1
  fi
done

exit "${failed}"
