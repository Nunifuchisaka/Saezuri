# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

常に日本語で応答する。

## 概要

saezuriは、X（旧Twitter）のキーワード検索・アカウント追跡・トレンド・ブックマーク、およびInstagram・Threadsの特定アカウントの投稿をPlaywrightで収集し、JSONLへ保存した上でMarkdownダイジェストを生成するツール。公式X APIは有料のため採用せず、Playwrightのブラウザ自動操作でXの内部GraphQL APIレスポンスを傍受する方式（スクレイピング）を取っている。

台本・記事本体への取捨選択・執筆は本ツールの範囲外（vrchat-video-script／note-vrchat-guideスキル側の担当）。

## 環境構築・実行コマンド

```
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium   # config.yamlのbrowserでfirefox/webkitを使う場合は該当エンジンも install
```

全コマンドはプロジェクトルートから実行する（`config.yaml`等を相対パスで参照するため）。

```
python scripts/login.py                                          # 初回・失効時の手動ログイン
python scripts/search.py --query "VRChat アップデート" --max 100  # キーワード検索
python scripts/user_tweets.py                                     # watch_accounts全件の投稿追跡
python scripts/trends.py                                          # トレンド取得
python scripts/bookmarks.py                                       # サブ垢自身のブックマーク取得
python scripts/digest.py data/raw/*.jsonl --title "..."           # JSONL→Markdownダイジェスト生成
python scripts/media.py data/raw/related_20260725_003.jsonl       # 収集済みJSONLから画像ダウンロード
python scripts/ig_login.py                                        # Instagramの初回・失効時の手動ログイン
python scripts/ig_user_posts.py                                   # instagram.watch_accounts全件の投稿追跡
python scripts/th_login.py                                        # Threadsの初回・失効時の手動ログイン
python scripts/th_user_posts.py                                   # threads.watch_accounts全件の投稿追跡
```

`tests/`配下にpytestの単体テストが存在する（Playwright非依存の純粋なパース・正規化ロジックが対象。例: `tests/test_capture_quoted.py`は`capture.py`の引用元投稿抽出ロジック、`tests/test_instagram_parse.py`・`tests/test_threads_parse.py`は`instagram.py`・`threads.py`の投稿正規化ロジックを検証する）。実行は `pytest tests/`。一方、Playwrightを使った実ブラウザ操作を伴う部分（収集本体のスクレイピングロジック）は引き続き自動テスト化されておらず、手動QAで確認する運用である。

## 運用上の制約（コード変更時に踏まえること）

- **オンデマンド実行専用**：cron・タスクスケジューラ等の定期実行を導入するコードは書かない（利用規約抵触・BAN回避のための設計方針）。
- **自動ログイン厳禁**：ID/PWの自動入力ロジックは追加しない。`login.py`は毎回人間の手動ログインを待つ設計を維持する。
- **エラー時は自動リトライ・自動突破をしない**：レートリミット（429）・チャレンジ画面（CAPTCHA）検知時は即座に処理を中断し、検知時点までの収集済みデータを保存してから終了する。自動リトライループや自動突破コードは追加しない。

## アーキテクチャ

### 共通基盤（`scripts/`直下、収集モジュールからimportされる）

- **`session.py`**：`config.yaml`の読み込み、ブラウザ起動（chromium/firefox/webkit/brave対応。braveはchromiumエンジン+`executable_path`指定で起動）、`storage_state`（手動ログイン結果）を使った`BrowserContext`生成、`verify_login`/`require_login`によるログイン状態検証（失効時`SessionExpiredError`）。
- **`storage.py`**：JSONLの追記・読み込み、`state/seen_ids.json`（グローバル既読ID）、`state/<account>_last_id.json`（アカウント別前回取得ID）の読み書きと差分判定（`filter_unseen`）。
- **`schema.py`**：`Post`／`TrendEntry`のdataclass定義。`to_dict()`/`from_dict()`でJSONLとの相互変換を行う（項目順・欠損キーの扱いはここに集約）。
- **`capture.py`**：収集処理の中核。
  - `GraphQLCapture`：`page.on("response")`で対象オペレーション（`SearchTimeline`/`UserTweets`/`UserTweetsAndReplies`/`Bookmarks`、トレンド系は`ExplorePage`等を部分一致）のレスポンスを保持し、`drain()`時に初めて`response.json()`を読む（ハンドラ内での同期読み出しによる取りこぼし・デッドロックを避けるため）。
  - `parse_timeline_payload`/`parse_trends_payload`：GraphQLペイロードから`Post`/`TrendEntry`へ正規化。X側のレスポンス構造は非公開・無予告変更されるため、キー名を決め打ちせず「`instructions`という名のlistを再帰探索する」等の耐性を持たせている。想定キー欠如時は例外を投げず`logger.warning()`でそのエントリだけスキップし、全体は落とさない設計を踏襲すること。
  - `scroll_and_collect`：スクロール→`drain()`→ランダム待機（`config.yaml`の`wait_seconds`）を、上限件数到達または新規アイテムなしが規定回数（既定3回）続くまで繰り返す。429検知で`RateLimitedError`、チャレンジ画面遷移で`ChallengeDetectedError`を送出。

### `scripts/tweet.py`（単一ツイート取得）

URL/IDで指定した1件のツイートを`x.com/<handle>/status/<id>`（ハンドル不明時は`i/status/<id>`）で開き`TweetDetail`を傍受する。他の収集エントリポイントと異なり`scroll_and_collect`は使わず、指定IDのPostが見つかった時点で即座に打ち切る専用の`fetch_tweet`関数で完結させている（会話スレッド全体を集める`related.py`の①会話スレッド収集とは目的が異なるため独立させた）。`related.py`から`resolve_start`/`build_status_url`/`find_post`をimportして再利用する。結果は標準出力に表示しつつ`data/raw/tweet_<日付>_<連番3桁>.jsonl`に1件保存する。差分管理（state）は行わない。見つからなかった場合は終了コード1。

対象ツイートが引用ツイートの場合、TweetDetailペイロードに同梱される`quoted_status_result.result`から引用元投稿も追加リクエストなしで正規化し(`capture.py`の`find_tweet_result`/`parse_quoted_post`)、同じJSONLへ2件目のレコード(`source_query`は`quoted-by:<起点投稿id>`)として保存する。この挙動は`tweet.py`のみが対象で、`parse_timeline_payload`を使う他の収集モジュール(`search.py`等)には影響しない。

### 収集エントリポイント（`scripts/search.py` / `user_tweets.py` / `trends.py` / `bookmarks.py`）

各スクリプトは同一パターンで構成される：
1. `load_config` → `launch_browser`（`--headed`未指定ならheadless）→ `new_context`（`storage_state`ロード。未ログインなら`FileNotFoundError`）
2. `require_login`でセッション検証（失効なら`SessionExpiredError`）
3. `GraphQLCapture`を対象オペレーションで生成・`attach`
4. `scroll_and_collect`で収集ループを回す（parse関数内で外側の`buffer`にも溜めておき、例外中断時も収集済み分を保存できるようにする、というパターンを踏襲すること）
5. 収集結果を`storage`経由でJSONL保存・既読/差分状態を更新
6. 終了コードは共通契約：`0`=正常 / `1`=設定・環境エラー / `2`=セッション失効 / `3`=レートリミット検知 / `4`=チャレンジ画面検知（3・4でも検知時点までの収集分は保存してから終了する）

保存先・差分管理の違い：
- `search.py`：実行ごとに新規ファイル`data/raw/search_<日付>_<連番3桁>.jsonl`。既読IDは`state/seen_ids.json`でグローバル管理。
- `user_tweets.py`：`data/raw/user_tweets_<日付>.jsonl`に追記。ハンドルごとに`state/<handle>_last_id.json`で差分管理。
- `trends.py`：`data/raw/trends_<日付>.jsonl`に追記。`interest_keywords`によるフィルタが既定でON（`--no-filter`で解除）。
- `bookmarks.py`：`data/raw/bookmarks_<日付>.jsonl`に追記。`state/bookmarks_last_id.json`で差分管理。ログイン中のサブ垢自身のブックマークしか取得できない制約あり（ID大小比較での差分判定のため、古い投稿を後からブックマークすると取得漏れが起こりうる。既知の制約としてREADME参照）。

### Instagram収集（`scripts/instagram.py` / `ig_login.py` / `ig_user_posts.py`）

X側と同じ方針（手動ログインのstorage_state再利用・内部APIレスポンス傍受・オンデマンド実行・自動リトライ/自動突破なし）でInstagramの特定アカウントの投稿を収集する。

- **`instagram.py`**（共通基盤）：Instagram用storage_state（`config.yaml`の`instagram.storage_state_path`、X用とは別ファイル）のロードと、`sessionid` Cookie＋ログイン画面リダイレクト有無によるログイン検証（`require_instagram_login`、失効時`SessionExpiredError`）。`InstagramCapture`は`GraphQLCapture`を継承し、URLで`web_profile_info`/`feed/user`/`graphql/query`/`api/graphql`を捕捉する。429でレートリミット、本文の`checkpoint_required`等やURLの`/challenge`等でチャレンジ、ログイン画面遷移で失効を検知する。`capture.scroll_and_collect`にそのまま渡せる。
- **`parse_instagram_payload`**：ルートキーを決め打ちせず、v1形式（`code`+`taken_at`+`pk`/`id`）と旧GraphQL形式（`shortcode`+`taken_at_timestamp`）の投稿ノードを再帰探索で拾って`Post`へ正規化する。投稿ノードの内側（カルーセル子要素）には潜らない。ストーリーズ（`product_type == "story"`）は除外。欠けたキーは`logger.warning()`でその1件だけスキップする。`Post`への対応は`url`＝`/p/<code>/`、`reply_count`＝コメント数、`repost_count`/`quote_count`＝0、`source_query`＝`instagram:<ユーザー名>`。
- **`ig_login.py`**：`login.py`のInstagram版。`sessionid` Cookie付与をポーリングで待ってstorage_stateを保存する。自動入力は追加しない。
- **`ig_user_posts.py`**：`user_tweets.py`と同じ構成・終了コード契約。`data/raw/instagram_posts_<日付>.jsonl`に追記し、`state/instagram_<ユーザー名>_last_id.json`で差分管理（`user_tweets.py`の`filter_new_posts`/`max_post_id`/`dedupe_by_id`を再利用）。収集途中の失効（ログイン画面遷移）もそこまでの分を保存してから終了コード2で終える。作者が対象アカウント本人の投稿のみ保存する（プロフィールページのサイドバー等に出る他人の投稿は`filter_by_author`で`scroll_and_collect`に渡す前に除外し、件数・差分管理の最大IDにも含めない）。

### Threads収集（`scripts/threads.py` / `th_login.py` / `th_user_posts.py`）

ThreadsはInstagramと同じ基盤で投稿ノードもv1形式とほぼ同じため、`instagram.py`を再利用する（Instagram収集と同じ方針・同じ終了コード契約）。

- **`threads.py`**（共通基盤）：Threads用storage_state（`threads.storage_state_path`）のロードと、`threads.com`/`threads.net`の`sessionid` Cookieによるログイン検証（`require_threads_login`）。`ThreadsCapture`は`InstagramCapture`を継承し、`endpoint_name`/`is_login_url`/`is_challenge_url`をThreads用に差し替えている（`InstagramCapture`側はこれらのメソッド経由で判定するため、判定条件を変える場合はオーバーライドで行う）。
- **初回表示分はAPIではなくページ埋め込みJSON**（`<script type="application/json">`）で届くため、`load_embedded_payloads`で取り出し`ThreadsCapture.inject()`で次の`drain()`に載せてから`scroll_and_collect`に入る。
- **`parse_threads_payload`**：`text_post_app_info`を持つv1形式の投稿ノードを再帰探索で拾う（内側の引用元投稿・カルーセル子要素には潜らない）。メディア抽出・時刻変換は`instagram.py`の`_parse_v1_media_list`/`_timestamp_to_iso`等を流用。`reply_count`＝`direct_reply_count`、`repost_count`/`quote_count`＝`text_post_app_info`の値、`quoted_id`＝`share_info.quoted_post`のpk、投稿者が対象アカウントと異なる投稿は`is_repost=True`。
- **`th_login.py`** / **`th_user_posts.py`**：`ig_login.py`/`ig_user_posts.py`と同構成。`data/raw/threads_posts_<日付>.jsonl`に追記し、`state/threads_<ユーザー名>_last_id.json`で差分管理。

### `digest.py`（収集系と非依存）

`data/raw/*.jsonl`を読み込みMarkdownダイジェストを生成する唯一の非Playwright依存モジュール。Xへの実アクセスは一切行わない。各行を`trend_word`キーの有無／`id`+`author_handle`キーの有無で`Post`/`TrendEntry`に判別する（`classify_record`）。エンゲージメント合計（like+repost+reply+quote）順の「注目ポスト」セクションは著作権配慮のため本文要約（先頭100文字）+出典URLのみで全文転載はしない設計を維持すること。

### `media.py`（収集系と非依存）

収集済み`data/raw/*.jsonl`に記録済みのメディアURLから画像だけをダウンロードする。`digest.py`と同じくPlaywright非依存で、X本体（x.com）へは一切アクセスしない（画像CDNへのHTTP GETのみ）。行の判別は`digest.py`の`classify_record`をそのままimportして流用し、Post以外・解釈できない行は`logger.warning()`でスキップする。

- 対象は`MediaItem.type == "photo"`のみ。`video`／`animated_gif`はダウンロードせず件数だけ数える（この方針を変えない）。
- ダウンロードは標準ライブラリ`urllib.request`のみで行い、`requirements.txt`に依存を足さない（外部依存最小の設計を維持する）。逐次実行・1枚ごとに0.5〜2.0秒のランダム待機（`config.yaml`の`wait_seconds`はスクロール用のため流用しない）。並列化・自動リトライは追加しない。
- `pbs.twimg.com`のURLは`to_original_url()`で原寸化（サイズ指定サフィックス除去＋`name=orig`）する。他ホストのURLは変換しない。
- 保存名は`<post_id>_<連番2桁>.<拡張子>`。同名（拡張子違い含む）のファイルが既にあればHTTPアクセス自体を行わずスキップする（再実行の冪等性）。書き込みは`.part`一時ファイル経由でリネームする。
- 安全策：http/https以外のスキーム拒否、`Content-Type`が`image/`以外は保存しない、50MB上限超過で中断・一時ファイル削除。
- 終了コードは`0`=正常 / `1`=設定・環境エラー / `3`=レートリミット（429）検知。429検知時は即中断し、そこまでの保存分は残す（リトライしない）。

## 設定ファイル（`config.yaml`）

`watch_accounts`（追跡ハンドル）、`interest_keywords`（トレンド絞り込み・タグ付け用）、`max_items`、`wait_seconds.min/max`（スクロール間ランダム待機）、`output.raw_dir`/`state_dir`/`digest_dir`/`media_dir`、`session.storage_state_path`、`instagram.watch_accounts`/`instagram.storage_state_path`（Instagram収集用）、`threads.watch_accounts`/`threads.storage_state_path`（Threads収集用）、`browser`（既定chromium、不正値はchromiumにフォールバック＋警告）、`browser_executable_path`（brave使用時のみ）。全てプロジェクトルート相対パス（`browser_executable_path`のみ絶対パス）。

## 配置・Git管理について

配置場所未定のスタンドアロン構成で、相対パス完結・外部依存最小に作られている（将来の移動を想定）。`.gitignore`はセッションファイル・生データ・既読管理ファイルをパス指定なしのファイル名列挙（`*.jsonl`、`state/`、`digest/`等）で除外しており、将来の移動先でも同名パターンでそのまま機能する。
