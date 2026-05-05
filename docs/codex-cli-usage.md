# Codex CLI 利用ガイド

このドキュメントでは、同期済み Confluence ドキュメントとローカル検索インデックスを Codex CLI からどのように使うかをまとめます。

## 前提

Codex CLI からこのリポジトリを使う前に、最低限次が完了している必要があります。

1. Confluence の同期が終わっている
2. ローカル検索インデックスが構築されている

基本コマンド:

```bash
uv run python tools/sync_confluence.py incremental --space PROJECT_A --reindex
```

page tree を対象にする場合:

```bash
uv run python tools/sync_confluence.py incremental --space PROJECT_B --root-page-id 123456
```

特定 space を再インデックスだけしたい場合:

```bash
uv run python tools/build_doc_index.py --space PROJECT_A
```

## まず何を見るか

Codex CLI で社内仕様を調べるときは、いきなり本文を総当たりせず、次の順で見ると安定します。

1. `docs/confluence/{SPACE_KEY}/index.md`
2. `uv run python tools/search_docs.py "query" --space {SPACE_KEY} --top-k 10`
3. ヒットした Markdown 本文

`index.md` は、その space の入り口です。正式そうな文書や全体像を確認する用途に向いています。

page tree target を使っている場合は、次も確認対象になります。

1. `docs/confluence/{SPACE_KEY}/targets/page-tree--{ROOT_PAGE_ID}/index.md`
2. `docs/confluence/{SPACE_KEY}/targets/page-tree--{ROOT_PAGE_ID}/manifest.jsonl`

## 基本的な使い方

### 1. 正式な仕様候補を探す

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_A --top-k 10
```

page tree target に絞る場合:

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_B --root-page-id 123456 --top-k 10
```

このコマンドは、次を含む検索結果を返します。

- path
- page 単位のまとまり
- chunk ごとの line range
- URL
- version
- fetched_at
- labels

Codex CLI から回答するときは、これらのメタデータを根拠として扱います。

### 2. パスだけ確認したい

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_A --path-only
```

候補ファイルだけ素早く見たいときに使います。

### 3. JSON で扱いたい

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_A --json
```

検索結果を別ツールやスクリプトへ渡したいときに向いています。

### 4. 先頭結果を開きたい

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_A --open
```

macOS の `open` コマンドで、先頭ヒットの Markdown を開きます。

## Codex CLI での質問の仕方

### 良い例

- 「PROJECT_A の認証APIで refresh token rotation について書かれている箇所を調べて」
- 「PROJECT_B の正式な仕様書を優先して、Webhook 認証方式を要約して」
- 「まず search_docs で調べて、根拠の path と line range を付けて答えて」

### より安定する依頼

- 対象 space を明示する
- 必要なら root page id も明示する
- キーワードを 2 から 4 個程度に絞る
- `official/current/approved` を優先したい意図を伝える
- 回答時に `path`、`line_range`、`version_number`、`fetched_at` を出すように依頼する

## 回答時の扱い

Codex CLI で検索結果を根拠に回答する場合は、次を意識します。

- まず `official/current/approved` ラベル付き文書を優先する
- `draft/wip/deprecated/old` 系は低信頼として扱う
- `fetched_at` が古い場合は、ローカルスナップショットが古い可能性を明記する
- 複数候補がある場合は、差分や競合も合わせて説明する

## 実務向けの流れ

### 仕様確認だけしたい場合

```bash
uv run python tools/search_docs.py "認証API refresh token" --space PROJECT_A --top-k 5
```

### 変更前に関連文書を洗いたい場合

```bash
uv run python tools/search_docs.py "Webhook 認証" --space PROJECT_A --top-k 10
uv run python tools/search_docs.py "Webhook 認証" --space PROJECT_B --top-k 10
```

### page tree 単位で確認したい場合

まず target 配下の `index.md` と `manifest.jsonl` で対象ページ集合を確認します。

```bash
sed -n '1,120p' docs/confluence/PROJECT_B/targets/page-tree--123456/index.md
```

そのうえで通常の `search_docs.py` を使い、必要なら対象 path を絞って読みます。

検索結果自体を page tree target に絞りたい場合は `--root-page-id` を付けます。

```bash
uv run python tools/search_docs.py "認証 API" --space PROJECT_B --root-page-id 123456 --top-k 10
```

### 自然文に近い質問をしたい場合

現状の検索は FTS5 ベースなので、意味検索ではなくキーワード検索です。自然文そのままより、語を少し分解した方が安定します。

例:

- そのまま: `プロジェクトAの認証について教えて`
- 推奨: `PROJECT_A 認証 API`

## 注意点

- 現状のインデックス更新は page 単位差分ではなく、space 単位再構築です
- page tree target を使っても、検索インデックス自体は現状 space 単位です
- `--root-page-id` 検索は state DB の membership を使って結果を絞っています
- Markdown 出力は同一ページ内の複数 hit を 1 つのページ結果に集約します
- 抜粋中の `[[...]]` はクエリ一致語の強調です
- 検索は `title`、`headings`、`body` を対象にしており、意味ベース検索ではありません
- まずは検索 CLI で候補を絞ってから本文を見る方が、Codex CLI の回答品質が安定します

## 関連ドキュメント

- [README.md](/Users/takeshi/ghq/github.com/carbuncle123/local-confluence-indexer/README.md)
- [operations.md](/Users/takeshi/ghq/github.com/carbuncle123/local-confluence-indexer/docs/operations.md)
- [AGENTS.md](/Users/takeshi/ghq/github.com/carbuncle123/local-confluence-indexer/AGENTS.md)
