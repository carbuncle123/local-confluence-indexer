# Local Confluence Sync and Search

Confluence Cloud の指定スペースをローカル Markdown として同期し、SQLite FTS5 で検索できるようにするためのツール群です。

## Setup

```bash
uv sync
cp .env.example .env
```

`.env` を編集して Confluence 接続情報を設定してください。
認証は `Authorization: Bearer ...` を使う前提です。

最低限必要な設定値:

- `CONFLUENCE_BASE_URL`
- `CONFLUENCE_BEARER_TOKEN`
- `CONFLUENCE_DEFAULT_SPACE`

## Full sync

```bash
uv run python tools/sync_confluence.py full --space PROJECT_A --reindex
```

## Incremental sync

```bash
uv run python tools/sync_confluence.py incremental --space PROJECT_A --reindex
```

## Search

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_A --top-k 10
```

パスだけ見たい場合:

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_A --path-only
```

macOS で先頭結果を開く場合:

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_A --open
```

## Test

```bash
uv run pytest
```

## Scheduled Run

差分同期と再インデックスの定期実行例:

```bash
scripts/run_incremental_sync.sh PROJECT_A
```

cron や launchd では、このスクリプトをリポジトリルートで実行してください。
詳しい運用手順は [docs/operations.md](/Users/takeshi/ghq/github.com/carbuncle123/local-confluence-indexer/docs/operations.md) を参照してください。

## Security

`.env`、`docs/confluence/`、`.local-confluence-sync/`、`.local-doc-index/` はコミットしないでください。
