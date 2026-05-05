#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

SPACE_KEY="${1:-${CONFLUENCE_DEFAULT_SPACE:-}}"

if [[ -z "${SPACE_KEY}" ]]; then
  echo "SPACE_KEY が未指定です。第1引数または CONFLUENCE_DEFAULT_SPACE を設定してください。" >&2
  exit 1
fi

mkdir -p .local-confluence-sync

exec uv run python tools/sync_confluence.py incremental --space "${SPACE_KEY}" --reindex
