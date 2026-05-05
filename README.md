# Local Confluence Sync and Search

Confluence Cloud の指定スペースをローカル Markdown として同期し、SQLite FTS5 で検索できるようにするためのツール群です。

## Setup

```bash
uv sync
cp .env.example .env
```

`.env` を編集して Confluence 接続情報を設定してください。

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

## Security

`.env`、`docs/confluence/`、`.local-confluence-sync/`、`.local-doc-index/` はコミットしないでください。
