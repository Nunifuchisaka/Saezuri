# tweet.py 拡張設計書：X Article対応 と 引用元投稿の自動取得

- 日付: 2026-08-08
- 対象: saezuri
- 変更: `scripts/tweet.py`、`scripts/capture.py`（引用元パース追加）
- 新規: `scripts/article.py`（実装時のGraphQLオペレーション調査結果次第で、`tweet.py`への分岐追加に代える可能性あり）

## 背景

`tweet.py`はURL/ID指定で単一投稿1件を取得できるが、以下2点で実用上不足していた。

1. `x.com/i/article/<id>`形式のX Article（長文記事）はURL形式が`/status/<id>`と異なり、
   `related.py`由来の`STATUS_URL_RE`で解釈できずエラーになる
2. 対象投稿が引用ツイートの場合、`schema.Post.quoted_id`に引用元のID文字列は残るが
   本文などの内容までは取得しておらず、引用元を読むには手動でURLを特定して
   別途`tweet.py`を実行し直す必要があった

## 機能① X Article対応

- 対象URL形式: `x.com/i/article/<id>`（`twitter.com`表記・クエリ付きも許容）
- 実装前に実機でArticleページのGraphQLレスポンスを観測し、使用オペレーション名
  （`TweetDetail`とは別名の可能性が高い）を特定する
- URL判定を`tweet.py`側に追加し、Article用オペレーションを`GraphQLCapture`で
  傍受する分岐を設ける。本文の正規化先スキーマは既存`schema.Post`を流用する
  （記事本文が長文になる点以外は投稿と同じ枠組みで扱える想定）
- 出力: 既存`tweet_<日付>_<連番>.jsonl`にそのまま保存し、標準出力の表示形式も
  既存の単一投稿取得と揃える

## 機能② 引用元投稿の自動取得

- `tweet.py`で単一投稿を取得する際、対象投稿が引用ツイートであれば引用元投稿も
  同時に取得する（対象は`tweet.py`のみ。`search.py`等の一覧系コマンドは対象外）
- TweetDetailのGraphQLペイロードには引用元ツイートの全データが
  `quoted_status_result.result`として同梱されている（`TweetWithVisibilityResults`で
  1段ネストが深くなるケースは`_resolve_tweet_result`と同様の処理で吸収する）ため、
  追加のページ遷移・追加リクエストなしでその場でパースする
- `capture.py`の`_parse_tweet_result`（または呼び出し側）に、`quoted_status_result`を
  検出したら再帰的に`Post`へ正規化する処理を追加する
- 出力: 引用元投稿は起点投稿と同じJSONLファイルに2件目のレコードとして保存する。
  `source_query`は`quoted-by:<起点投稿id>`とし、由来を追跡できるようにする
- 標準出力にも起点投稿の表示に続けて引用元投稿を表示する
- 引用元が見つからない・削除済み等で取得できない場合はエラーにせず、
  その旨をログに残した上で起点投稿のみの結果として正常終了する

## テスト

- Rust側と異なりPythonのため、`pytest`等（既存プロジェクトのテスト方針に合わせる）で
  以下をカバーする
  - Article URL判定の正規表現の単体テスト
  - `quoted_status_result`を含むペイロードのパース（サンプルJSONを用いた単体テスト）
- 実機でのGraphQLオペレーション名確認・実際のArticle取得・引用ツイート取得は
  モックが困難なため手動QAとする
