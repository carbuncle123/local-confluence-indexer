# Confluence ローカル同期・Markdown化・ローカル検索インデックス構築 実装仕様書

作成日: 2026-05-05  
対象: Codex CLI による実装  
実装言語: Python 3.11+  
主目的: Confluence Cloud の指定スペースをローカル Markdown として定期同期し、Codex CLI から検索・参照できるローカルインデックスを構築する。

---

## 0. この仕様書の使い方

この文書は、Codex CLI に実装を依頼するための実装仕様書である。抽象設計ではなく、以下の実装成果物を作ることを前提とする。

- Confluence Cloud から指定スペースのページを取得する同期ツール
- 取得した Confluence ページを Markdown に変換してローカル保存する処理
- 前回同期状態を SQLite で管理し、新規作成・更新ページのみ差分取得する処理
- Markdown をローカル全文検索インデックスに登録する処理
- Codex CLI から呼び出しやすい検索 CLI
- 定期実行しやすいコマンド構成

この実装では、Confluence ページをローカルに複製する。したがって、ローカル出力先とインデックスファイルは Git にコミットしない前提とする。

---

## 1. 実装対象範囲

### 1.1 必須要件

以下を必ず実装する。

1. Confluence Cloud の指定スペースからページを取得する。
2. 初回同期では指定スペース内の `current` ページを全件取得する。
3. 2回目以降の差分同期では、新規作成・更新されたページのみ本文を再取得する。
4. ページ本文を Markdown に変換してローカル保存する。
5. Markdown にはインデックス化に必要な YAML frontmatter を付与する。
6. ローカルに同期状態を保持し、page_id / version_number / content_hash / local_path を管理する。
7. インデックス化用の `manifest.jsonl` を出力する。
8. 人間と Codex CLI が起点にできる `index.md` を出力する。
9. ローカル Markdown を SQLite FTS5 で全文検索できるようにする。
10. Codex CLI から `python tools/search_docs.py "検索語"` の形で検索できるようにする。
11. rate limit / 一時的な API エラーに対して retry/backoff する。
12. エラー時に同期状態を壊さない。
13. 成功時のみ `last_successful_sync_at` を更新する。

### 1.2 初期実装では対象外

以下は初期実装では対象外とする。ただし、後で拡張できる設計にする。

- Slack 同期
- Jira / GitHub / GitLab 同期
- ベクトル検索 / embedding / FAISS
- Confluence への書き戻し
- 添付ファイルの完全ダウンロード
- OAuth 2.0 3LO 認証
- MCP サーバー化
- Web UI
- 複数ユーザーの ACL 再現

---

## 2. 推奨ディレクトリ構成

リポジトリ直下に以下の構成を作る。

```text
repo-root/
  AGENTS.md
  .env.example
  .gitignore
  requirements.txt
  README.md

  tools/
    confluence_client.py
    sync_confluence.py
    markdown_converter.py
    build_doc_index.py
    search_docs.py
    db.py
    utils.py

  docs/
    confluence/
      {SPACE_KEY}/
        index.md
        manifest.jsonl
        pages/
          {page_id}__{slug}.md

  .local-confluence-sync/
    state.db
    sync.log
    sync.err.log
    raw/
      {SPACE_KEY}/
        {page_id}.page.json
        {page_id}.storage.html

  .local-doc-index/
    docs.db
```

### 2.1 Git 管理対象

Git 管理する。

```text
AGENTS.md
.env.example
requirements.txt
README.md
tools/*.py
```

### 2.2 Git 管理しないもの

`.gitignore` に必ず入れる。

```gitignore
docs/confluence/
.local-confluence-sync/
.local-doc-index/
.env
```

理由:

- Confluence から取得した情報には社内機密が含まれる可能性がある。
- ローカルインデックスにも本文が含まれる。
- API token を `.env` に保存するため、`.env` はコミット禁止。

---

## 3. 認証と設定

### 3.1 認証方式

初期実装では、Confluence Cloud の Basic auth を使う。

- ユーザーの Atlassian account email
- Atlassian API token

HTTP Authorization header は以下の形式にする。

```text
Authorization: Basic base64(email:api_token)
```

### 3.2 `.env.example`

```bash
# Confluence base URL. 末尾スラッシュなし。
CONFLUENCE_BASE_URL=https://your-domain.atlassian.net

# Atlassian account email
CONFLUENCE_EMAIL=you@example.com

# Atlassian API token
CONFLUENCE_API_TOKEN=replace-me

# default space key
CONFLUENCE_DEFAULT_SPACE=PROJECT_A

# local paths
CONFLUENCE_DOCS_DIR=docs/confluence
CONFLUENCE_SYNC_DIR=.local-confluence-sync
DOC_INDEX_DIR=.local-doc-index

# 差分同期時の取りこぼし防止 overlap 分
CONFLUENCE_INCREMENTAL_OVERLAP_MINUTES=30

# API request retry
CONFLUENCE_REQUEST_TIMEOUT_SECONDS=30
CONFLUENCE_MAX_RETRIES=5
```

### 3.3 設定読み込みルール

- `.env` が存在すれば読み込む。
- 環境変数が直接設定されていればそれを優先する。
- CLI 引数で渡された値は環境変数より優先する。
- API token はログに出してはいけない。

---

## 4. Confluence API 利用方針

### 4.1 Space key から space id を解決する

Confluence Cloud REST API v2 の spaces API を使う。

```http
GET /wiki/api/v2/spaces?keys={SPACE_KEY}&limit=25
```

期待する処理:

1. `results` から `key == SPACE_KEY` の space を探す。
2. 見つかった `id` を `space_id` として保存する。
3. 見つからない場合はエラー終了する。

### 4.2 スペース内ページの全件取得

初回 full sync では、指定 space id の current page を全件取得する。

推奨 endpoint:

```http
GET /wiki/api/v2/pages?space-id={SPACE_ID}&status=current&body-format=storage&limit=100
```

ただし、一覧取得のレスポンスだけでは `labels` 等が不足する場合があるため、各 page_id について詳細取得を行う。

### 4.3 ページ詳細取得

各ページについて必ず詳細取得する。

```http
GET /wiki/api/v2/pages/{PAGE_ID}?body-format=storage&include-labels=true&include-version=true
```

期待するレスポンス情報:

- id
- status
- title
- spaceId
- parentId
- authorId
- ownerId
- createdAt
- version.number
- version.createdAt
- version.message
- version.minorEdit
- version.authorId
- body.storage.value
- labels.results[].name
- _links.webui
- _links.base

### 4.4 差分同期用 CQL 検索

差分同期では、前回成功同期時刻を基準に CQL で更新ページ候補を探す。

```text
space = "{SPACE_KEY}" and type = page and lastmodified >= "{SINCE}" order by lastmodified asc
```

API:

```http
GET /wiki/rest/api/search?cql={URL_ENCODED_CQL}&limit=25
```

注意:

- CQL 検索結果は候補取得として使う。
- 本文の Markdown 化には必ず v2 page detail endpoint を使う。
- `last_successful_sync_at` から overlap 分だけ過去に戻して検索する。
- 重複候補は `page_id + version_number` で skip する。

### 4.5 ページング

ページングは、レスポンスの `_links.next` または Link header の next を使って最後まで取得する。

実装方針:

- 最初の URL は組み立てる。
- 2ページ目以降はレスポンスで返された next URL を使う。
- next URL が相対 URL の場合は `CONFLUENCE_BASE_URL` と結合する。
- cursor を自前で再エンコードしない。原則として next URL をそのまま使う。

### 4.6 rate limit / retry

以下の応答に対応する。

- HTTP 429
- HTTP 500
- HTTP 502
- HTTP 503
- HTTP 504
- ネットワークタイムアウト

処理方針:

1. 429 で `Retry-After` header がある場合、その秒数だけ待つ。
2. 503 などでも `Retry-After` がある場合は尊重する。
3. `Retry-After` がない場合は指数バックオフ + jitter を使う。
4. 最大 retry 回数を超えたら該当ページを failed として記録する。
5. failed があった同期 run では、原則 `last_successful_sync_at` を更新しない。
6. ただし一部ページだけ失敗した場合も、成功ページの Markdown と state は保存してよい。

---

## 5. ローカル同期状態 DB

### 5.1 DB ファイル

```text
.local-confluence-sync/state.db
```

### 5.2 `spaces` table

```sql
CREATE TABLE IF NOT EXISTS spaces (
  space_key TEXT PRIMARY KEY,
  space_id TEXT NOT NULL,
  name TEXT,
  homepage_id TEXT,
  last_resolved_at TEXT NOT NULL,
  metadata_json TEXT
);
```

### 5.3 `pages` table

```sql
CREATE TABLE IF NOT EXISTS pages (
  page_id TEXT PRIMARY KEY,
  space_key TEXT NOT NULL,
  space_id TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT,
  parent_id TEXT,
  author_id TEXT,
  owner_id TEXT,
  created_at TEXT,
  version_number INTEGER NOT NULL,
  version_created_at TEXT,
  version_message TEXT,
  version_minor_edit INTEGER,
  version_author_id TEXT,
  source_url TEXT,
  webui_path TEXT,
  local_path TEXT NOT NULL,
  raw_json_path TEXT,
  raw_storage_path TEXT,
  labels_json TEXT,
  content_hash TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  deleted_or_missing INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT
);
```

### 5.4 `sync_state` table

```sql
CREATE TABLE IF NOT EXISTS sync_state (
  space_key TEXT PRIMARY KEY,
  space_id TEXT NOT NULL,
  last_successful_sync_at TEXT,
  last_started_at TEXT,
  last_completed_at TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL
);
```

### 5.5 `sync_runs` table

```sql
CREATE TABLE IF NOT EXISTS sync_runs (
  run_id TEXT PRIMARY KEY,
  space_key TEXT NOT NULL,
  mode TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  fetched_pages INTEGER NOT NULL DEFAULT 0,
  updated_pages INTEGER NOT NULL DEFAULT 0,
  skipped_pages INTEGER NOT NULL DEFAULT 0,
  failed_pages INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  error_message TEXT
);
```

### 5.6 `sync_errors` table

```sql
CREATE TABLE IF NOT EXISTS sync_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  space_key TEXT NOT NULL,
  page_id TEXT,
  operation TEXT NOT NULL,
  error_type TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL
);
```

---

## 6. Markdown 出力仕様

### 6.1 ファイルパス

ページ Markdown は以下に保存する。

```text
docs/confluence/{SPACE_KEY}/pages/{page_id}__{slug}.md
```

例:

```text
docs/confluence/PROJECT_A/pages/123456__auth-api-spec.md
```

### 6.2 slug 生成ルール

`slug` は title から生成する。

- Unicode はできるだけ残す。
- `/`, `:`, `*`, `?`, `"`, `<`, `>`, `|`, `\` は `-` に置換する。
- 空白は `-` に置換する。
- 連続 `-` は1つに畳む。
- 先頭・末尾の `-` を削除する。
- 長すぎる場合は80文字程度で切る。
- 空になった場合は `untitled` にする。

ただし page_id がファイル名先頭にあるため、タイトル変更で slug が変わっても page_id で同一ページと判定できる。

### 6.3 YAML frontmatter

すべての Markdown の先頭に以下を付与する。

```yaml
---
source: confluence
site: "your-domain.atlassian.net"
space_key: "PROJECT_A"
space_id: "12345"
page_id: "123456"
title: "認証API仕様"
status: "current"
parent_id: "120000"
url: "https://your-domain.atlassian.net/wiki/spaces/PROJECT_A/pages/123456"
webui: "/spaces/PROJECT_A/pages/123456"
version_number: 18
version_created_at: "2026-05-05T09:30:00.000Z"
version_message: "Updated auth scope section"
version_minor_edit: false
version_author_id: "abc123"
author_id: "abc123"
owner_id: "abc123"
created_at: "2026-04-01T12:00:00.000Z"
fetched_at: "2026-05-05T11:00:00+09:00"
body_format: "storage"
content_hash: "sha256:..."
labels:
  - official
  - auth
  - api
converter:
  name: "confluence-storage-to-md"
  version: "0.1.0"
indexing:
  chunking_hint: "heading"
  include: true
---
```

必須項目:

- source
- space_key
- space_id
- page_id
- title
- status
- url
- version_number
- version_created_at
- fetched_at
- content_hash
- labels

### 6.4 Markdown 本文先頭

frontmatter の直後に以下の情報ブロックを入れる。

```markdown
# 認証API仕様

> Source: Confluence  
> Space: PROJECT_A  
> Page ID: 123456  
> Version: 18  
> Last updated: 2026-05-05T09:30:00.000Z  
> Fetched at: 2026-05-05T11:00:00+09:00  
> URL: https://your-domain.atlassian.net/wiki/spaces/PROJECT_A/pages/123456
```

### 6.5 HTML / storage XHTML から Markdown への変換

初期実装では以下の方針にする。

- `body.storage.value` を入力とする。
- `BeautifulSoup` で前処理する。
- `markdownify` で Markdown に変換する。
- 見出し、表、リンク、コードブロックをできるだけ壊さない。
- Confluence 独自タグは検索可能なテキストとして保存する。

### 6.6 Confluence 独自要素の扱い

#### 6.6.1 structured macro

`ac:structured-macro` は、可能なら `ac:name` を取り出して Markdown に残す。

例:

```markdown

> Confluence macro: status

```

中身が抽出できる場合は、検索可能なテキストとして残す。

#### 6.6.2 mention

Confluence mention は accountId だけでは人間が読みにくい。初期実装では以下のように保存する。

```markdown
@accountId:abc123
```

将来的に user API で displayName 解決を追加してもよい。

#### 6.6.3 internal link

内部リンクは可能な範囲で URL を維持する。

- local page_id が解決できる場合はローカル Markdown への相対リンクに変換してもよい。
- 初期実装では Confluence URL のままでもよい。

#### 6.6.4 attachments

初期実装では添付ファイル本体はダウンロードしない。

本文中には次のようなリンクとして残す。

```markdown
[attachment: file.png](https://your-domain.atlassian.net/wiki/...)
```

---

## 7. manifest.jsonl 仕様

### 7.1 出力先

```text
docs/confluence/{SPACE_KEY}/manifest.jsonl
```

### 7.2 1行1ページ

各行は JSON object とする。

```json
{"page_id":"123456","path":"docs/confluence/PROJECT_A/pages/123456__auth-api-spec.md","title":"認証API仕様","space_key":"PROJECT_A","space_id":"12345","version_number":18,"version_created_at":"2026-05-05T09:30:00.000Z","fetched_at":"2026-05-05T11:00:00+09:00","labels":["official","auth"],"status":"current","url":"https://your-domain.atlassian.net/wiki/spaces/PROJECT_A/pages/123456","content_hash":"sha256:...","include_in_index":true}
```

### 7.3 並び順

以下の順序で出力する。

1. `space_key`
2. `title`
3. `page_id`

---

## 8. index.md 仕様

### 8.1 出力先

```text
docs/confluence/{SPACE_KEY}/index.md
```

### 8.2 内容

`index.md` は人間と Codex CLI の起点になる。以下を含める。

```markdown
# Confluence Export: PROJECT_A

- Space key: PROJECT_A
- Space id: 12345
- Exported at: 2026-05-05T11:00:00+09:00
- Pages: 243
- Updated in last sync: 12
- Manifest: ./manifest.jsonl

## Usage for Codex CLI

社内仕様を調べる場合は、まずこのファイルを確認し、必要に応じて `python tools/search_docs.py "query" --space PROJECT_A` を実行すること。

## Official / Current Candidates

| Title | Path | Version | Updated | Labels |
|---|---|---:|---|---|
| 認証API仕様 | pages/123456__auth-api-spec.md | 18 | 2026-05-05 | official, auth |

## Draft / WIP / Deprecated Candidates

| Title | Path | Version | Updated | Labels |
|---|---|---:|---|---|
| 認証方式検討メモ | pages/124000__auth-draft.md | 3 | 2026-05-03 | draft |

## All Pages

| Title | Path | Version | Updated | Labels |
|---|---|---:|---|---|
```

### 8.3 分類ルール

`labels` または title に基づいて分類する。

Official / Current 候補:

- label に `official`, `current`, `approved` のいずれかを含む。

Draft / WIP / Deprecated 候補:

- label に `draft`, `wip`, `deprecated`, `old`, `archived` のいずれかを含む。
- title に `draft`, `wip`, `deprecated`, `old`, `旧`, `廃止`, `検討`, `メモ`, `コピー` を含む。

分類できないものは All Pages にのみ載せるか、通常セクションに載せる。

---

## 9. 同期 CLI 仕様

実装対象ファイル:

```text
tools/sync_confluence.py
```

### 9.1 full sync

```bash
python tools/sync_confluence.py full --space PROJECT_A
```

処理:

1. 設定を読み込む。
2. space key から space id を解決する。
3. 対象 space の current page を全件列挙する。
4. 各 page_id の詳細を取得する。
5. storage XHTML を raw 保存する。
6. page JSON を raw 保存する。
7. Markdown に変換して保存する。
8. state.db を更新する。
9. manifest.jsonl を再生成する。
10. index.md を再生成する。
11. 成功した場合のみ sync_state を更新する。

### 9.2 incremental sync

```bash
python tools/sync_confluence.py incremental --space PROJECT_A
```

処理:

1. `sync_state.last_successful_sync_at` を読む。
2. 未同期の場合は full sync を促すか、自動で full sync を行う。初期実装では自動 full sync でよい。
3. `since = last_successful_sync_at - overlap_minutes` を計算する。
4. CQL で更新ページ候補を取得する。
5. 各候補 page_id の詳細を取得する。
6. state.db の version_number と比較する。
7. version が同じなら skip する。
8. version が新しい、またはローカル未登録なら Markdown を再生成する。
9. manifest.jsonl と index.md を再生成する。
10. 成功した場合のみ sync_state を更新する。

### 9.3 single page sync

```bash
python tools/sync_confluence.py page --space PROJECT_A --page-id 123456
```

処理:

- 指定 page_id を強制的に詳細取得し、Markdown を再生成する。
- state.db を更新する。
- manifest.jsonl と index.md を再生成する。

### 9.4 reindex option

```bash
python tools/sync_confluence.py incremental --space PROJECT_A --reindex
```

処理:

- 同期完了後に `tools/build_doc_index.py --space PROJECT_A` を呼ぶ。
- 呼び出しは subprocess でも内部関数でもよい。

### 9.5 dry-run option

```bash
python tools/sync_confluence.py incremental --space PROJECT_A --dry-run
```

処理:

- 更新候補を表示する。
- Markdown 保存、state 更新、manifest 更新は行わない。

### 9.6 force option

```bash
python tools/sync_confluence.py full --space PROJECT_A --force
python tools/sync_confluence.py page --space PROJECT_A --page-id 123456 --force
```

処理:

- version が同じでも Markdown を再生成する。
- converter version を変更した場合に使用する。

---

## 10. 同期アルゴリズム詳細

### 10.1 full sync 疑似コード

```python
def full_sync(space_key: str, force: bool = False, reindex: bool = False):
    run_id = new_run_id()
    started_at = now_iso()
    create_sync_run(run_id, space_key, mode="full", started_at=started_at)

    try:
        space = client.get_space_by_key(space_key)
        upsert_space(space)

        page_summaries = client.list_pages_in_space(
            space_id=space.id,
            status="current",
            body_format="storage",
        )

        for summary in page_summaries:
            page_id = summary["id"]
            try:
                page = client.get_page_detail(
                    page_id,
                    body_format="storage",
                    include_labels=True,
                    include_version=True,
                )

                local = get_page_state(page_id)
                remote_version = page["version"]["number"]

                if local and local.version_number == remote_version and not force:
                    mark_skipped(run_id, page_id)
                    continue

                raw_paths = save_raw(space_key, page)
                markdown, content_hash = convert_page_to_markdown(page)
                local_path = write_markdown(space_key, page, markdown)
                upsert_page_state(page, local_path, raw_paths, content_hash)
                mark_updated(run_id, page_id)

            except Exception as e:
                record_sync_error(run_id, space_key, page_id, "sync_page", e)
                mark_failed(run_id, page_id)

        regenerate_manifest(space_key)
        regenerate_index_md(space_key)

        if has_failed_pages(run_id):
            complete_sync_run(run_id, status="partial_failed")
            do_not_update_last_successful_sync(space_key)
        else:
            update_last_successful_sync(space_key, started_at)
            complete_sync_run(run_id, status="success")

        if reindex:
            build_doc_index(space_key)

    except Exception as e:
        record_sync_error(run_id, space_key, None, "full_sync", e)
        complete_sync_run(run_id, status="failed", error_message=str(e))
        raise
```

### 10.2 incremental sync 疑似コード

```python
def incremental_sync(space_key: str, reindex: bool = False, dry_run: bool = False):
    run_id = new_run_id()
    started_at = now_iso()
    create_sync_run(run_id, space_key, mode="incremental", started_at=started_at)

    try:
        space = ensure_space_resolved(space_key)
        sync_state = get_sync_state(space_key)

        if not sync_state or not sync_state.last_successful_sync_at:
            if dry_run:
                print("No previous sync. full sync is required.")
                return
            return full_sync(space_key, reindex=reindex)

        since = parse_iso(sync_state.last_successful_sync_at) - overlap_delta()
        candidates = client.search_updated_pages_by_cql(space_key, since)

        if dry_run:
            print_candidates(candidates)
            complete_sync_run(run_id, status="dry_run")
            return

        seen_page_ids = set()
        for candidate in candidates:
            page_id = extract_page_id_from_search_result(candidate)
            if page_id in seen_page_ids:
                continue
            seen_page_ids.add(page_id)

            try:
                page = client.get_page_detail(
                    page_id,
                    body_format="storage",
                    include_labels=True,
                    include_version=True,
                )

                local = get_page_state(page_id)
                remote_version = page["version"]["number"]

                if local and local.version_number == remote_version:
                    mark_skipped(run_id, page_id)
                    continue

                raw_paths = save_raw(space_key, page)
                markdown, content_hash = convert_page_to_markdown(page)
                local_path = write_markdown(space_key, page, markdown)
                upsert_page_state(page, local_path, raw_paths, content_hash)
                mark_updated(run_id, page_id)

            except Exception as e:
                record_sync_error(run_id, space_key, page_id, "sync_page", e)
                mark_failed(run_id, page_id)

        regenerate_manifest(space_key)
        regenerate_index_md(space_key)

        if has_failed_pages(run_id):
            complete_sync_run(run_id, status="partial_failed")
            do_not_update_last_successful_sync(space_key)
        else:
            update_last_successful_sync(space_key, started_at)
            complete_sync_run(run_id, status="success")

        if reindex:
            build_doc_index(space_key)

    except Exception as e:
        record_sync_error(run_id, space_key, None, "incremental_sync", e)
        complete_sync_run(run_id, status="failed", error_message=str(e))
        raise
```

---

## 11. ローカル検索インデックス仕様

### 11.1 初期実装の検索方式

初期実装では SQLite FTS5 を使う。

理由:

- ローカル完結する。
- サーバー不要。
- Python 標準 sqlite3 で扱える。
- Codex CLI から呼び出しやすい。
- 日本語 Markdown には trigram tokenizer が使いやすい。

### 11.2 インデックス DB ファイル

```text
.local-doc-index/docs.db
```

### 11.3 `documents` table

```sql
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  space_key TEXT NOT NULL,
  space_id TEXT,
  page_id TEXT NOT NULL,
  path TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT,
  status TEXT,
  parent_id TEXT,
  version_number INTEGER,
  version_created_at TEXT,
  fetched_at TEXT,
  labels_json TEXT,
  content_hash TEXT,
  metadata_json TEXT
);
```

`doc_id` は `confluence:{space_key}:{page_id}` とする。

### 11.4 `chunks` table

```sql
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL,
  page_id TEXT NOT NULL,
  space_key TEXT NOT NULL,
  path TEXT NOT NULL,
  title TEXT NOT NULL,
  headings TEXT,
  body TEXT NOT NULL,
  start_line INTEGER,
  end_line INTEGER,
  chunk_index INTEGER NOT NULL,
  token_count INTEGER,
  labels_json TEXT,
  metadata_json TEXT,
  FOREIGN KEY(doc_id) REFERENCES documents(doc_id)
);
```

`chunk_id` は `confluence:{space_key}:{page_id}:{chunk_index}` とする。

### 11.5 FTS5 table

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
USING fts5(
  chunk_id UNINDEXED,
  title,
  headings,
  body,
  tokenize = 'trigram'
);
```

### 11.6 chunking 仕様

Markdown を heading-aware に分割する。

基本ルール:

1. YAML frontmatter は chunk 本文に含めない。ただし metadata として documents に保存する。
2. Markdown の見出し `#`, `##`, `###`, ... を解析する。
3. 見出し階層を `headings` として持つ。
4. 原則として見出しセクション単位で chunk を作る。
5. 1 chunk が長すぎる場合は段落単位で分割する。
6. コードブロック内では分割しない。
7. 表は可能な限り同じ chunk に保持する。

初期値:

```text
max_chunk_chars = 3000
chunk_overlap_chars = 300
```

将来的に token 数ベースへ変更してもよい。

### 11.7 検索スコア

初期実装では FTS5 の `bm25()` を利用する。

注意:

- SQLite FTS5 の bm25 は小さい値ほど関連度が高い。
- CLI 出力では人間向けに `score = -bm25` のように反転してもよい。

検索 SQL 例:

```sql
SELECT
  c.chunk_id,
  c.path,
  c.title,
  c.headings,
  c.start_line,
  c.end_line,
  c.body,
  d.url,
  d.version_number,
  d.version_created_at,
  d.fetched_at,
  d.labels_json,
  bm25(chunks_fts) AS rank
FROM chunks_fts f
JOIN chunks c ON c.chunk_id = f.chunk_id
JOIN documents d ON d.doc_id = c.doc_id
WHERE chunks_fts MATCH ?
ORDER BY rank ASC
LIMIT ?;
```

### 11.8 metadata boost

初期実装では SQL の rank の後に Python 側で軽く補正する。

加点対象:

- labels に `official` がある。
- labels に `current` がある。
- labels に `approved` がある。
- title に query の文字列が含まれる。

減点対象:

- labels に `draft` がある。
- labels に `wip` がある。
- labels に `deprecated` がある。
- labels に `old` がある。
- title に `旧`, `廃止`, `コピー`, `メモ`, `検討中`, `draft`, `old` がある。

最終スコアは単純でよい。

```python
final_score = keyword_score + metadata_boost
```

---

## 12. インデックス構築 CLI 仕様

実装対象ファイル:

```text
tools/build_doc_index.py
```

### 12.1 基本コマンド

```bash
python tools/build_doc_index.py --space PROJECT_A
```

処理:

1. `docs/confluence/PROJECT_A/manifest.jsonl` を読む。
2. 各 Markdown を読む。
3. frontmatter を解析する。
4. heading-aware chunking を行う。
5. `.local-doc-index/docs.db` に documents / chunks / chunks_fts を作る。
6. 対象 space の既存 documents/chunks を削除して再登録する。

### 12.2 全スペース再構築

```bash
python tools/build_doc_index.py --all
```

処理:

- `docs/confluence/*/manifest.jsonl` を対象にする。

### 12.3 差分更新

初期実装では、space 単位の再構築でよい。

将来的に `--changed-manifest` や page_id 単位更新を追加できるよう、関数は分ける。

---

## 13. 検索 CLI 仕様

実装対象ファイル:

```text
tools/search_docs.py
```

### 13.1 基本コマンド

```bash
python tools/search_docs.py "認証API refresh token 仕様" --space PROJECT_A --top-k 10
```

### 13.2 出力形式

デフォルトは Markdown 形式で表示する。

```markdown
# Search Results

Query: 認証API refresh token 仕様  
Space: PROJECT_A  
Top K: 10

## 1. 認証API仕様 > Token更新

- Score: 0.912
- Path: docs/confluence/PROJECT_A/pages/123456__auth-api-spec.md
- Lines: 120-180
- URL: https://your-domain.atlassian.net/wiki/spaces/PROJECT_A/pages/123456
- Version: 18
- Updated: 2026-05-05T09:30:00.000Z
- Fetched: 2026-05-05T11:00:00+09:00
- Labels: official, auth

```excerpt
refresh token rotation は...
```
```

### 13.3 JSON 出力

```bash
python tools/search_docs.py "認証API" --space PROJECT_A --top-k 10 --json
```

出力:

```json
[
  {
    "score": 0.912,
    "chunk_id": "confluence:PROJECT_A:123456:3",
    "path": "docs/confluence/PROJECT_A/pages/123456__auth-api-spec.md",
    "line_range": "120-180",
    "title": "認証API仕様",
    "headings": ["認証API仕様", "Token更新"],
    "url": "https://your-domain.atlassian.net/wiki/spaces/PROJECT_A/pages/123456",
    "version_number": 18,
    "version_created_at": "2026-05-05T09:30:00.000Z",
    "fetched_at": "2026-05-05T11:00:00+09:00",
    "labels": ["official", "auth"],
    "excerpt": "refresh token rotation は..."
  }
]
```

### 13.4 検索オプション

```bash
--space PROJECT_A        # 対象space。省略時は全space。
--top-k 10              # 返却件数。
--json                  # JSON出力。
--include-draft         # draft/wip/deprecatedも除外せず検索。
--path-only             # pathだけ表示。
--open                  # macOSなら `open path` する。初期実装では任意。
```

### 13.5 query のエスケープ

FTS5 MATCH query は特殊文字に弱い場合がある。初期実装では、ユーザー入力をそのまま高度な MATCH 構文として扱わず、以下の安全な処理にする。

- ユーザー入力を空白で分割する。
- 各 token を double quote で囲む。
- AND 検索ではなく、まずは OR 風に緩く検索する。
- エラーになった場合は、文字列全体を quote した検索に fallback する。

---

## 14. AGENTS.md 仕様

Codex CLI が実装・利用時に迷わないよう、リポジトリ直下に `AGENTS.md` を置く。

```markdown
# AGENTS.md

## Project Goal

This repository contains a local Confluence sync and local document search system.

## Important Rules

- Do not commit `docs/confluence/`, `.local-confluence-sync/`, `.local-doc-index/`, or `.env`.
- Do not print API tokens in logs.
- Use `tools/sync_confluence.py` to download Confluence pages.
- Use `tools/build_doc_index.py` to build the local SQLite FTS5 index.
- Use `tools/search_docs.py` before answering questions about synced Confluence specs.
- Prefer documents with `official`, `current`, or `approved` labels.
- Treat `draft`, `wip`, `deprecated`, `old`, `copy`, `memo`, and similar pages as lower confidence.
- When answering based on local docs, always mention `path`, `line_range`, `version_number`, and `fetched_at`.
- If `fetched_at` is old, explicitly state that the local snapshot may be stale.

## Commands

```bash
python tools/sync_confluence.py full --space PROJECT_A
python tools/sync_confluence.py incremental --space PROJECT_A --reindex
python tools/build_doc_index.py --space PROJECT_A
python tools/search_docs.py "query" --space PROJECT_A --top-k 10
```
```

---

## 15. requirements.txt

初期実装の依存関係:

```text
requests>=2.31.0
python-dotenv>=1.0.0
beautifulsoup4>=4.12.0
markdownify>=0.11.6
PyYAML>=6.0.1
python-dateutil>=2.8.2
```

SQLite は Python 標準の `sqlite3` を使う。

開発・テスト用に追加してよいもの:

```text
pytest>=8.0.0
responses>=0.25.0
```

---

## 16. ログ仕様

### 16.1 ログ出力先

```text
.local-confluence-sync/sync.log
.local-confluence-sync/sync.err.log
```

### 16.2 ログに含めるもの

- run_id
- mode
- space_key
- page_id
- title
- version_number
- local_path
- fetched / updated / skipped / failed counts
- retry count
- error summary

### 16.3 ログに含めてはいけないもの

- API token
- Authorization header
- Cookie
- 個人情報の本文抜粋

---

## 17. エラー処理仕様

### 17.1 ページ単位エラー

ページ単位で失敗した場合:

- `sync_errors` に記録する。
- 他ページの同期は続ける。
- run status は `partial_failed` にする。
- `last_successful_sync_at` は更新しない。

### 17.2 全体エラー

space 解決失敗、認証失敗、DB 初期化失敗などは全体エラーとする。

- run status は `failed`。
- 例外を再 raise する。
- `last_successful_sync_at` は更新しない。

### 17.3 認証エラー

HTTP 401 / 403 は retry しない。

- 401: email/token/base_url を確認するようメッセージを出す。
- 403: 対象スペースまたはページへの閲覧権限を確認するようメッセージを出す。

---

## 18. 定期実行

### 18.1 cron 例

```cron
# 毎日 8:30 に差分同期 + インデックス再構築
30 8 * * * cd /path/to/repo && /usr/bin/env bash -lc 'python tools/sync_confluence.py incremental --space PROJECT_A --reindex >> .local-confluence-sync/sync.log 2>> .local-confluence-sync/sync.err.log'
```

### 18.2 macOS launchd 例

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
      <string>-lc</string>
      <string>cd /path/to/repo && python tools/sync_confluence.py incremental --space PROJECT_A --reindex</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key>
      <integer>8</integer>
      <key>Minute</key>
      <integer>30</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>/path/to/repo/.local-confluence-sync/sync.log</string>

    <key>StandardErrorPath</key>
    <string>/path/to/repo/.local-confluence-sync/sync.err.log</string>
  </dict>
</plist>
```

---

## 19. README.md に書くべき内容

README には最低限以下を記載する。

```markdown
# Local Confluence Sync and Search

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` を編集する。

## Full sync

```bash
python tools/sync_confluence.py full --space PROJECT_A --reindex
```

## Incremental sync

```bash
python tools/sync_confluence.py incremental --space PROJECT_A --reindex
```

## Search

```bash
python tools/search_docs.py "認証API refresh token" --space PROJECT_A --top-k 10
```

## Security

Do not commit `.env`, `docs/confluence/`, `.local-confluence-sync/`, or `.local-doc-index/`.
```

---

## 20. テスト仕様

### 20.1 単体テスト対象

- slug 生成
- frontmatter 生成
- content_hash 計算
- Markdown 変換
- heading-aware chunking
- FTS5 query escape
- state.db upsert
- manifest.jsonl 生成
- index.md 生成
- retry/backoff 判定

### 20.2 結合テスト対象

mock HTTP を使って以下を確認する。

1. full sync で複数ページが Markdown 保存される。
2. page_id を含むファイル名になる。
3. state.db に version_number が保存される。
4. 同じ version の incremental sync では skip される。
5. version が増えた場合のみ Markdown が更新される。
6. CQL 検索結果が複数ページングされても全件処理される。
7. 429 + Retry-After で待機後に retry される。
8. 401/403 は retry されない。
9. `build_doc_index.py` で docs.db が作成される。
10. `search_docs.py` で期待するチャンクが返る。

### 20.3 手動確認項目

```bash
# 1. 初回同期
python tools/sync_confluence.py full --space PROJECT_A

# 2. Markdown生成確認
ls docs/confluence/PROJECT_A/pages/
cat docs/confluence/PROJECT_A/index.md
head -80 docs/confluence/PROJECT_A/pages/*.md

# 3. インデックス構築
python tools/build_doc_index.py --space PROJECT_A

# 4. 検索
python tools/search_docs.py "認証 API" --space PROJECT_A --top-k 5

# 5. 差分同期
python tools/sync_confluence.py incremental --space PROJECT_A --dry-run
python tools/sync_confluence.py incremental --space PROJECT_A --reindex
```

---

## 21. 実装ファイル別責務

### 21.1 `tools/confluence_client.py`

責務:

- 認証 header 作成
- GET request wrapper
- retry/backoff
- pagination
- `get_space_by_key(space_key)`
- `list_pages_in_space(space_id)`
- `get_page_detail(page_id)`
- `search_updated_pages_by_cql(space_key, since)`

### 21.2 `tools/db.py`

責務:

- state.db 初期化
- docs.db 初期化
- pages upsert
- sync_state update
- sync_runs insert/update
- sync_errors insert
- index documents/chunks insert

### 21.3 `tools/markdown_converter.py`

責務:

- storage XHTML 前処理
- Confluence 独自タグの簡易変換
- Markdown 変換
- frontmatter 生成
- content_hash 計算
- slug 生成

### 21.4 `tools/sync_confluence.py`

責務:

- CLI parse
- full / incremental / page mode
- raw 保存
- Markdown 保存
- manifest/index 再生成
- reindex 呼び出し

### 21.5 `tools/build_doc_index.py`

責務:

- manifest 読み込み
- Markdown/frontmatter 解析
- chunking
- SQLite FTS5 index 構築

### 21.6 `tools/search_docs.py`

責務:

- query parse
- FTS5 検索
- metadata boost
- Markdown / JSON 出力

### 21.7 `tools/utils.py`

責務:

- ISO datetime helper
- path helper
- safe file write
- JSONL helper
- logging helper

---

## 22. 安全なファイル書き込み

Markdown / manifest / index を書くときは、破損を避けるため atomic write にする。

手順:

1. 同じディレクトリに `.tmp` ファイルを書く。
2. flush / close する。
3. `os.replace(tmp, final)` で置換する。

---

## 23. 文字コード

- すべて UTF-8。
- 改行は `\n`。
- JSON は `ensure_ascii=False`。

---

## 24. Codex CLI に依頼する実装順序

Codex CLI には以下の順序で実装させる。

### Step 1: プロジェクト骨格

- `requirements.txt`
- `.env.example`
- `.gitignore`
- `README.md`
- `AGENTS.md`
- `tools/` 作成

### Step 2: DB 層

- `tools/db.py`
- state.db schema
- docs.db schema
- migration は初期実装では不要。ただし `CREATE TABLE IF NOT EXISTS` を使う。

### Step 3: Confluence client

- `tools/confluence_client.py`
- Basic auth
- request retry
- pagination
- spaces/pages/search API wrapper

### Step 4: Markdown converter

- `tools/markdown_converter.py`
- storage XHTML → Markdown
- frontmatter
- slug
- content_hash

### Step 5: sync CLI

- `tools/sync_confluence.py`
- full
- incremental
- page
- dry-run
- force
- reindex

### Step 6: index builder

- `tools/build_doc_index.py`
- manifest load
- chunking
- FTS5 index

### Step 7: search CLI

- `tools/search_docs.py`
- Markdown output
- JSON output
- metadata boost

### Step 8: tests

- pytest 追加
- mock API response で主要処理をテスト

---

## 25. 完了条件

実装は以下を満たしたら完了とする。

1. `.env` 設定後、`python tools/sync_confluence.py full --space PROJECT_A` でページ Markdown が生成される。
2. `docs/confluence/PROJECT_A/index.md` が生成される。
3. `docs/confluence/PROJECT_A/manifest.jsonl` が生成される。
4. `.local-confluence-sync/state.db` に同期状態が保存される。
5. 2回目の `incremental` で version が変わらないページは skip される。
6. Confluence 側で更新されたページのみ Markdown が再生成される。
7. `python tools/build_doc_index.py --space PROJECT_A` で `.local-doc-index/docs.db` が生成される。
8. `python tools/search_docs.py "検索語" --space PROJECT_A` で検索結果が返る。
9. 検索結果に path / line range / URL / version / fetched_at / labels が表示される。
10. API token がログや出力に表示されない。
11. 429 で Retry-After に従って retry する。
12. 401/403 は retry せず、原因が分かるエラーを出す。

---

## 26. ベクトル検索・ハイブリッド検索の追加実装仕様

この章は、初期実装の SQLite FTS5 全文検索が完成した後に追加するローカルベクトル検索、および SQLite FTS5 + vector search のハイブリッド検索の実装仕様である。

重要方針:

- 初期実装の `documents`, `chunks`, `chunks_fts` は維持する。
- ベクトル検索を追加しても、Markdown 同期仕様は変えない。
- chunk 単位で embedding を作成する。
- ベクトル検索結果は `chunk_id` を返す。
- `chunk_id` を使って SQLite の `chunks` / `documents` から metadata を引く。
- Codex CLI からの利用コマンドは `tools/search_docs.py` に統一する。
- FAISS を第一候補、Chroma を代替実装とする。
- どちらを使う場合でも、検索 CLI の出力形式は同じにする。

---

## 27. ローカルベクトル検索 実装仕様

### 27.1 目的

SQLite FTS5 はキーワード一致に強い。一方で、言い換えや自然文の質問には弱い。

例:

```text
質問: ログイン状態を延長する仕組みはどこに書いてありますか？
文書: refresh token rotation / access token renewal / session renewal
```

このような場合に、ローカル embedding + vector search を使って意味的に近い chunk を検索する。

### 27.2 対象データ単位

ベクトル化対象は Markdown ファイル単位ではなく、`build_doc_index.py` で生成した chunk 単位とする。

理由:

- ページ全体を embedding すると粒度が粗すぎる。
- chunk 単位なら検索結果を Codex CLI が直接読める。
- path / line_range / heading と紐づけやすい。

embedding input text は以下の形式にする。

```text
Title: {title}
Headings: {heading1} > {heading2} > ...
Labels: {labels}

{chunk_body}
```

`title` と `headings` を含める理由:

- chunk 本文だけだと文脈が欠落する。
- 「Token更新」などの見出しが検索精度に効く。
- Confluence 由来の長いページでは見出し階層が重要。

### 27.3 embedding model

初期候補は `sentence-transformers` を使う。

推奨デフォルト:

```text
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

理由:

- 日本語と英語が混ざる社内ドキュメントで使いやすい。
- モデルサイズが比較的小さい。
- ローカル CPU 実行でも現実的。

高精度寄りの候補:

```text
intfloat/multilingual-e5-base
intfloat/multilingual-e5-large
BAAI/bge-m3
```

注意:

- 初期実装ではモデルを1つに固定し、設定で切り替え可能にする。
- embedding model 名は `.local-doc-index/vector_meta.json` に保存する。
- モデルを変更した場合は、既存 vector index を再構築する。
- 初回実行時に Hugging Face からモデルをダウンロードする可能性がある。
- 完全オフライン運用にしたい場合は、事前にモデルをローカルキャッシュしておく。

### 27.4 追加 requirements

FAISS を使う場合:

```text
sentence-transformers>=3.0.0
numpy>=1.26.0
faiss-cpu>=1.8.0
```

Chroma を使う場合:

```text
sentence-transformers>=3.0.0
chromadb>=0.5.0
```

初期実装では `requirements.txt` にすべてを必ず入れる必要はない。以下のように分けてもよい。

```text
requirements.txt
requirements-vector-faiss.txt
requirements-vector-chroma.txt
```

推奨:

```text
requirements.txt                 # FTS5までの最小構成
requirements-vector-faiss.txt    # FAISSベクトル検索用
requirements-vector-chroma.txt   # Chromaベクトル検索用
```

### 27.5 設定項目

`.env.example` に以下を追加する。

```bash
# vector search backend: none, faiss, chroma
DOC_VECTOR_BACKEND=faiss

# sentence-transformers model name or local model path
DOC_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2

# batch size for embedding
DOC_EMBEDDING_BATCH_SIZE=32

# normalize embeddings for cosine similarity via inner product
DOC_EMBEDDING_NORMALIZE=true

# vector index files
DOC_FAISS_INDEX_PATH=.local-doc-index/faiss.index
DOC_VECTOR_META_PATH=.local-doc-index/vector_meta.json
DOC_CHROMA_DIR=.local-doc-index/chroma
DOC_CHROMA_COLLECTION=confluence_chunks
```

### 27.6 SQLite 追加テーブル

FAISS 利用時は、FAISS index 側にはベクトルしか入らないため、FAISS の row index と `chunk_id` の対応表を SQLite に保存する。

```sql
CREATE TABLE IF NOT EXISTS vector_chunks (
  vector_id INTEGER PRIMARY KEY,
  chunk_id TEXT NOT NULL UNIQUE,
  doc_id TEXT NOT NULL,
  space_key TEXT NOT NULL,
  page_id TEXT NOT NULL,
  embedding_model TEXT NOT NULL,
  embedding_dim INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT,
  FOREIGN KEY(chunk_id) REFERENCES chunks(chunk_id)
);
```

補足:

- `vector_id` は FAISS index に追加した順番と一致させる。
- `content_hash` は chunk body + title + headings から計算する。
- chunk 内容が変わった場合は vector index を再構築する。
- 初期実装では差分ベクトル更新ではなく、space 単位または全体再構築でよい。

### 27.7 FAISS backend 仕様

FAISS を使う場合のファイル構成:

```text
.local-doc-index/
  docs.db
  faiss.index
  vector_meta.json
```

`vector_meta.json` 例:

```json
{
  "backend": "faiss",
  "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
  "embedding_dim": 384,
  "normalized": true,
  "metric": "inner_product",
  "index_type": "IndexFlatIP",
  "created_at": "2026-05-05T12:00:00+09:00",
  "chunk_count": 12345
}
```

推奨 index:

```text
IndexFlatIP
```

理由:

- 実装が単純。
- ローカル Markdown 数千〜数万 chunk 程度なら十分扱いやすい。
- embedding を L2 normalize しておけば inner product を cosine similarity として扱える。
- 近似検索ではなく exact search なのでデバッグしやすい。

大規模化した場合の候補:

```text
IndexHNSWFlat
IndexIVFFlat
```

ただし初期実装では採用しない。

### 27.8 FAISS index 構築フロー

`tools/build_doc_index.py --space PROJECT_A --vector-backend faiss` で以下を行う。

```text
1. manifest.jsonl を読む
2. Markdown を読み、chunk を生成する
3. SQLite documents/chunks/chunks_fts を更新する
4. chunk ごとに embedding input text を作る
5. sentence-transformers で embedding を生成する
6. DOC_EMBEDDING_NORMALIZE=true の場合は L2 normalize する
7. FAISS IndexFlatIP を作る
8. embedding を index に add する
9. vector_chunks に vector_id -> chunk_id を保存する
10. faiss.index を保存する
11. vector_meta.json を保存する
```

疑似コード:

```python
def build_faiss_index(chunks, model_name, index_path):
    model = SentenceTransformer(model_name)
    texts = [make_embedding_text(chunk) for chunk in chunks]
    embeddings = model.encode(
        texts,
        batch_size=DOC_EMBEDDING_BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    embeddings = np.asarray(embeddings, dtype="float32")

    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)
    faiss.write_index(index, index_path)

    for vector_id, chunk in enumerate(chunks):
        insert_vector_chunk(vector_id=vector_id, chunk_id=chunk.chunk_id, ...)
```

### 27.9 FAISS search フロー

`tools/search_docs.py "query" --vector --space PROJECT_A` で以下を行う。

```text
1. vector_meta.json を読む
2. embedding model をロードする
3. query を embedding する
4. normalize する
5. faiss.index をロードする
6. top_k 件を検索する
7. vector_id を chunk_id に変換する
8. chunks/documents metadata を SQLite から読む
9. Markdown/JSON形式で表示する
```

疑似コード:

```python
def vector_search(query, top_k):
    model = SentenceTransformer(meta["embedding_model"])
    q = model.encode([query], normalize_embeddings=True)
    q = np.asarray(q, dtype="float32")

    index = faiss.read_index(DOC_FAISS_INDEX_PATH)
    scores, vector_ids = index.search(q, top_k)

    results = []
    for score, vector_id in zip(scores[0], vector_ids[0]):
        if vector_id < 0:
            continue
        chunk_id = get_chunk_id_by_vector_id(vector_id)
        result = load_chunk_result(chunk_id)
        result["vector_score"] = float(score)
        results.append(result)
    return results
```

### 27.10 Chroma backend 仕様

Chroma を使う場合のファイル構成:

```text
.local-doc-index/
  docs.db
  chroma/
  vector_meta.json
```

Chroma collection:

```text
confluence_chunks
```

Chroma document id:

```text
{chunk_id}
```

Chroma metadata:

```json
{
  "chunk_id": "confluence:PROJECT_A:123456:3",
  "doc_id": "confluence:PROJECT_A:123456",
  "space_key": "PROJECT_A",
  "page_id": "123456",
  "path": "docs/confluence/PROJECT_A/pages/123456__auth-api-spec.md",
  "title": "認証API仕様",
  "headings": "認証API仕様 > Token更新",
  "start_line": 120,
  "end_line": 180,
  "version_number": 18,
  "fetched_at": "2026-05-05T11:00:00+09:00",
  "labels": "official,auth"
}
```

Chroma では collection に documents と metadata を入れることができる。初期実装では、Chroma 側にも chunk body を保存してよい。ただし最終出力の metadata は SQLite を正とする。

### 27.11 Chroma index 構築フロー

```python
def build_chroma_index(chunks, model_name, persist_dir, collection_name):
    client = chromadb.PersistentClient(path=persist_dir)
    collection = client.get_or_create_collection(name=collection_name)

    # space単位再構築の場合、対象spaceの既存chunkを削除する。
    # Chromaのdelete条件が使いにくい場合はcollection再作成でもよい。

    ids = [chunk.chunk_id for chunk in chunks]
    documents = [make_embedding_text(chunk) for chunk in chunks]
    metadatas = [make_chroma_metadata(chunk) for chunk in chunks]

    collection.add(
        ids=ids,
        documents=documents,
        metadatas=metadatas,
    )
```

実装方針:

- Chroma の embedding function を使ってもよい。
- ただし FAISS backend と結果を比較しやすくするため、初期実装では sentence-transformers で明示的に embeddings を作って `embeddings=` として渡す方が望ましい。
- これにより FAISS / Chroma で同じ embedding model を使える。

### 27.12 Chroma search フロー

```python
def chroma_search(query, top_k):
    client = chromadb.PersistentClient(path=DOC_CHROMA_DIR)
    collection = client.get_collection(DOC_CHROMA_COLLECTION)

    query_embedding = model.encode([query], normalize_embeddings=True)[0].tolist()

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
    )

    chunk_ids = results["ids"][0]
    distances = results["distances"][0]

    # chunk_id を使って SQLite から metadata を読む。
```

注意:

- Chroma の distance は backend 設定により意味が変わる可能性がある。
- `search_docs.py` の内部で `vector_score` に正規化する。
- CLI 出力では FAISS と同じ形式に揃える。

### 27.13 FAISS と Chroma の選択基準

| 観点 | FAISS | Chroma |
|---|---:|---:|
| ローカル軽量性 | 高い | 中〜高 |
| 実装の透明性 | 高い | 中 |
| metadata 管理 | SQLite併用が必要 | Chroma内にも保存可能 |
| Codex CLIからの単純さ | 高い | 中 |
| 将来のRAG拡張 | 中 | 高い |
| 依存関係の少なさ | 比較的少ない | やや多い |
| 推奨度 | 第一候補 | 代替候補 |

本プロジェクトでは、初期の vector backend は FAISS を推奨する。

---

## 28. SQLite FTS5 + Vector Search ハイブリッド検索仕様

### 28.1 目的

キーワード検索と意味検索は得意領域が異なる。

SQLite FTS5 が強いもの:

```text
- API path
- エラーコード
- チケット番号
- 固有名詞
- exact phrase
- 設定キー
- 関数名
- 具体的な仕様語
```

vector search が強いもの:

```text
- 言い換え
- 自然文質問
- 仕様の概念検索
- 日本語/英語が混在した意味検索
- 表現が統一されていないドキュメント検索
```

したがって、最終的な検索は FTS5 と vector search の結果を統合する。

### 28.2 検索 CLI モード

`tools/search_docs.py` に以下のオプションを追加する。

```bash
# FTSのみ。初期実装の既定値。
python tools/search_docs.py "認証API" --space PROJECT_A --mode fts

# vectorのみ。
python tools/search_docs.py "ログイン状態を延長する仕組み" --space PROJECT_A --mode vector

# FTS + vector。追加実装後の推奨既定値。
python tools/search_docs.py "ログイン状態を延長する仕組み" --space PROJECT_A --mode hybrid
```

既定値:

```text
初期実装: --mode fts
vector 実装後: --mode hybrid
```

追加オプション:

```bash
--vector-backend faiss|chroma
--fts-k 30
--vector-k 30
--top-k 10
--include-draft
--json
--explain
```

### 28.3 ハイブリッド検索処理フロー

```text
1. query を受け取る
2. FTS5 で fts_k 件取得する
3. vector search で vector_k 件取得する
4. chunk_id で結果をマージする
5. 各結果に fts_score / vector_score / metadata_score を付ける
6. score を正規化する
7. weighted sum または RRF で統合する
8. top_k 件を返す
```

### 28.4 推奨統合方式: RRF

初期のハイブリッド検索では Reciprocal Rank Fusion 風の順位統合を使う。

理由:

- FTS の bm25 と vector cosine score はスケールが異なる。
- スコア正規化の調整に時間を使わずに安定した統合ができる。
- 実装が簡単。

式:

```text
rrf_score = 1 / (k + fts_rank) + 1 / (k + vector_rank) + metadata_boost
```

推奨値:

```text
k = 60
```

rank は 1 始まり。

FTS に出なかった chunk は `fts_rank = None` とし、FTS 側の項は 0 とする。vector に出なかった chunk も同様。

### 28.5 metadata boost

metadata boost は RRF score に加算する。

推奨初期値:

```python
boost = 0.0

if has_label("official"):
    boost += 0.030
if has_label("current"):
    boost += 0.020
if has_label("approved"):
    boost += 0.020
if title_contains_query_keyword:
    boost += 0.015

if has_label("draft"):
    boost -= 0.030
if has_label("wip"):
    boost -= 0.020
if has_label("deprecated"):
    boost -= 0.050
if has_label("old"):
    boost -= 0.040
if title_has_risky_word:
    boost -= 0.030
```

危険語:

```text
旧
廃止
コピー
メモ
検討中
draft
old
deprecated
wip
```

注意:

- metadata boost は小さくする。
- official ラベルだけで検索順位を完全に支配しない。
- 古いが正式な文書もあり得るため、最終回答では必ず version / fetched_at / labels を出す。

### 28.6 hybrid result object

内部的には以下の形で結果を表現する。

```python
@dataclass
class HybridSearchResult:
    chunk_id: str
    doc_id: str
    path: str
    title: str
    headings: list[str]
    body: str
    start_line: int | None
    end_line: int | None
    url: str | None
    version_number: int | None
    version_created_at: str | None
    fetched_at: str | None
    labels: list[str]
    fts_rank: int | None
    vector_rank: int | None
    fts_score: float | None
    vector_score: float | None
    metadata_boost: float
    final_score: float
    match_reason: list[str]
```

`match_reason` 例:

```text
- keyword match
- semantic match
- label: official
- title match
- penalty: draft
```

### 28.7 `--explain` 出力

`--explain` を指定した場合は、各検索結果に score breakdown を表示する。

```markdown
### Score Breakdown

- final_score: 0.0617
- fts_rank: 2
- vector_rank: 5
- metadata_boost: 0.030
- match_reason:
  - keyword match
  - semantic match
  - label: official
```

Codex CLI が「なぜこの文書を根拠にしたか」を判断しやすくなる。

### 28.8 ハイブリッド検索疑似コード

```python
def hybrid_search(query, space_key=None, top_k=10, fts_k=30, vector_k=30):
    fts_results = fts_search(query, space_key=space_key, top_k=fts_k)
    vector_results = vector_search(query, space_key=space_key, top_k=vector_k)

    merged = {}

    for rank, result in enumerate(fts_results, start=1):
        item = merged.setdefault(result.chunk_id, result)
        item.fts_rank = rank
        item.fts_score = result.fts_score
        item.match_reason.append("keyword match")

    for rank, result in enumerate(vector_results, start=1):
        item = merged.setdefault(result.chunk_id, result)
        item.vector_rank = rank
        item.vector_score = result.vector_score
        item.match_reason.append("semantic match")

    for item in merged.values():
        score = 0.0
        if item.fts_rank is not None:
            score += 1.0 / (60 + item.fts_rank)
        if item.vector_rank is not None:
            score += 1.0 / (60 + item.vector_rank)
        item.metadata_boost = compute_metadata_boost(item, query)
        item.final_score = score + item.metadata_boost

    return sorted(merged.values(), key=lambda x: x.final_score, reverse=True)[:top_k]
```

### 28.9 検索出力仕様の拡張

Hybrid mode の Markdown 出力例:

```markdown
## 1. 認証API仕様 > Token更新

- Final Score: 0.0617
- Match: keyword + semantic
- Path: docs/confluence/PROJECT_A/pages/123456__auth-api-spec.md
- Lines: 120-180
- URL: https://your-domain.atlassian.net/wiki/spaces/PROJECT_A/pages/123456
- Version: 18
- Updated: 2026-05-05T09:30:00.000Z
- Fetched: 2026-05-05T11:00:00+09:00
- Labels: official, auth

```excerpt
refresh token rotation は...
```
```

JSON 出力には以下を追加する。

```json
{
  "final_score": 0.0617,
  "fts_rank": 2,
  "vector_rank": 5,
  "fts_score": -8.31,
  "vector_score": 0.742,
  "metadata_boost": 0.03,
  "match_reason": ["keyword match", "semantic match", "label: official"]
}
```

### 28.10 再構築方針

初期の vector / hybrid 実装では、差分更新ではなく space 単位再構築でよい。

理由:

- 実装が単純。
- vector_id と chunk_id の対応を安全に保てる。
- ローカル用途では多少の再構築時間は許容しやすい。

コマンド:

```bash
python tools/build_doc_index.py --space PROJECT_A --vector-backend faiss
python tools/build_doc_index.py --space PROJECT_A --vector-backend chroma
```

将来、ページ数が多くなった場合は、以下を検討する。

- chunk content_hash による差分 embedding
- FAISS index の全体再構築を避けるための Chroma 利用
- 削除・更新 chunk の tombstone 管理
- 世代別 index directory

### 28.11 エラー処理

vector search 関連で想定するエラー:

- embedding model が未ダウンロード
- faiss が import できない
- chromadb が import できない
- vector_meta.json が存在しない
- embedding model が index 作成時と検索時で異なる
- embedding dimension が合わない
- FAISS index と vector_chunks の件数が一致しない

処理方針:

- `--mode vector` で vector index がない場合はエラーにする。
- `--mode hybrid` で vector index がない場合は警告を出して FTS のみで検索してよい。
- embedding dimension mismatch は必ずエラーにする。
- FAISS index と vector_chunks の件数不一致はエラーにし、再構築を促す。

### 28.12 テスト仕様: vector search

単体テスト:

1. embedding input text に title/headings/body が含まれる。
2. vector_meta.json が正しく保存される。
3. vector_chunks に vector_id と chunk_id が保存される。
4. FAISS index の ntotal と vector_chunks 件数が一致する。
5. vector search で chunk_id が返る。
6. Chroma backend でも同じ形式の result object が返る。

結合テスト:

1. 小さな Markdown fixture を用意する。
2. `build_doc_index.py --vector-backend faiss` を実行する。
3. `search_docs.py "自然文クエリ" --mode vector` で期待 chunk が返る。
4. `search_docs.py "自然文クエリ" --mode hybrid` で FTS と vector の結果が統合される。
5. `--json` の schema が安定している。

### 28.13 テスト仕様: hybrid search

fixture 例:

```text
A: refresh token rotation について書かれた official 文書
B: セッション延長について日本語で書かれた draft 文書
C: refresh_token という文字列だけ含む無関係なログ文書
```

期待:

- `refresh_token` のような exact query では A/C が FTS で出る。
- `ログイン状態を延長する仕組み` のような自然文 query では A/B が vector で出る。
- hybrid では official boost により A が上位になる。
- draft penalty により B は下がるが、完全には消えない。

---

## 29. requirements 追加案

初期構成:

```text
requirements.txt
```

```text
requests>=2.31.0
python-dotenv>=1.0.0
beautifulsoup4>=4.12.0
markdownify>=0.11.6
PyYAML>=6.0.1
python-dateutil>=2.8.2
```

FAISS追加構成:

```text
requirements-vector-faiss.txt
```

```text
-r requirements.txt
sentence-transformers>=3.0.0
numpy>=1.26.0
faiss-cpu>=1.8.0
```

Chroma追加構成:

```text
requirements-vector-chroma.txt
```

```text
-r requirements.txt
sentence-transformers>=3.0.0
chromadb>=0.5.0
numpy>=1.26.0
```

開発・テスト構成:

```text
requirements-dev.txt
```

```text
-r requirements.txt
pytest>=8.0.0
responses>=0.25.0
```

---

## 30. Codex CLI への追加実装依頼文例: vector / hybrid

SQLite FTS5 までの初期実装が終わった後、以下を Codex CLI に渡す。

```text
既存の Confluence ローカル同期・SQLite FTS5 検索実装に、ローカルベクトル検索とハイブリッド検索を追加してください。

実装対象:

1. sentence-transformers による chunk embedding
2. FAISS backend
3. 任意で Chroma backend
4. vector_meta.json
5. SQLite の vector_chunks table
6. build_doc_index.py の --vector-backend faiss|chroma オプション
7. search_docs.py の --mode fts|vector|hybrid オプション
8. hybrid search の RRF 統合
9. metadata boost / penalty
10. --explain 出力
11. vector / hybrid の pytest

重要条件:

- chunk_id を検索結果の主キーにしてください。
- FAISS index には metadata を入れず、vector_id -> chunk_id を SQLite の vector_chunks で管理してください。
- embedding input text は title, headings, labels, body を含めてください。
- embeddings は normalize し、FAISS では IndexFlatIP を使ってください。
- 初期実装では差分 vector 更新は不要です。space 単位再構築で構いません。
- Hybrid search は FTS rank と vector rank を RRF で統合してください。
- 検索結果には path, line_range, URL, version_number, fetched_at, labels, final_score, fts_rank, vector_rank を表示してください。
```

---

## 31. 将来拡張メモ

初期実装および vector / hybrid 実装後、以下を追加候補とする。

1. MCP サーバー化
2. 添付ファイル OCR / テキスト抽出
3. Confluence ページ階層ツリーの保存
4. ページ削除・アーカイブ検知の強化
5. labels / page properties に基づく official/current 判定強化
6. Slack / Jira / GitHub との統合
7. レポート生成 CLI
8. ローカル snapshot の世代管理
9. Cross-encoder reranker による再ランキング
10. embedding model 評価スクリプトの追加

---

## 27. Codex CLI への最初の依頼文例

以下を Codex CLI に渡す。

```text
このリポジトリに、Confluence Cloud の指定スペースをローカル Markdown として同期し、SQLite FTS5 で検索できる仕組みを実装してください。

まず `confluence_local_sync_index_spec.md` を読み、仕様に従って以下を実装してください。

1. requirements.txt, .env.example, .gitignore, README.md, AGENTS.md
2. tools/confluence_client.py
3. tools/db.py
4. tools/markdown_converter.py
5. tools/sync_confluence.py
6. tools/build_doc_index.py
7. tools/search_docs.py
8. pytest による主要単体テスト

実装時の重要条件:
- `.env`, `docs/confluence/`, `.local-confluence-sync/`, `.local-doc-index/` は Git 管理しないでください。
- API token をログに出さないでください。
- Confluence API の 429 は Retry-After に従って retry してください。
- full sync と incremental sync の両方を実装してください。
- incremental sync は CQL の lastmodified 条件と state.db の version_number 比較を併用してください。
- Markdown には YAML frontmatter を必ず付けてください。
- 検索結果には path, line range, URL, version_number, fetched_at, labels を表示してください。

まずは SQLite FTS5 の全文検索までを完成させ、ベクトル検索は実装しないでください。
```

