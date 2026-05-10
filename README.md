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

`--mode` で検索方式を切り替えられます (既定は `fts`)。

```bash
# キーワード検索のみ
uv run python tools/search_docs.py "認証API" --space PROJECT_A --mode fts

# 意味検索のみ (FAISS index 必須)
uv run python tools/search_docs.py "ログイン状態を延長する仕組み" --space PROJECT_A --mode vector

# キーワード + 意味検索のハイブリッド (RRF でランキング統合)
uv run python tools/search_docs.py "ログイン状態を延長する仕組み" --space PROJECT_A --mode hybrid --explain
```

ベクトル検索を使うには、まず後述の `--vector-backend faiss` 付きでインデックスを構築してください。

## Vector / Hybrid Search

FAISS によるベクトル検索を有効化する手順です。

```bash
# 追加依存をインストール (sentence-transformers / numpy / faiss-cpu)
uv sync --extra vector

# .env で DOC_VECTOR_BACKEND=faiss を設定するか、CLI で都度指定する
uv run python tools/build_doc_index.py --space PROJECT_A --vector-backend faiss
```

成果物:

- `.local-doc-index/faiss.index`
- `.local-doc-index/vector_meta.json`
- `docs.db` の `vector_chunks` テーブル

embedding model を変更した場合や FAISS index と vector_chunks の件数が一致しない場合は、`--vector-backend faiss` 付きで再構築してください。

特定 page 配下の target に絞って検索したい場合:

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_B --root-page-id 123456 --top-k 10
```

Markdown 出力では、同一ページ内の複数 hit をページ単位でまとめ、抜粋中の一致語を `[[...]]` で強調表示します。

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
