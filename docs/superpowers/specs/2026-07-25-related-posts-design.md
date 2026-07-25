# related.py 設計書：ひとつのPostから関連Postを収集する

- 日付: 2026-07-25
- 対象: saezuri（`E:\git\Saezuri`）
- 新規: `scripts/related.py`
- 変更: `scripts/capture.py`（`TweetDetail` オペレーション追加）

## 目的

起点となる1つのPost（URLまたはID指定）から、それに関連するPostを収集し、
実行ごとに新規JSONLへ保存する。台本・記事執筆は範囲外（既存方針どおり収集のみ）。

## 収集する「関連」3種

| 種別 | source_query の関係種別 | 取得方法 |
|---|---|---|
| ①会話スレッド（親＋リプライ） | `reply` | `x.com/i/status/<id>` を開き `TweetDetail` を傍受 |
| ②引用ポスト | `quote` | SearchTimeline を検索演算子 `quoted_tweet_id:<id>` で回す |
| ③同一投稿者の他の投稿 | `author` | UserTweets を起点Postの投稿者ハンドルで回す |

②③は既存の `SearchTimeline` / `UserTweets` 傍受ロジック（`GraphQLCapture` +
`scroll_and_collect` + `parse_timeline_payload`）をそのまま再利用する。

> 【2026-07-25 改定】当初は④「類似キーワード投稿」（本文からキーワード抽出→SearchTimeline）も
> 含む4種構成だったが、実機テストで日本語本文からの主題語抽出・クエリ組み立ての精度が出ず
> （助詞込み断片の誤抽出、固有名詞をANDすると起点自身しかヒットしない等）、
> 「品質が出ない補助機能は載せない」判断で④を削除し3種構成に確定した。

## 処理フロー

1. `--url`（または `--id`）で起点を受け取る。URLからはID・ハンドルを抽出、ID単体指定なら
   ハンドル不明でも `https://x.com/i/status/<id>` で詳細ページを開ける。
2. `TweetDetail` を傍受し **①会話スレッド**を取得。**この時点で起点Postの投稿者ハンドルも
   得られる**ので、③の材料（ハンドル）に使う。
   - 起点Post自身が payload に含まれるので、そこから `author_handle` を拾う。
3. 選択された関係種別について、②③を順に実行する。
   - ②: `quoted_tweet_id:<id>` で SearchTimeline。X側の演算子挙動は変わりうるため
     取得ゼロでも異常終了しない（warning のみ）。
   - ③: 起点ハンドルで UserTweets。watch_accounts に含まれるかは問わない（任意ハンドル可）。

## フラグ

- `--url` / `--id`（どちらか必須。両方指定はエラー）
- 関係選択: `--replies` / `--quotes` / `--author`
  - **いずれも未指定なら全部ON**。1つ以上指定した場合は指定分のみ。
- `--unseen-only`: グローバル既読フィルタをオプトインで有効化（後述）
- `--max` / `--config` / `--browser` / `--headed`（他スクリプト共通）

## 保存

- 出力先: `data/raw/related_<日付>_<連番3桁>.jsonl`（search.py と同じ実行ごと新規ファイル方式。
  `next_search_out_path` 相当の連番採番を related 用に用意する）
- 各レコードの `source_query` に `related:<起点id>:<関係種別>` を入れる
  （例 `related:1234567890:reply`）。どの起点・どの関係で取れたか後から区別できる。
- 重複排除: **その1回の実行の中だけ**（3種を集める過程で同じPostが複数種に出たら1件にまとめる）。
  種別をまたいで最初に出た種別の `source_query` を採用する。
- **起点Post自身は保存対象から除外**する（`--url`/`--id`で指定したidのPost）。関連ではなく
  問い合わせの起点であり、①のfocalとしても現れるが保存しない（管理人は元Postを既に持っている）。
  親ツイート・リプライ・引用・同一投稿者の他投稿は残す。

## 既読フィルタ（重要）

- **既定（フラグなし）**: グローバル既読フィルタを掛けない。`state/seen_ids.json` を
  **読まず・書かず**。起点Postの関連を毎回丸ごと1ファイルに残すため。
- **`--unseen-only` 指定時**: `seen_ids.json` で既読IDを除外し、保存した新規分のIDを
  書き戻す（search.py と同じ挙動）。
- 運用: 秘書が管理人から「掛けて」「新しい情報だけちょうだい」と指示されたときのみ
  `--unseen-only` を付けて実行する。それ以外は付けない。

## 運用制約（既存方針の踏襲）

- 終了コード契約: `0`=正常 / `1`=設定・環境エラー / `2`=セッション失効 /
  `3`=レートリミット検知 / `4`=チャレンジ画面検知。**3・4でも検知時点までの収集分は保存**してから終了。
- `scroll_and_collect` の buffer パターンを踏襲し、例外中断時も収集済み分を保存する。
- 自動リトライループ・自動突破コードは追加しない。定期実行・自動ログインも追加しない。

## capture.py への変更

- `TWEET_OPERATIONS` に `"TweetDetail"` を追加（`related.py` の①でのみ使う。既存4スクリプトは
  自前で `operations=(...)` を明示指定しているため影響なし）。
- `parse_timeline_payload` は TweetDetail ペイロードにそのまま適用できる想定
  （focal/ancestor は itemType=TimelineTweet、リプライは会話モジュールの `content.items[]` 経由で
  既存 `_iter_item_contents` が拾う）。実データで欠落があれば warning でスキップし全体は落とさない、
  という既存の耐性方針を維持する。

## テスト・確認

- 自動テストは無い（既存方針）。`digest.py` 同様、Playwright非依存の純関数
  （URL/IDパース、関係種別解決、source_query組み立て、連番採番、重複排除＋起点除外）は
  単体importで確認可能にする。
- 手動確認は起点Post 1件で `--headed` 実行し、3種が想定どおり集まるか目視する。
  （2026-07-25 実機確認済み: @aichan_nel の起点Postで reply/quote/author の3種が正常収集された）

## スコープ外

- 台本・記事本体への取捨選択・執筆（vrchat-video-script / note 系スキルの担当）。
- ②引用取得の網羅性保証（X側演算子仕様に依存。ベストエフォート）。
