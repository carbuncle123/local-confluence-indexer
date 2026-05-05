# Local Confluence Sync and Search

Confluence Cloud の指定スペースまたは特定ページ配下をローカル Markdown として同期し、SQLite FTS5 で検索できるようにするためのツール群です。

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

定期更新で target 設定ファイルを切り替えたい場合:

- `CONFLUENCE_TARGETS_FILE`

## Full sync

```bash
uv run python tools/sync_confluence.py full --space PROJECT_A --reindex
```

特定 page 配下だけを同期したい場合:

```bash
uv run python tools/sync_confluence.py full --space PROJECT_B --root-page-id 123456
```

## Incremental sync

```bash
uv run python tools/sync_confluence.py incremental --space PROJECT_A --reindex
```

特定 page 配下を差分同期したい場合:

```bash
uv run python tools/sync_confluence.py incremental --space PROJECT_B --root-page-id 123456
```

## Search

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_A --top-k 10
```

特定 page 配下の target に絞って検索したい場合:

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_B --root-page-id 123456 --top-k 10
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
scripts/run_incremental_sync.sh space:PROJECT_A
```

cron や launchd では、このスクリプトをリポジトリルートで実行してください。
詳しい運用手順は [docs/operations.md](/Users/takeshi/ghq/github.com/carbuncle123/local-confluence-indexer/docs/operations.md) を参照してください。

複数 target を定期更新したい場合は、[confluence_targets.example.yaml](/Users/takeshi/ghq/github.com/carbuncle123/local-confluence-indexer/confluence_targets.example.yaml) を参考に `confluence_targets.yaml` を作成してください。

```yaml
targets:
  - type: space
    space_key: PROJECT_A
  - type: page_tree
    space_key: PROJECT_B
    root_page_id: "123456"
```

この状態で引数なし実行すると、設定した target を順番に処理します。

```bash
scripts/run_incremental_sync.sh
```

このスクリプトは target ごとの同期自体は `--reindex` なしで流し、最後に成功した space ごとに 1 回だけ再インデックスします。

Codex CLI からの使い方は [docs/codex-cli-usage.md](/Users/takeshi/ghq/github.com/carbuncle123/local-confluence-indexer/docs/codex-cli-usage.md) を参照してください。

## Security

`.env`、`docs/confluence/`、`.local-confluence-sync/`、`.local-doc-index/` はコミットしないでください。
