# Operations Guide

このドキュメントでは、Confluence のローカル同期とインデックス更新を定期運用するための手順をまとめます。

## 前提

- リポジトリルートで `uv sync` が完了していること
- `.env` に最低限次の値が設定されていること
  - `CONFLUENCE_BASE_URL`
  - `CONFLUENCE_BEARER_TOKEN`
  - `CONFLUENCE_DEFAULT_SPACE`
- 複数 target を定期更新したい場合は `confluence_targets.yaml` を用意できる
- 初回同期前に、対象 space に対する API 権限があること

## 初回セットアップ

```bash
uv sync
cp .env.example .env
```

`.env` を編集したら、まず 1 回だけ full sync を実行してください。

```bash
uv run python tools/sync_confluence.py full --space PROJECT_A --reindex
```

page tree を対象にする場合:

```bash
uv run python tools/sync_confluence.py full --space PROJECT_B --root-page-id 123456
```

初回同期で確認するもの:

- `docs/confluence/PROJECT_A/pages/` に Markdown が作られている
- `docs/confluence/PROJECT_A/manifest.jsonl` が作られている
- `docs/confluence/PROJECT_A/index.md` が作られている
- `.local-doc-index/docs.db` が作られている

## 日次・定期運用

定期更新では、差分同期と再インデックスをまとめて実行します。

```bash
scripts/run_incremental_sync.sh space:PROJECT_A
```

第1引数を省略した場合は、`confluence_targets.yaml` があればそれを使い、なければ `.env` の `CONFLUENCE_DEFAULT_SPACE` を使います。

```bash
scripts/run_incremental_sync.sh
```

複数 target を自動更新したい場合は、リポジトリルートに `confluence_targets.yaml` を置きます。

```yaml
targets:
  - type: space
    space_key: PROJECT_A
  - type: page_tree
    space_key: PROJECT_B
    root_page_id: "123456"
    name: 認証関連ページ
```

サンプルは [confluence_targets.example.yaml](/Users/takeshi/ghq/github.com/carbuncle123/local-confluence-indexer/confluence_targets.example.yaml) にあります。

この状態で引数なし実行すると、指定したすべての target を順番に処理します。

```bash
scripts/run_incremental_sync.sh
```

このスクリプトは target ごとの同期自体は `--reindex` なしで実行し、最後に成功した space ごとに 1 回だけ `build_doc_index.py --space ...` を実行します。したがって、同じ space に属する page tree target が複数あっても、再インデックスは原則 1 回にまとまります。

コマンド引数を渡した場合は、引数の target 一覧を優先します。

```bash
scripts/run_incremental_sync.sh space:PROJECT_A page_tree:PROJECT_B:123456
```

## cron 例

毎日 8:30 に差分同期を実行する例です。

```cron
30 8 * * * cd /path/to/local-confluence-indexer && /bin/bash scripts/run_incremental_sync.sh >> .local-confluence-sync/sync.log 2>> .local-confluence-sync/sync.err.log
```

ポイント:

- 必ずリポジトリルートへ `cd` してから実行する
- 標準出力と標準エラーを別ログへ分ける
- 最初の数回は手動実行で成功することを確認してから cron に載せる

## macOS launchd 例

`~/Library/LaunchAgents/local.confluence.sync.project_a.plist` の例です。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>local.confluence.sync.project_a</string>

    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>/path/to/local-confluence-indexer/scripts/run_incremental_sync.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/path/to/local-confluence-indexer</string>

    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key>
      <integer>8</integer>
      <key>Minute</key>
      <integer>30</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>/path/to/local-confluence-indexer/.local-confluence-sync/sync.log</string>

    <key>StandardErrorPath</key>
    <string>/path/to/local-confluence-indexer/.local-confluence-sync/sync.err.log</string>
  </dict>
</plist>
```

読み込み例:

```bash
launchctl unload ~/Library/LaunchAgents/local.confluence.sync.project_a.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/local.confluence.sync.project_a.plist
```

## ログ確認

ログファイル:

- `.local-confluence-sync/sync.log`
- `.local-confluence-sync/sync.err.log`

最近の実行状況を確認する例:

```bash
tail -n 100 .local-confluence-sync/sync.log
tail -n 100 .local-confluence-sync/sync.err.log
```

## 手動復旧の基本手順

1. まず同じコマンドを手動で再実行する

```bash
scripts/run_incremental_sync.sh space:PROJECT_A
```

2. 認証エラーなら `.env` の `CONFLUENCE_BASE_URL` と `CONFLUENCE_BEARER_TOKEN` を見直す
3. 特定の target だけ落ちる場合は、その target を引数指定して単体実行する

```bash
scripts/run_incremental_sync.sh space:PROJECT_A
```

page tree の場合:

```bash
scripts/run_incremental_sync.sh page_tree:PROJECT_B:123456
```

4. 変換や index 周りだけ怪しい場合は、再インデックスだけを手動実行する

```bash
uv run python tools/build_doc_index.py --space PROJECT_A
```

5. 増分同期の状態が怪しい場合は、明示的に full sync をやり直す

```bash
uv run python tools/sync_confluence.py full --space PROJECT_A --reindex
```

page tree の場合:

```bash
uv run python tools/sync_confluence.py full --space PROJECT_B --root-page-id 123456
```

## ベクトル / ハイブリッド検索

FAISS によるベクトル検索を併用する場合の運用手順です。

```bash
# 依存追加 (初回のみ)
uv sync --extra vector

# .env で DOC_VECTOR_BACKEND=faiss を有効化するか、CLI で個別指定
uv run python tools/build_doc_index.py --space PROJECT_A --vector-backend faiss
```

検索:

```bash
uv run python tools/search_docs.py "ログイン状態を延長する仕組み" --space PROJECT_A --mode hybrid
```

注意点:

- ベクトル再構築は space 単位 (または `--all`) です。差分更新には現状未対応で、`--vector-backend faiss` 付きの再ビルドで全面再構築されます
- `scripts/run_incremental_sync.sh` の reindex は FTS のみを再構築します。ベクトルも更新したい場合は別途 `uv run python tools/build_doc_index.py --space ... --vector-backend faiss` を実行してください
- embedding model を変更した場合は必ず再構築してください (`vector_meta.json` の `embedding_model` と一致しないとエラーになります)
- `--mode vector` で FAISS index が無い場合はエラー、`--mode hybrid` の場合は警告のうえ FTS のみで結果を返します

## 運用上の注意

- `.env`、同期済み Markdown、ローカル SQLite はコミットしない
- bearer token はログやシェル履歴に出さない
- 大きい space では `--reindex` により space 単位でインデックスを再構築するため、時間がかかる場合がある
- 定期更新スクリプトでは、同じ space に属する target が複数あっても再インデックスは原則 1 回にまとめる
- 複数 target を順番に回す場合、1 つ失敗しても残りは継続し、最後に非 0 で終了する
- まずは手動実行が安定してから定期実行に移す
