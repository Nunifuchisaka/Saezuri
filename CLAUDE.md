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
python scripts/x_profiles.py                                      # watch_accountsのフォロワー数等プロフィール取得
python scripts/th_profiles.py                                     # threads.watch_accounts全件のフォロワー数等プロフィール取得
python scripts/trends.py                                          # トレンド取得
python scripts/bookmarks.py                                       # サブ垢自身のブックマーク取得
python scripts/related.py --url https://x.com/foo/status/123      # 起点ポストのリプライ・引用(人気順)+いいね数上位Markdown
python scripts/ig_related.py --url https://www.instagram.com/p/XXX/  # Instagram投稿のコメント+いいね数上位Markdown
python scripts/th_related.py --url https://www.threads.com/@foo/post/XXX  # Threads投稿のリプライ・引用+いいね数上位Markdown
python scripts/digest.py data/raw/*.jsonl --title "..."           # JSONL→Markdownダイジェスト生成
python scripts/media.py data/raw/related_20260725_003.jsonl       # 収集済みJSONLから画像ダウンロード
python scripts/ig_login.py                                        # Instagramの初回・失効時の手動ログイン
python scripts/ig_user_posts.py                                   # instagram.watch_accounts全件の投稿追跡
python scripts/th_login.py                                        # Threadsの初回・失効時の手動ログイン
python scripts/th_user_posts.py                                   # threads.watch_accounts全件の投稿追跡
python scripts/tt_login.py                                        # TikTokの初回・失効時の手動ログイン
python scripts/tt_user_posts.py                                   # tiktok.watch_accounts全件の投稿追跡(CAPTCHA検知で即中断)
python scripts/tt_profiles.py                                     # tiktok.watch_accounts全件のフォロワー数等プロフィール取得
```

`tests/`配下にpytestの単体テストが存在する（Playwright非依存の純粋なパース・正規化ロジックが対象。例: `tests/test_capture_quoted.py`は`capture.py`の引用元投稿抽出ロジック、`tests/test_instagram_parse.py`・`tests/test_threads_parse.py`は`instagram.py`・`threads.py`の投稿正規化ロジックを検証する。`tests/test_tiktok_parse.py`・`test_tt_profiles_parse.py`・`test_tt_user_posts.py`は`tiktok.py`・`tt_profiles.py`・`tt_user_posts.py`の正規化・CAPTCHA検知ロジックを合成データで検証する。起点投稿のリプライ・引用・コメント上位抽出は`test_ranking.py`(ランキング・Markdown)・`test_related.py`・`test_related_scripts_meta.py`(各スクリプトの純関数)・`test_capture_reply_to.py`(Xの`in_reply_to_id`)・`test_instagram_comments.py`(IGコメント・shortcode変換)・`test_threads_related.py`(Threads返信・引用一覧)・`test_capture_scroll.py`(スクロールJS差し替え)で検証する。テストのfixtureは実データを使わず、ユーザー名・本文・ID・URLを架空値にした合成データにすること）。実行は `pytest tests/`。一方、Playwrightを使った実ブラウザ操作を伴う部分（収集本体のスクレイピングロジック）は引き続き自動テスト化されておらず、手動QAで確認する運用である。

## 運用上の制約（コード変更時に踏まえること）

- **オンデマンド実行専用**：cron・タスクスケジューラ等の定期実行を導入するコードは書かない（利用規約抵触・BAN回避のための設計方針）。
- **自動ログイン厳禁**：ID/PWの自動入力ロジックは追加しない。`login.py`は毎回人間の手動ログインを待つ設計を維持する。
- **エラー時は自動リトライ・自動突破をしない**：レートリミット（429）・チャレンジ画面（CAPTCHA）検知時は即座に処理を中断し、検知時点までの収集済みデータを保存してから終了する。自動リトライループや自動突破コードは追加しない。

## アーキテクチャ

### 共通基盤（`scripts/`直下、収集モジュールからimportされる）

- **`session.py`**：`config.yaml`の読み込み、ブラウザ起動（chromium/firefox/webkit/brave対応。braveはchromiumエンジン+`executable_path`指定で起動）、`storage_state`（手動ログイン結果）を使った`BrowserContext`生成、`verify_login`/`require_login`によるログイン状態検証（失効時`SessionExpiredError`）。X側の入口は`open_x_context`（contextマネージャ。`--cdp`ありなら`connect_cdp`で管理人のChromeの既存contextを使い、`has_x_session_cookie`（`x.com`/`twitter.com`の`auth_token`が有効期限内か）→`verify_login`で検証、Chrome本体・contextは閉じずstorage_stateも読み書きしない。`context_kwargs`（viewport等）は既存contextに適用できないので無視してinfoログを出す。`--cdp`なしなら従来どおり`launch_browser`→`new_context`→`require_login`で、終了時に`browser.close()`）。接続先の決定は`get_x_cdp_url`（`--cdp` > `session.cdp_url` > None）。CDP時の失効メッセージは`login.py`ではなく「接続先ChromeでXに手動ログイン」を案内するため、`require_login`ではなく`verify_login`を直接使っている。
- **`storage.py`**：JSONLの追記・読み込み、`state/seen_ids.json`（グローバル既読ID）、`state/<account>_last_id.json`（アカウント別前回取得ID）の読み書きと差分判定（`filter_unseen`）。
- **`schema.py`**：`Post`／`TrendEntry`のdataclass定義。`to_dict()`/`from_dict()`でJSONLとの相互変換を行う（項目順・欠損キーの扱いはここに集約）。`Post.in_reply_to_id`（返信先の投稿ID、任意項目。`quoted_id`の直後に出力。キーが無い既存JSONLは`None`で読む）。
- **`capture.py`**：収集処理の中核。
  - `GraphQLCapture`：`page.on("response")`で対象オペレーション（`SearchTimeline`/`UserTweets`/`UserTweetsAndReplies`/`Bookmarks`、トレンド系は`ExplorePage`等を部分一致）のレスポンスを保持し、`drain()`時に初めて`response.json()`を読む（ハンドラ内での同期読み出しによる取りこぼし・デッドロックを避けるため）。
  - `parse_timeline_payload`/`parse_trends_payload`：GraphQLペイロードから`Post`/`TrendEntry`へ正規化。X側のレスポンス構造は非公開・無予告変更されるため、キー名を決め打ちせず「`instructions`という名のlistを再帰探索する」等の耐性を持たせている。想定キー欠如時は例外を投げず`logger.warning()`でそのエントリだけスキップし、全体は落とさない設計を踏襲すること。
  - `_parse_tweet_result`は投稿本体の正規化に加え、`result.article.article_results.result`（X Article＝長文記事へのリンクを含む投稿の場合に同梱される）を`_parse_article`で解析し`Post.article_title`/`Post.article_text`に格納する。新規GraphQLリクエストは発生しない（既に捕捉済みのレスポンスの解析のみ）。記事でない投稿では両方`None`のままで、これは異常系ではないため警告も出さない。`content_state.blocks[].text`を改行連結したプレーンテキストとして保存する（装飾・箇条書き記号は失われる）。
  - `scroll_and_collect`：スクロール→`drain()`→ランダム待機（`config.yaml`の`wait_seconds`）を、上限件数到達または新規アイテムなしが規定回数（既定3回）続くまで繰り返す。429検知で`RateLimitedError`、チャレンジ画面遷移で`ChallengeDetectedError`を送出。`scroll_script`引数でスクロールJSを差し替えられる（既定はウィンドウのスクロール。Instagramのコメント欄・Threadsのアクティビティのダイアログのように内部コンテナで追加読み込みするページ用）。
  - `GraphQLCapture.inject()`：レスポンス以外のペイロード（ページ埋め込みJSON・読み出し済みペイロード）を次の`drain()`の先頭に載せる。基底クラスにあるため`InstagramCapture`/`ThreadsCapture`/`TikTokCapture`もそのまま使える。
  - `_parse_tweet_result`は`legacy.in_reply_to_status_id_str`を`Post.in_reply_to_id`に格納する（X側の全収集モジュールのJSONLに入る）。

### `scripts/tweet.py`（単一ツイート取得）

URL/IDで指定した1件のツイートを`x.com/<handle>/status/<id>`（ハンドル不明時は`i/status/<id>`）で開き`TweetDetail`を傍受する。他の収集エントリポイントと異なり`scroll_and_collect`は使わず、指定IDのPostが見つかった時点で即座に打ち切る専用の`fetch_tweet`関数で完結させている（会話スレッド全体を集める`related.py`の①会話スレッド収集とは目的が異なるため独立させた）。`related.py`から`resolve_start`/`build_status_url`/`find_post`をimportして再利用する。結果は標準出力に表示しつつ`data/raw/tweet_<日付>_<連番3桁>.jsonl`に1件保存する。差分管理（state）は行わない。見つからなかった場合は終了コード1。

対象ツイートが引用ツイートの場合、TweetDetailペイロードに同梱される`quoted_status_result.result`から引用元投稿も追加リクエストなしで正規化し(`capture.py`の`find_tweet_result`/`parse_quoted_post`)、同じJSONLへ2件目のレコード(`source_query`は`quoted-by:<起点投稿id>`)として保存する。この挙動は`tweet.py`のみが対象で、`parse_timeline_payload`を使う他の収集モジュール(`search.py`等)には影響しない。

起点投稿・引用元投稿いずれかがX Articleへのリンクの場合、記事タイトル・本文も`Post.article_title`/`Post.article_text`として同時に保存・標準出力表示される。この解析自体は`capture.py`の`_parse_tweet_result`に組み込まれているため、`tweet.py`専用ではなく`search.py`等の収集モジュールで収集した投稿にも同様に適用される（引用元投稿の取得自体が`tweet.py`専用なのは変わらない）。標準出力の表示は`tweet.py`の`_print_post`ヘルパーに集約した（起点投稿・引用元投稿の表示コードの重複を解消）。

### `scripts/x_profiles.py`（アカウントのプロフィール・フォロワー数取得）

`x.com/<handle>`を1ハンドルにつき1回だけ開き（スクロールなし・リトライなし）、`UserByScreenName`を傍受して`XProfile`（フォロワー数・フォロー数・投稿数・表示名・プロフィール文等）へ正規化する。`watch_accounts`全件、または`--handle`指定。`parse_profile_payload`は「`rest_id`とscreen_nameを持つdict」を再帰探索で探し、2026-10時点の実機構造（`relationship_counts.followers/following`、`tweet_counts.tweets`、`core.name`、`profile_bio.description`）を主に、旧形式の`legacy.followers_count`等へフォールバックする。`legacy`配下にはフォロワー数が無いので注意。`UserByScreenName`だけを捕捉対象にする（サイドバー系のレスポンスには他人のフォロワー数が入るため捕捉しない）。`data/raw/x_profiles_<日付>.jsonl`に追記（差分管理なし）。429/チャレンジ検知時は取得済み分を保存して即中断し、終了コード契約は他と共通（取得できないアカウントがあれば1）。出力はPost/TrendEntryではないため`digest.py`には渡さない。


### `scripts/th_profiles.py`（Threadsアカウントのプロフィール・フォロワー数取得）

`threads.com/@<username>`を1ユーザーにつき1回だけ開き（スクロールなし・リトライなし）、ページ埋め込みJSONの`user`ノード（`username`と`follower_count`を持つdictを再帰探索）から`ThProfile`（フォロワー数・表示名・プロフィール文・認証済み・非公開・リンク）へ正規化する。`threads.watch_accounts`全件、または`--user`指定。フォロワー数は埋め込みJSONの`follower_count`が正確値（`followers_is_approx=False`、`followers_source=embedded_json`）。画面表示の「フォロワー4.3万人」は丸め表示なので、JSONに無い場合だけ`parse_follower_text`で表示テキストから概数に変換し`followers_is_approx=True`/`followers_source=page_text`で区別する。注意：`threads.py`の`load_embedded_payloads`は`text_post_app_info`を含むJSONしか返さず、投稿0件のアカウントではプロフィールJSONが落ちるため、本モジュールは`follower_count`を目印にした専用の読み出しを持つ。`data/raw/th_profiles_<日付>.jsonl`に追記（差分管理なし）。429/チャレンジ/失効検知時は取得済み分を保存して即中断し、終了コード契約は他と共通（取得できないアカウントがあれば1）。出力はPost/TrendEntryではないため`digest.py`には渡さない。

### 収集エントリポイント（`scripts/search.py` / `user_tweets.py` / `trends.py` / `bookmarks.py`）

各スクリプトは同一パターンで構成される：
1. `load_config` → `get_x_cdp_url`で`--cdp`を解決 → `open_x_context`（storage_state方式なら`launch_browser`（`--headed`未指定ならheadless）→ `new_context`（`storage_state`ロード。未ログインなら`FileNotFoundError`）。CDP方式なら既存Chromeに接続、接続失敗は`CdpConnectionError`で終了コード1）
2. `open_x_context`の中でセッション検証（失効なら`SessionExpiredError`）。`tweet.py`/`related.py`/`x_profiles.py`も同じ入口を使う。`context.new_page()`で開いたページは必ず`try/finally`で`page.close()`する（CDP接続では`browser.close()`でタブが片付かず、管理人のChromeにタブが溜まるため）
3. `GraphQLCapture`を対象オペレーションで生成・`attach`
4. `scroll_and_collect`で収集ループを回す（parse関数内で外側の`buffer`にも溜めておき、例外中断時も収集済み分を保存できるようにする、というパターンを踏襲すること）
5. 収集結果を`storage`経由でJSONL保存・既読/差分状態を更新
6. 終了コードは共通契約：`0`=正常 / `1`=設定・環境エラー / `2`=セッション失効 / `3`=レートリミット検知 / `4`=チャレンジ画面検知（3・4でも検知時点までの収集分は保存してから終了する）

保存先・差分管理の違い：
- `search.py`：実行ごとに新規ファイル`data/raw/search_<日付>_<連番3桁>.jsonl`。既読IDは`state/seen_ids.json`でグローバル管理。
  - 検索演算子用オプション`--from HANDLE`（`@`除去）・`--since`/`--until`（YYYY-MM-DD。`--since`は`--since-days`より優先）。`--split-days N`は`--since`〜`--until`（省略時は明日まで）をN日幅の窓に分割し、1ブラウザセッション内で窓ごとに順次検索して1つのJSONLに保存する（`--max`は窓ごとの上限、窓間はwait_seconds範囲のランダム待機、各窓の取得件数をログ出力、429/チャレンジ検知時は取得済み分を保存して即中断）。クエリ組み立て・窓分割は純粋関数（`build_query`/`split_windows`/`resolve_date_range`、`tests/test_search_query.py`）。
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

### TikTok収集（`scripts/tiktok.py` / `tt_login.py` / `tt_user_posts.py` / `tt_profiles.py`）

Instagram・Threadsと同じ方針・同じ終了コード契約。**実機未確認**（APIのURL・JSONキーは公開情報・既知の構造に基づく想定。管理人のログイン後に実機で突き合わせること）。

- **`tiktok.py`**（共通基盤）：TikTok用storage_state（`tiktok.storage_state_path`）のロードと`sessionid` Cookie（有効期限つき）によるログイン検証（`require_tiktok_login`）。TikTokは未ログインでも閲覧できCAPTCHAリスクがあるため、検証用のページ遷移はせずCookieのみで判定する。`TikTokCapture`は`InstagramCapture`を継承し、捕捉対象は`/api/post/item_list/`（投稿一覧）と`/api/user/detail/`（プロフィール）。**CAPTCHAは「`/captcha/`配信への通信」「`/captcha`URL遷移」「`CAPTCHA_SELECTORS`のDOM要素」のいずれかで検知して`ChallengeDetectedError`**（突破・回避コードは書かない）。ログイン画面遷移は`SessionExpiredError`、429は`RateLimitedError`。
- **`parse_tiktok_payload`**：`id`+`createTime`+`video`/`imagePost`/`stats`を持つdictを再帰探索で拾う（内側には潜らない）。`like_count`＝`diggCount`、`reply_count`＝`commentCount`、`repost_count`＝`shareCount`、`quote_count`＝0、`text`＝`desc`、`lang`＝`textLanguage`。`stats`が無ければ`statsV2`（文字列）を読む。メディアはフォトモード（`imagePost.images`）なら各画像を`photo`、それ以外は`originCover`>`cover`>`dynamicCover`の最初の1件を`photo`＋`playAddr`/`downloadAddr`を`video`。再生数・保存数はPostスキーマに無いため保持しない（`schema.py`は拡張しない方針）。`author.uniqueId`が無い投稿は警告してスキップ。
- **`tt_login.py`** / **`tt_user_posts.py`**：`th_login.py`/`th_user_posts.py`と同構成（埋め込みJSON注入は無し）。`filter_by_author`は`ig_user_posts.py`から流用。`data/raw/tiktok_posts_<日付>.jsonl`に追記し、`state/tiktok_<ユーザー名>_last_id.json`で差分管理。
- **`tt_profiles.py`**：`th_profiles.py`相当。埋め込みJSON（`userInfo`）または`user_detail`APIからフォロワー数・フォロー数・いいね合計（`heartCount`）・動画数を取り`data/raw/tt_profiles_<日付>.jsonl`に追記（差分管理なし）。概数フォールバック（画面テキスト解析）は実装していない。

**CDP接続（`--cdp`）**：X側の`search.py`/`user_tweets.py`/`trends.py`/`bookmarks.py`/`tweet.py`/`related.py`/`x_profiles.py`も`--cdp`（省略時は`config.yaml`の`session.cdp_url`、空ならstorage_state方式）で同じように接続できる（`login.py`がPlaywright起動のBraveで「自動操作中」扱いになりGoogleログインを拒否される対策）。入口は`session.py`の`open_x_context`、ログイン検証は`auth_token` Cookie＋`x.com/home`のリダイレクト判定（どちらかNGで終了コード2）。TikTokと同じ9222番のChrome（同じ専用プロファイル）を共用できる。`related.py`の人気順は既存contextにviewportを渡せないため、CDP時のみ`collect_thread`/`collect_timeline_relation`の`viewport`引数で開いたタブに`page.set_viewport_size(VIEWPORT)`をかける（storage_state方式は従来どおりcontextのviewport）。起動例: `chrome.exe --remote-debugging-port=9222 --user-data-dir=<専用プロファイル>`→Xにサブ垢で手動ログイン→`python scripts/tweet.py --url ... --cdp http://127.0.0.1:9222`。X側も実機未確認。TikTok側：`tt_user_posts.py`/`tt_profiles.py`は`--cdp http://127.0.0.1:9222`（省略時は`config.yaml`の`tiktok.cdp_url`、空ならstorage_state方式）で、管理人が自分で起動・ログイン済みのChromeに`connect_over_cdp`で接続できる(tt_login.pyの試行上限対策)。共通ヘルパーは`session.py`の`resolve_cdp_url`/`connect_cdp`/`CdpConnectionError`、TikTok側の入口は`tiktok.py`の`open_tiktok_context`(ログイン検証込みのcontextマネージャ)。既存の`contexts[0]`に新規タブを開いて使い、閉じるのはタブだけ(Chrome本体は閉じない・storage_stateは保存しない)。ログイン検証は`sessionid` Cookieの有効期限判定(無ければ終了コード2)、接続失敗は終了コード1。起動例: `chrome.exe --remote-debugging-port=9222 --user-data-dir=<専用プロファイル>`で起動→TikTokにログイン→`python scripts/tt_profiles.py --cdp http://127.0.0.1:9222`。**最近のChromeは既定プロファイルではデバッグポートが使えないため、`--user-data-dir`での専用プロファイル指定が必須。** CAPTCHA検知で即中断(終了コード4)・自動ログイン/突破/リトライなしの方針は不変。単体テストは`tests/test_cdp.py`。実機未確認。

### 起点投稿のリプライ・引用・コメント上位抽出（`scripts/related.py` / `ig_related.py` / `th_related.py` / `ranking.py`）

起点投稿1件に付いた反応を集めて全件JSONL保存し、起点への**直接の**返信/コメントと引用のうち**起点投稿者本人のものを除いて**、**いいね数のみ**の降順で上位N件（`--top`、既定10）をMarkdownに書き出す。SNS側の人気順表示で集め（並びはいいね数順と一致しないため）手元で並べ直す。人気順への切替は画面の並び替えメニュー操作かURLパラメータのみで行い、**GraphQLリクエスト変数の書き換え・route介入はしない**。切替に失敗した（メニューが見つからない等）場合は`logger.warning`を出して既定の並びのまま続行する。これは429/チャレンジ/ログイン画面遷移の扱い（即中断・収集済み分を保存して終了コード2/3/4）とは別物で、切替失敗の「警告して続行」をブロック検知に広げないこと。終了コード契約・ログイン方式は各SNSの既存スクリプトと同じ。

- **`ranking.py`**（Playwright非依存）：`rank_by_likes`（対象判定関数を満たし、起点自身・起点投稿者本人を除いた投稿をいいね数降順・同数は収集順）、`build_sections`（種別ごとの上位と対象件数）、`render_top_markdown`/`render_line`（順位・いいね数・投稿者・日時(JST)・本文先頭100字・URL。`pick.py`の`to_jst`/`_summary`を流用。digest.pyと同じ著作権配慮で全文は載せない）、`top_markdown_path`（`output.digest_dir`/`top_<sns>_<起点>_<日付>.md`、同日再実行は上書き）。対象判定は`source_query`ではなく構造（`in_reply_to_id`/`quoted_id`等）で行う（`merge_posts`は初出の種別しか残さないため）。
- **`related.py`（X）**：既定`--sort popular`。①会話スレッドは`switch_reply_sort_to_likes`で並び替えメニュー（primaryColumn内の`aria-haspopup="menu"`かつ`data-testid`無しで、テキストが「関連性が高い/最新/いいね」（英語UIのRelevant/Recent/Likesは未確認の想定値）のボタン）→`#layers`の`menuitem`「いいね」を選ぶ（`rankingMode: Likes`になる）。「いいね」を押す直前に関連度順の初回分を`drain()`で退避し（いいね順の取り直しに起点も含まれるため捨てる）、切替失敗時は`inject()`で戻す。②引用は検索の`f=top`と`f=live`をそれぞれ`--max`件まで集めIDで合算（popular時のスクロールは1回でページ末尾まで送る`QUOTE_SEARCH_SCROLL_SCRIPT`。既定の2画面分では20件分の末尾に届かず次ページが来ないまま止まるため）。直接リプライは`in_reply_to_id == 起点ID`、引用は`quoted_id == 起点ID`。`--sort recent`は従来互換（①既定の並び・②`f=live`のみ・Markdownは出さない）。③`--author`は従来どおりでランキング対象外。popular時のみBrowserContextのviewportを1280x1000にする（実機調査と同条件）。`next_related_out_path`は`prefix`引数で`ig_related`/`th_related`にも使う。
- **`ig_related.py`（Instagram）**：投稿ページ`/p/<shortcode>/`を開き、ページ埋め込みJSON（目印`xdt_api__v1__media__`。`instagram.load_embedded_json`）から起点投稿（投稿者）と初回コメントを取り、`InstagramCapture`に`inject`してから、コメント欄の内部スクロールコンテナを送る`COMMENT_SCROLL_SCRIPT`で`scroll_and_collect`する（追加分は`/api/graphql`の`PolarisPostCommentsPaginationQuery`）。Web版に並び替えUIは無いので既定（popular）のまま。`instagram.parse_instagram_comments`がコメントノード（`pk`+`created_at`+`comment_like_count`）を`Post`化：`url`=`/p/<code>/c/<pk>/`、`like_count`=`comment_like_count`、`reply_count`=`child_comment_count`、トップレベル（`parent_comment_id`がnull）は`in_reply_to_id`=起点のmedia pk（コメントノードに無いため補う）、子コメントは`parent_comment_id`。子コメントを展開する追加リクエストはしない。`source_query`=`ig-related:<code>:comment`、`data/raw/ig_related_<日付>_<連番>.jsonl`。shortcode⇔pk変換は`instagram.shortcode_to_pk`/`pk_to_shortcode`（64進数）。
- **`th_related.py`（Threads）**：投稿ページを`?sort_order=top`で開き、`ensure_reply_sort_top`で並び替えボタン（`[role=button][aria-haspopup=menu]`のテキスト「トップ/最近」）を確認、「トップ」でなければメニューのリンク（`a[role=menuitem]`）で選び直す（ページ遷移する）。返信は埋め込みJSON＋スクロール時の`BarcelonaPostPageDirectRepliesRefetchQuery`。`threads.parse_threads_replies`は`is_reply`の投稿だけを返し、`reply_level == 1`かつ`reply_to_author`が起点投稿者なら`in_reply_to_id`=起点pk（ペイロードに親IDが無いため補う）、それ以外は`None`。`target_handle`は渡さない（返信が`is_repost`扱いになるため）。引用は「アクティビティを見る」→ダイアログ内の「引用」タブ（文言は件数込み「引用\n1」。「フォローする」ボタン等を押さないよう文言で限定）→`BarcelonaFeedbackHubTabQuery`（ダイアログを開いた時の`post_activity`の`icon_name=quote`も拾う）を`threads.parse_feedback_quote_items`で`ThreadsQuoteRef`（`destination`の`id=<pk>_…&shortcode=<code>`と`actor.username`。shortcodeが無ければpkから変換）にし、引用投稿を1件ずつ開いて（逐次・各アクセス前に`wait_seconds`範囲のランダム待機・`--max`件まで）`find_threads_post`で`Post`化する。「アクティビティを見る」が無ければ警告して引用はスキップ。操作後は必ず`raise_if_blocked`でブロック検知する。`source_query`=`th-related:<code>:reply|quote`、`data/raw/th_related_<日付>_<連番>.jsonl`。
- **TikTokは未対応（2026-10-09見送り）**：CDP接続のChromeで動画ページ（`/@<user>/video/<id>`）を自動操作の新規タブから開くと本文なしのHTTP 403で弾かれた（同じChrome・同じURLを通常起動で開くと正常表示）。そのためコメントAPIの構造は未採取。再開する場合は「既存タブに接続して通信だけ見る」方式の検討から。

### `pick.py`（収集系と非依存）

収集済み`data/raw/*.jsonl`（複数・ワイルドカード可）から投稿を絞り込んでMarkdown（月見出し→日付(JST)・エンゲージメント・本文先頭100字・URL）またはJSONLで出力する。Playwright非依存でXへアクセスしない。`--handle`、`--keyword`（複数はOR・大文字小文字無視、text/記事本文）、`--since`/`--until`（`created_at`の"Mon Sep 21 03:28:32 +0000 2026"形式をパースしJST基準・両端含む）、`--exclude-reposts`、`--kind reply|quote|comment`（関連収集の種別。`source_query`が`related:`/`ig-related:`/`th-related:`で始まるものの末尾で判別する`post_kind`。複数はOR）、`--top-per month|none`＋`--top N`（エンゲージメント上位）、`--format md|jsonl`。id重複は除去。テストは`tests/test_pick.py`。

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

`watch_accounts`（追跡ハンドル）、`interest_keywords`（トレンド絞り込み・タグ付け用）、`max_items`、`wait_seconds.min/max`（スクロール間ランダム待機）、`output.raw_dir`/`state_dir`/`digest_dir`/`media_dir`、`session.storage_state_path`、`instagram.watch_accounts`/`instagram.storage_state_path`（Instagram収集用）、`threads.watch_accounts`/`threads.storage_state_path`（Threads収集用）、`tiktok.watch_accounts`/`tiktok.storage_state_path`（TikTok収集用）、`browser`（既定chromium、不正値はchromiumにフォールバック＋警告）、`browser_executable_path`（brave使用時のみ）。全てプロジェクトルート相対パス（`browser_executable_path`のみ絶対パス）。

## 配置・Git管理について

配置場所未定のスタンドアロン構成で、相対パス完結・外部依存最小に作られている（将来の移動を想定）。`.gitignore`はセッションファイル・生データ・既読管理ファイルをパス指定なしのファイル名列挙（`*.jsonl`、`state/`、`digest/`等）で除外しており、将来の移動先でも同名パターンでそのまま機能する。
