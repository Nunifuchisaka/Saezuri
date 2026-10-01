# saezuri

X（旧Twitter）のキーワード検索・アカウント追跡・トレンド・ブックマークをPlaywrightで収集し、JSONLへ保存した上でMarkdownダイジェストを生成するツール。Instagram・Threadsの特定アカウントの投稿収集にも対応する。

## 目次

- [概要](#概要)
- [利用規約リスクについて（必読）](#利用規約リスクについて必読)
- [収集専用サブアカウントについて](#収集専用サブアカウントについて)
- [セットアップ](#セットアップ)
- [使い方](#使い方)
  - [login.py（初回ログイン）](#loginpy初回ログイン)
  - [search.py（キーワード／ハッシュタグ検索）](#searchpyキーワードハッシュタグ検索)
  - [user_tweets.py（特定アカウント投稿追跡）](#user_tweetspy特定アカウント投稿追跡)
  - [x_profiles.py（アカウントのフォロワー数等取得）](#x_profilespyアカウントのフォロワー数等取得)
  - [tweet.py（単一ツイート取得）](#tweetpy単一ツイート取得)
  - [trends.py（トレンド把握）](#trendspyトレンド把握)
  - [bookmarks.py（サブ垢自身のブックマーク収集）](#bookmarkspyサブ垢自身のブックマーク収集)
  - [digest.py（Markdownダイジェスト生成）](#digestpymarkdownダイジェスト生成)
  - [media.py（収集済みJSONLからの画像ダウンロード）](#mediapy収集済みjsonlからの画像ダウンロード)
  - [Instagram収集（ig_login.py／ig_user_posts.py）](#instagram収集ig_loginpyig_user_postspy)
  - [Threads収集（th_login.py／th_user_posts.py）](#threads収集th_loginpyth_user_postspy)
- [設定ファイル（config.yaml）](#設定ファイルconfigyaml)
- [出力ファイルについて](#出力ファイルについて)
- [トラブルシューティング](#トラブルシューティング)
- [既知の制約](#既知の制約)
- [運用上の注意](#運用上の注意)
- [配置場所・Git管理について](#配置場所git管理について)

## 概要

saezuriは、VRChat動画台本・note記事のネタ・リサーチ用に、X上のキーワード検索・特定アカウント投稿・トレンド・ブックマークを収集するためのツール。

公式X APIは有料のため採用せず、Playwrightによるブラウザ自動操作でXの内部GraphQL APIレスポンスを傍受する方式（スクレイピング）を取っている。取得したデータはJSONLとして保存し、`digest.py`でMarkdownダイジェストに整形する。台本・記事本体への取捨選択・執筆は本ツールの範囲外で、既存のvrchat-video-script／note-vrchat-guideスキル側でClaudeが行う想定。

## 利用規約リスクについて（必読）

本ツールはスクレイピングでXの内部APIにアクセスするため、**X利用規約への抵触・アカウント凍結（BAN）が最大のリスク**である。以下の緩和策を必ず守って運用すること。

- **オンデマンド実行のみ**：cron・タスクスケジューラ等による定期実行は行わない。台本・記事執筆の直前に手動で実行する。
- **収集専用サブアカウントを使用**：本垢は使わない（詳細は次項）。
- **自動ログインはしない**：ID/PWの自動入力は行わず、`login.py`で毎回人間が手動ログインする。
- **低頻度アクセス**：多くても1日1〜数回程度にとどめ、連続実行は避ける（詳細は[運用上の注意](#運用上の注意)を参照）。

これらを守っても凍結リスクをゼロにはできない。自己責任で利用すること。

## 収集専用サブアカウントについて

本垢ではなく、**収集専用に新規作成したサブアカウント**でログインして使うこと。本垢を使うと凍結時の被害が大きいため。

ただし収集専用サブ垢を使う場合、`bookmarks.py`で取得できるのは**ログイン中のサブ垢自身がブックマークしたもの**に限られる。本垢が実際にブックマークした投稿は取得できない（Xの仕様上、他アカウントのブックマークは参照不可のため）。ブックマーク収集を使う場合は、サブ垢側でも同じ投稿をブックマークし直す運用が必要になる。

## セットアップ

初回のみ、以下の手順で環境を作る（コマンドはすべてプロジェクトルートで実行）。

1. Python 3.11の仮想環境を作成する
   ```
   py -3.11 -m venv .venv
   ```
2. 仮想環境を有効化する（PowerShellの場合）
   ```
   .venv\Scripts\Activate.ps1
   ```
3. 依存パッケージをインストールする
   ```
   pip install -r requirements.txt
   ```
4. Playwright用のChromiumをインストールする
   ```
   playwright install chromium
   ```
   `config.yaml`の`browser`で`firefox`／`webkit`を使う場合は、それぞれ`playwright install firefox` / `playwright install webkit`も実行する。`brave`を使う場合、追加インストールは不要（Chromiumエンジンをそのまま使うため）だが、事前にBraveブラウザ本体をインストールしておくこと。
5. `config.yaml`を編集する（`watch_accounts`＝追跡したいXハンドル、`interest_keywords`＝トレンド絞り込み・タグ付けに使う関心キーワード、取得上限・待機秒・出力先パス、起動するブラウザエンジン（`browser`）など。詳細は[設定ファイル](#設定ファイルconfigyaml)を参照）
6. 手動ログインを実行する
   ```
   python scripts/login.py
   ```
   ブラウザが起動するので、そこでサブ垢に手動でログインする（最大5分待機）。ログインを検知すると`session/x_storage_state.json`にセッションが保存され、ブラウザが自動で閉じる。以降の収集コマンドはこのセッションを再利用する。

## 使い方

各コマンドはプロジェクトルートから実行する（`config.yaml`等を相対パスで参照するため）。共通の終了コードは以下の通り。

| 終了コード | 意味 |
|---|---|
| 0 | 正常終了 |
| 1 | 設定・環境エラー（`config.yaml`不備、`login.py`未実行等） |
| 2 | セッション失効（`login.py`を再実行して手動ログインし直す） |
| 3 | レートリミット（429）検知 |
| 4 | チャレンジ画面（CAPTCHA等）検知 |

終了コード3・4の場合も、検知時点までに収集済みの分はJSONLへ保存してから終了する。

### login.py（初回ログイン）

```
python scripts/login.py
```

| オプション | 説明 |
|---|---|
| `--config` | 設定ファイルのパス（既定: `config.yaml`） |
| `--browser` | 起動するブラウザエンジン（`chromium`／`firefox`／`webkit`／`brave`。既定: `config.yaml`の`browser`） |

`headless=False`で指定ブラウザ（既定: `config.yaml`の`browser`、既定値`chromium`）を起動し、ユーザーが手動でサブ垢にログインするのを待つ（最大5分）。`x.com/home`への遷移を検知したら`storage_state`（Cookie＋localStorage）を`config.yaml`の`session.storage_state_path`（既定: `session/x_storage_state.json`）へ保存する。ID/PWの自動入力は行わない。

**注意**：`login.py`と収集スクリプト（`search.py`等）で異なるブラウザエンジンを使うと、`storage_state`を保存したエンジンと読み込むエンジンでフィンガープリントが変わりbot検知リスクが上がるため、同じ`--browser`（またはconfig.yamlの`browser`）を使うこと。`firefox`／`webkit`を使う場合は事前に`playwright install firefox` / `playwright install webkit`が必要。

### search.py（キーワード／ハッシュタグ検索）

`x.com/search?q=<クエリ>&f=live|top`を開き、`SearchTimeline`オペレーションのレスポンスを傍受する。

```
python scripts/search.py --query "VRChat アップデート" --mode live --max 100
python scripts/search.py --query "#VRChat" --since-days 7
```

| オプション | 説明 |
|---|---|
| `--query`（必須） | 検索クエリ（ハッシュタグ・検索演算子も可） |
| `--mode` | `live`（最新）／`top`（話題）。既定: `live` |
| `--since-days N` | N日前以降に絞る（クエリに`since:YYYY-MM-DD`を付加） |
| `--max` | 取得上限件数（省略時は`config.yaml`の`max_items`） |
| `--config` | 設定ファイルのパス（既定: `config.yaml`） |
| `--browser` | 起動するブラウザエンジン（`chromium`／`firefox`／`webkit`／`brave`。既定: `config.yaml`の`browser`） |
| `--headed` | ブラウザを可視状態で起動する（デバッグ用） |

結果は実行ごとに`data/raw/search_<日付>_<連番3桁>.jsonl`という新規ファイルに保存する（同日に複数回実行してもキーワードごとにファイルが分離され、連番はその日の実行回数でカウントする）。既読IDは`state/seen_ids.json`でグローバル管理する（同一IDは再保存しない）。

### user_tweets.py（特定アカウント投稿追跡）

`config.yaml`の`watch_accounts`に列挙したハンドルそれぞれについて`x.com/<handle>`を開き、`UserTweets`（`--include-replies`指定時は`UserTweetsAndReplies`）を傍受する。

```
python scripts/user_tweets.py                     # watch_accounts全件
python scripts/user_tweets.py --handle some_user   # 特定の1アカウントのみ
python scripts/user_tweets.py --include-replies
```

なお、単一ツイートをURL/IDから直接1件だけ取得したい場合は[tweet.py](#tweetpy単一ツイート取得)を使う。

| オプション | 説明 |
|---|---|
| `--handle` | 特定の1アカウントだけ実行（`@`は不要。省略時は`watch_accounts`全件） |
| `--include-replies` | 返信も含めて収集する（`UserTweetsAndReplies`を傍受） |
| `--max` | 1アカウントあたりの取得上限件数（省略時は`config.yaml`の`max_items`） |
| `--config` | 設定ファイルのパス（既定: `config.yaml`） |
| `--browser` | 起動するブラウザエンジン（`chromium`／`firefox`／`webkit`／`brave`。既定: `config.yaml`の`browser`） |
| `--headed` | ブラウザを可視状態で起動する（デバッグ用） |

結果は`data/raw/user_tweets_<日付>.jsonl`に追記する。ハンドルごとに`state/<handle>_last_id.json`へ前回取得済み最新IDを保存し、それより新しいIDの投稿のみを差分保存する。レートリミット・チャレンジ検知時はその時点までの収集分を保存し、残りのアカウントは処理しない。

### x_profiles.py（アカウントのフォロワー数等取得）

`config.yaml`の`watch_accounts`（または`--handle`）の各アカウントについて`x.com/<handle>`を1回だけ開き、`UserByScreenName`を傍受してフォロワー数・フォロー数・投稿数・表示名・プロフィール文等を取得する。スクロールや再アクセスはしない。

```
python scripts/x_profiles.py                     # watch_accounts全件
python scripts/x_profiles.py --handle some_user   # 特定の1アカウントのみ
```

結果は標準出力に表示し、`data/raw/x_profiles_<日付>.jsonl`に追記する（差分管理なし。実行ごとの時点スナップショット）。このJSONLはポストではないため`digest.py`には渡さない。レートリミット・チャレンジ検知時はそこまでの取得分を保存して中断する（終了コードは他スクリプトと共通。取得できないアカウントがあった場合は1）。

### tweet.py（単一ツイート取得）

URLまたはIDで指定した1件のツイートを`x.com/<handle>/status/<id>`（ハンドル不明時は`x.com/i/status/<id>`）で開き、`TweetDetail`オペレーションのレスポンスを傍受する。会話スレッド全体やリプライは集めず、指定した1件が見つかった時点で打ち切る（スレッド全体・引用・同一投稿者の他投稿も含めて集めたい場合は`related.py`を使う）。

```
python scripts/tweet.py --url https://x.com/foo/status/1234567890
python scripts/tweet.py --id 1234567890
```

| オプション | 説明 |
|---|---|
| `--url` | 対象ツイートのURL（`--id`とどちらか必須） |
| `--id` | 対象ツイートの数値ID（`--url`とどちらか必須） |
| `--max-rounds` | 起点ツイートが見つかるまでのドレイン試行回数の上限（スクロールはしない。既定: 5） |
| `--config` | 設定ファイルのパス（既定: `config.yaml`） |
| `--browser` | 起動するブラウザエンジン（`chromium`／`firefox`／`webkit`／`brave`。既定: `config.yaml`の`browser`） |
| `--headed` | ブラウザを可視状態で起動する（デバッグ用） |

本文・投稿者・エンゲージメント数を標準出力に表示し、`data/raw/tweet_<日付>_<連番3桁>.jsonl`に1件保存する。対象ツイートが見つからなかった場合（削除・非公開・鍵アカウント等）は終了コード`1`でエラーメッセージを表示する。

対象ツイートが引用ツイートの場合、`quoted_status_result`として同梱されている引用元投稿も追加リクエストなしで同時に取得し、同じJSONLファイルに2件目のレコード(`source_query`は`quoted-by:<起点投稿id>`)として保存する。標準出力にも起点投稿に続けて引用元投稿を表示する。引用元が削除済み・非公開等で取得できない場合はエラーにせず、起点投稿のみを保存して正常終了する。

### trends.py（トレンド把握）

`x.com/explore/tabs/for-you`を開き、トレンド系オペレーション（`ExplorePage`等、名称の変動を考慮し緩めにキーワード判定）を傍受する。

```
python scripts/trends.py               # interest_keywordsで絞り込み(既定)
python scripts/trends.py --no-filter   # 全件
```

| オプション | 説明 |
|---|---|
| `--filter` / `--no-filter` | `config.yaml`の`interest_keywords`を含むトレンドだけに絞る。既定: `--filter`（ON） |
| `--max` | 取得上限件数（省略時は`config.yaml`の`max_items`） |
| `--config` | 設定ファイルのパス（既定: `config.yaml`） |
| `--browser` | 起動するブラウザエンジン（`chromium`／`firefox`／`webkit`／`brave`。既定: `config.yaml`の`browser`） |
| `--headed` | ブラウザを可視状態で起動する（デバッグ用） |

結果は`data/raw/trends_<日付>.jsonl`に追記する。`interest_keywords`が空の場合は絞り込みをせず全件保存する（警告メッセージを表示）。

### bookmarks.py（サブ垢自身のブックマーク収集）

`x.com/i/bookmarks`を開き、`Bookmarks`オペレーションを傍受する。**取得できるのはログイン中のサブ垢自身がブックマークしたものに限る**（本垢のブックマークは対象外。[収集専用サブアカウントについて](#収集専用サブアカウントについて)を参照）。

```
python scripts/bookmarks.py
```

| オプション | 説明 |
|---|---|
| `--max` | 取得上限件数（省略時は`config.yaml`の`max_items`） |
| `--config` | 設定ファイルのパス（既定: `config.yaml`） |
| `--browser` | 起動するブラウザエンジン（`chromium`／`firefox`／`webkit`／`brave`。既定: `config.yaml`の`browser`） |
| `--headed` | ブラウザを可視状態で起動する（デバッグ用） |

結果は`data/raw/bookmarks_<日付>.jsonl`に追記する。`state/bookmarks_last_id.json`へ前回取得済み最新IDを保存し、差分取得する（差分判定の制約は[既知の制約](#既知の制約)を参照）。

### digest.py（Markdownダイジェスト生成）

`data/raw/*.jsonl`を読み込み、人が読むためのMarkdownダイジェストを生成する。Xへの実アクセスは行わない（JSONL読み込みと文字列整形のみ）。

```
python scripts/digest.py data/raw/search_20260718_001.jsonl --title "VRChat アップデート"
python scripts/digest.py data/raw/*.jsonl --output digest/my_digest.md
```

| オプション | 説明 |
|---|---|
| `inputs`（必須・可変長） | 入力JSONLファイルパス（複数指定可、globパターン可） |
| `--output` | 出力Markdownファイルパス（省略時は`config.yaml`の`output.digest_dir`配下に日時付きファイル名で自動生成） |
| `--title` | frontmatterの`source`やドキュメント見出しに使う説明（例: 検索クエリ名） |
| `--config` | 設定ファイルのパス（既定: `config.yaml`） |

入力の各行は`trend_word`キーの有無でトレンド、`id`＋`author_handle`キーの有無でポストと判定するため、種別の異なるJSONLを混在させて渡すこともできる。終了コードは`0`＝正常、`1`＝入力ファイルが見つからない、のいずれか。

### media.py（収集済みJSONLからの画像ダウンロード）

収集済みの`data/raw/*.jsonl`に記録された画像URLから、画像を直接ダウンロードして保存する。**入力は収集済みJSONLであり、X本体（x.com）へは再アクセスしない**（記録済みの画像CDN URLへHTTP GETするだけ）。そのため新たに収集し直したい場合は先に`search.py`等を実行しておくこと。

```
python scripts/media.py data/raw/related_20260725_003.jsonl
python scripts/media.py data/raw/*.jsonl --out data/media/foo --max 50 --dry-run
```

| オプション | 説明 |
|---|---|
| `inputs`（必須・可変長） | 入力JSONLファイルパス（複数指定可、globパターン可） |
| `--out` | 保存先ディレクトリ（省略時は`config.yaml`の`output.media_dir`配下。入力が複数の場合は指定先の下に入力ファイルごとのサブディレクトリを作る） |
| `--max` | 対象にする画像の最大枚数（先頭からN枚。既存スキップ分も1枚と数える。省略時は無制限） |
| `--dry-run` | ダウンロードせず、対象URLと保存予定パスを一覧表示するだけ |
| `--config` | 設定ファイルのパス（既定: `config.yaml`） |

- **動画は対象外**：`media`のうち`type`が`photo`のものだけをダウンロードする。`video`／`animated_gif`は落とさず、スキップ件数をサマリーに出すだけ。
- 保存先は既定で`data/media/<入力JSONLのファイル名>/`、ファイル名は`<post_id>_<連番2桁>.<拡張子>`（連番はそのポスト内のメディアの並び順。拡張子はレスポンスの`Content-Type`から決める）。
- `pbs.twimg.com`のURLは原寸（`name=orig`）に変換して取得する（`:large`等のサイズ指定サフィックスは除去する）。
- **同名のファイルが既にある場合はダウンロードせずスキップする**ため、同じJSONLを何度流しても二重取得にならない。一時ファイル（`.part`）に書き切ってからリネームするので、中断しても壊れたファイルは残らない。
- 実行内で同一URLが複数ポストに現れた場合は1回だけ取得する。個別画像の取得失敗（404・接続エラー等）は警告を出してスキップし、全体は続行する。
- ダウンロードは逐次実行で、1枚ごとに0.5〜2.0秒のランダム待機を挟む（並列化・自動リトライはしない）。**429を受けた場合は即座に中断**し、終了コード`3`で終わる。
- 終了コードは`0`＝正常、`1`＝入力ファイルが見つからない・保存先が作れない、`3`＝レートリミット（429）検知、のいずれか。

### Instagram収集（ig_login.py／ig_user_posts.py）

Instagramの特定アカウントの投稿を、X側と同じ方式（手動ログインしたセッションの再利用＋内部APIレスポンスの傍受）で収集する。公式Instagram Graph APIは自社ビジネスアカウント以外の投稿本文・反応数を取得できないため採用していない。

**Instagramも利用規約でスクレイピングを禁止しており、X以上にbot検知・アカウント制限が厳しい。**[利用規約リスクについて](#利用規約リスクについて必読)の緩和策（オンデマンド実行・収集専用サブアカウント・手動ログイン・低頻度）をInstagramにも同様に適用すること。

1. 初回・失効時に手動ログインする（X用の`login.py`とはセッションが別。Instagramのサブ垢でログインする）
   ```
   python scripts/ig_login.py
   ```
   ブラウザでInstagramに手動ログインすると、`sessionid` Cookieの付与を検知して`config.yaml`の`instagram.storage_state_path`（既定: `session/instagram_storage_state.json`）へ保存する（最大5分待機）。ID/PWの自動入力は行わない。
2. `config.yaml`の`instagram.watch_accounts`に追跡したいユーザー名を列挙し、収集する
   ```
   python scripts/ig_user_posts.py                     # instagram.watch_accounts全件
   python scripts/ig_user_posts.py --user some_brand   # 特定の1アカウントのみ
   ```

| オプション（ig_user_posts.py） | 説明 |
|---|---|
| `--user` | 特定の1アカウントだけ実行（`@`は不要。省略時は`instagram.watch_accounts`全件） |
| `--max` | 1アカウントあたりの取得上限件数（省略時は`config.yaml`の`max_items`） |
| `--config` | 設定ファイルのパス（既定: `config.yaml`） |
| `--browser` | 起動するブラウザエンジン（既定: `config.yaml`の`browser`。`ig_login.py`と同じ値を使うこと） |
| `--headed` | ブラウザを可視状態で起動する（デバッグ用） |

- `instagram.com/<ユーザー名>/`を開いてスクロールし、プロフィール・フィード系の内部API（`web_profile_info`、`graphql/query`等）のレスポンスから投稿を抽出する。スクロール間の待機・上限件数・新規なし3回での停止はX側と共通（`wait_seconds`・`max_items`）。
- 結果は`data/raw/instagram_posts_<日付>.jsonl`に追記する。ポストの共通スキーマで保存し、`url`は`https://www.instagram.com/p/<ショートコード>/`、`text`はキャプション、`like_count`はいいね数（非表示設定の投稿は0）、`reply_count`はコメント数、`repost_count`／`quote_count`は常に0、`source_query`は`instagram:<ユーザー名>`。カルーセル投稿は`media`に全画像・動画を並べる。ストーリーズ（ハイライト含む）は対象外。
- 作者が対象アカウント本人の投稿のみ保存する（プロフィールページのサイドバーのおすすめ・広告等に含まれる他人の投稿は除外する。大文字小文字は区別しない）。
- 差分管理は`state/instagram_<ユーザー名>_last_id.json`（InstagramのメディアIDは投稿時刻順に増えるため、X側と同じくID大小比較で新規判定する）。
- 共通スキーマなので、`digest.py`・`media.py`にそのまま渡せる（`media.py`は`photo`のみダウンロードする。InstagramのCDN URLは署名付きで時間が経つと失効するため、収集後早めに実行すること）。
- 終了コードはX側と同じ契約（`0`＝正常／`1`＝設定・環境エラー／`2`＝セッション失効／`3`＝レートリミット（429）／`4`＝チャレンジ画面・確認要求）。3・4（および収集途中の失効による2）でも検知時点までの収集分は保存してから終了し、残りのアカウントは処理しない。
- キーワード・ハッシュタグ検索には対応していない（Instagram側のハッシュタグページは仕様変更が多く、安定した傍受対象がないため）。

### Threads収集（th_login.py／th_user_posts.py）

Threadsの特定アカウントの投稿を、Instagram収集と同じ方式（手動ログインしたセッションの再利用＋内部APIレスポンスの傍受）で収集する。公式Threads APIは原則として自分のアカウントの投稿しか扱えないため採用していない。ThreadsもMetaの利用規約でスクレイピングを禁止しているため、[利用規約リスクについて](#利用規約リスクについて必読)の緩和策を同様に適用すること。

1. 初回・失効時に手動ログインする（X用・Instagram用とはセッションが別。Threads（Instagramアカウント連携）のサブ垢でログインする）
   ```
   python scripts/th_login.py
   ```
   ブラウザでThreadsに手動ログインすると、`threads.com`の`sessionid` Cookieの付与を検知して`config.yaml`の`threads.storage_state_path`（既定: `session/threads_storage_state.json`）へ保存する（最大5分待機）。ID/PWの自動入力は行わない。
2. `config.yaml`の`threads.watch_accounts`に追跡したいユーザー名を列挙し、収集する
   ```
   python scripts/th_user_posts.py                     # threads.watch_accounts全件
   python scripts/th_user_posts.py --user some_brand   # 特定の1アカウントのみ
   ```

オプション（`--user`／`--max`／`--config`／`--browser`／`--headed`）は`ig_user_posts.py`と同じ（`--user`の省略時は`threads.watch_accounts`全件）。

- `threads.com/@<ユーザー名>`を開き、初回表示分はページに埋め込まれたJSONから、スクロールで追加読み込みされる分は内部API（`graphql/query`等）のレスポンスから投稿を抽出する。待機・上限件数・停止条件はX側と共通（`wait_seconds`・`max_items`）。
- 結果は`data/raw/threads_posts_<日付>.jsonl`に追記する。ポストの共通スキーマで保存し、`url`は`https://www.threads.com/@<投稿者>/post/<ショートコード>`、`like_count`はいいね数、`reply_count`は直接の返信数、`repost_count`／`quote_count`はリポスト数・引用数、`quoted_id`は引用元投稿のID、`source_query`は`threads:<ユーザー名>`。連投（スレッド）は1投稿ずつ別レコードになる。対象アカウントのプロフィールに並ぶ他人の投稿（リポスト）は`is_repost: true`になる。
- 差分管理は`state/threads_<ユーザー名>_last_id.json`（ID大小比較で新規判定）。
- `digest.py`・`media.py`にそのまま渡せる。終了コードの契約・ブロック検知時の挙動は`ig_user_posts.py`と同じ。
- キーワード検索・返信タブの収集には対応していない。

## 設定ファイル（config.yaml）

| キー | 内容 |
|---|---|
| `watch_accounts` | `user_tweets.py`が追跡対象にするXハンドルのリスト（`@`は付けない） |
| `interest_keywords` | トレンド絞り込み・ダイジェストのtagsに使う関心キーワードのリスト |
| `max_items` | 1回の収集ジョブで取得する最大件数の目安（各コマンドの`--max`未指定時に使用） |
| `wait_seconds.min` / `wait_seconds.max` | スクロール間のランダム待機秒数の範囲（機械的な連続アクセスを避けるため） |
| `output.raw_dir` | 生データ（JSONL）の出力先ディレクトリ（既定: `data/raw`） |
| `output.state_dir` | 既読ID・差分管理ファイルの出力先ディレクトリ（既定: `state`） |
| `output.digest_dir` | `digest.py`が生成するMarkdownダイジェストの出力先ディレクトリ（既定: `digest`） |
| `output.media_dir` | `media.py`がダウンロードした画像の出力先ディレクトリ（既定: `data/media`。この下に入力JSONLごとのサブディレクトリを作る） |
| `session.storage_state_path` | 手動ログイン結果（`storage_state`）の保存先（既定: `session/x_storage_state.json`） |
| `browser` | 起動するブラウザエンジン（`chromium`／`firefox`／`webkit`／`brave`。既定: `chromium`。不正値は`chromium`にフォールバックし警告を表示） |
| `instagram.watch_accounts` | `ig_user_posts.py`が追跡対象にするInstagramユーザー名のリスト（`@`は付けない） |
| `instagram.storage_state_path` | `ig_login.py`の手動ログイン結果の保存先（既定: `session/instagram_storage_state.json`） |
| `threads.watch_accounts` | `th_user_posts.py`が追跡対象にするThreadsユーザー名のリスト（`@`は付けない） |
| `threads.storage_state_path` | `th_login.py`の手動ログイン結果の保存先（既定: `session/threads_storage_state.json`） |
| `browser_executable_path` | `brave`使用時のみ必要。Brave実行ファイルの絶対パス。空／未指定なら一般的なインストール場所を自動探索する |

いずれもプロジェクトルートからの相対パスで指定する（`browser_executable_path`は絶対パス）。`firefox`／`webkit`を使う場合は事前に`playwright install firefox` / `playwright install webkit`が必要（[セットアップ](#セットアップ)参照）。`brave`使用時、自動検出（`C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe`等の一般的なインストール場所を順に確認）に失敗する場合は`browser_executable_path`に絶対パスを指定すること。

## 出力ファイルについて

**生データ（`data/raw/*.jsonl`）**：1行1レコードのJSONL。ポスト（`search.py`/`user_tweets.py`/`bookmarks.py`/`ig_user_posts.py`/`th_user_posts.py`）は以下の共通スキーマ。

```
id, url, author_handle, author_name, created_at, text, lang,
like_count, repost_count, reply_count, quote_count,
media(type/url配列), is_repost, quoted_id, source_query, collected_at
```

トレンド（`trends.py`）は別スキーマ。

```
trend_word, tweet_volume, rank, collected_at
```

アカウントのプロフィール（`x_profiles.py`）も別スキーマ。

```
handle, user_id, name, description, followers_count, following_count, tweet_count, media_count,
location, url, created_at, is_blue_verified, verified_type, protected, category, profile_url, collected_at
```

**既読・差分管理（`state/`）**

- `state/seen_ids.json`：`search.py`が使うグローバルな既読ID一覧
- `state/<handle>_last_id.json`：`user_tweets.py`がハンドルごとに保存する前回取得済み最新ID
- `state/bookmarks_last_id.json`：`bookmarks.py`が保存する前回取得済み最新ID
- `state/instagram_<ユーザー名>_last_id.json`：`ig_user_posts.py`がアカウントごとに保存する前回取得済み最新ID
- `state/threads_<ユーザー名>_last_id.json`：`th_user_posts.py`がアカウントごとに保存する前回取得済み最新ID

**画像（`media.py`の出力）**：既定で`data/media/<入力JSONLのファイル名>/<post_id>_<連番2桁>.<拡張子>`。`.gitignore`で`data/media/`を除外しているためリポジトリには含まれない。

**ダイジェスト（`digest.py`の出力Markdown）**：frontmatter（`type: reference`、`tags`、`source`＝検索条件やURL、`created`）に続き、`## 収集条件`（クエリ・期間・件数・収集日時）、`## 注目ポスト`（エンゲージメント合計順に本文要約100文字＋出典URL。著作権配慮のため全文転載はしない）、`## トレンド語`（言及数・rank）、生データJSONLへの相対パスリンク、という構成。

## トラブルシューティング

- **取得0件が続く**：Xの内部API構造変更が疑われる。コンソールの警告ログ（`instructionsが見つかりません(API構造変更の可能性)`等）を確認する。パーサーはベストエフォート実装のため、構造変更時は例外を投げず警告してスキップする設計になっている。
- **終了コード3（429／レートリミット検知）**：クールダウンして時間を置いてから再実行する。すぐに再実行しない。
- **終了コード2（セッション失効）**：`セッションが失効しています。login.pyを再実行して手動ログインし直してください。`と表示される。`python scripts/login.py`を再実行して手動ログインし直す。自動再ログインは行わない。Instagram側（`ig_user_posts.py`）の場合は`python scripts/ig_login.py`、Threads側（`th_user_posts.py`）の場合は`python scripts/th_login.py`を再実行する。
- **終了コード4（CAPTCHA／チャレンジ画面検知）**：自動突破は試みない設計。表示されたメッセージに従い、`--headed`オプションを付けて実行し直すなどしてブラウザで手動対応する。

## 既知の制約

- **本垢のブックマークは取得不可**：`bookmarks.py`はログイン中のサブ垢自身がブックマークしたものしか取得できない。
- **X内部API構造変更によるパーサー崩壊の可能性**：GraphQLのオペレーション名・レスポンス構造は非公開かつ予告なく変わる。キー名を決め打ちしない探索や警告スキップである程度耐性を持たせているが、大きな構造変更があれば追従できなくなる可能性がある。
- **Instagram内部API構造変更・取得範囲**：`ig_user_posts.py`も非公開の内部APIに依存するため、構造変更で取得0件になりうる（投稿らしいノードを再帰探索し、欠けた項目は警告スキップする設計）。ピン留めされた古い投稿は差分判定で新規扱いにならない。キーワード・ハッシュタグ検索は未対応。
- **Threads内部API構造変更・取得範囲**：`th_user_posts.py`も非公開の内部API・ページ埋め込みJSONに依存するため、構造変更で取得0件になりうる。リポストは元投稿のIDで記録されるため、古い投稿のリポストは差分判定で新規扱いにならない。キーワード検索・返信タブは未対応。
- **ブックマーク差分取得の抜け漏れ**：`bookmarks.py`の差分判定は投稿IDの大小比較で行っている。ブックマーク一覧はブックマークした順に並ぶため、IDの小さい（＝古い）投稿を後からブックマークした場合、前回取得済み最新IDより小さいと判定されて新規として拾われず、取得漏れが発生しうる。

## 運用上の注意

- 実行は**オンデマンドのみ**。定期実行（cron・タスクスケジューラ等）は設けない。
- 台本・記事執筆の直前に手動で実行する運用とし、**多くても1日1〜数回**にとどめる。
- 連続実行は避ける。

## 配置場所・Git管理について

本プログラムは配置場所未定のスタンドアロン構成であり、相対パス完結・外部依存最小で作られている。将来、現在の場所とは別のディレクトリに移動されることを想定している。

`.gitignore`では`x_storage_state.json`・`instagram_storage_state.json`・`threads_storage_state.json`・`seen_ids.json`・`*_last_id.json`・`*.jsonl`・`state/`など、セッションファイル・生データ・既読管理ファイルをパス指定なしのファイル名列挙で除外している。将来別の場所に移動しても同名パターンでの除外がそのまま機能する（`media.py`の保存先だけは既定パスの`data/media/`で除外している。`--out`で別の場所に保存する場合はその場所を各自で除外すること）。
