#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

run_for_space() {
  local space_key="$1"
  echo "[sync] start ${space_key}"
  uv run python tools/sync_confluence.py incremental --space "${space_key}" --reindex
  echo "[sync] done ${space_key}"
}

resolve_spaces() {
  local -a spaces=()

  if [[ "$#" -gt 0 ]]; then
    spaces=("$@")
  elif [[ -n "${CONFLUENCE_SPACES:-}" ]]; then
    IFS=',' read -r -a spaces <<< "${CONFLUENCE_SPACES}"
  elif [[ -n "${CONFLUENCE_DEFAULT_SPACE:-}" ]]; then
    spaces=("${CONFLUENCE_DEFAULT_SPACE}")
  else
    echo "SPACE_KEY が未指定です。引数、CONFLUENCE_SPACES、または CONFLUENCE_DEFAULT_SPACE を設定してください。" >&2
    exit 1
  fi

  for i in "${!spaces[@]}"; do
    spaces[$i]="$(echo "${spaces[$i]}" | xargs)"
  done

  printf '%s\n' "${spaces[@]}"
}

mkdir -p .local-confluence-sync

mapfile -t SPACE_KEYS < <(resolve_spaces "$@")

failed=0
for space_key in "${SPACE_KEYS[@]}"; do
  if [[ -z "${space_key}" ]]; then
    continue
  fi
  if ! run_for_space "${space_key}"; then
    echo "[sync] failed ${space_key}" >&2
    failed=1
  fi
done

exit "${failed}"
