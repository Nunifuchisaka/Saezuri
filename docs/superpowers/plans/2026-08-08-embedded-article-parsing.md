# 埋め込み記事(X Article)解析 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 投稿本文がX Article（長文記事）へのリンクだけになっているケースで、記事タイトル・本文を追加リクエストなしで取得・保存・表示できるようにする。

**Architecture:** `TweetDetail`等の既存GraphQLレスポンス内、`tweet_results.result`（または`quoted_status_result.result`）に既に同梱されている`article.article_results.result`を解析対象に加える。新規GraphQLリクエストは一切発生しない。`capture.py`の`_parse_tweet_result`（`parse_timeline_payload`と`parse_quoted_post`の両方が内部で使う共通関数）に解析を追加するため、`search.py`/`user_tweets.py`/`bookmarks.py`/`tweet.py`のいずれで収集した投稿でも、Articleへのリンクだけの投稿があれば自動的に記事タイトル・本文が付与される。

**Tech Stack:** Python 3.11、pytest（既存のPlaywright非依存パーステスト方針を踏襲）。

## 背景（実機調査の結果）

`x.com/i/article/<id>`への直接遷移では`ArticleRedirectScreenQuery`→`TweetResultByRestId`という2段階の別オペレーションが発行される（`docs/superpowers/specs/2026-08-08-article-and-quoted-post-design.md`の機能①が想定していた経路）。

しかし、**投稿（起点投稿・引用元投稿いずれも）がArticleへのリンクを含む場合、その投稿自身の`tweet_results.result`に`article`キーが最初から同梱されている**ことを実機で確認した（`quoted_status_result.result`直下に`article`キーが存在し、`TweetResultByRestId`で見た内容と完全に一致する構造だった）。つまり`TweetDetail`等の既存の傍受だけで記事本文まで手に入り、追加リクエストの実装（`ArticleRedirectScreenQuery`等の新規オペレーション対応）は不要と判明した。本計画はこの「同梱データの解析」のみをスコープとする（Article単体URLへの直接アクセス対応は引き続き別スコープとし、本計画には含めない）。

`article.article_results.result`の構造（実機確認済み）:
- `title`：記事タイトル（文字列）
- `content_state.blocks`：本文の段落配列。各要素は`{type, text, entityRanges, inlineStyleRanges, key}`。`type`は`unstyled`（通常段落）/`ordered-list-item`（リスト）/`atomic`（区切り線等）等。今回は装飾（太字等）や箇条書き記号を保持せず、`blocks[].text`を改行で連結したプレーンテキストとして扱う（本計画のスコープ。装飾保持は将来必要になれば別途検討）。

## Global Constraints

- **オンデマンド実行専用**：cron等の定期実行を導入するコードは書かない。
- **自動ログイン厳禁**：ID/PW自動入力ロジックは追加しない。
- **エラー時は自動リトライ・自動突破をしない**：本計画は解析ロジックのみでネットワーク呼び出しを増やさないため直接関係しないが、既存方針として維持する。
- **想定キー欠如時は例外を投げず`logger.warning()`でスキップ**：`article`キーが無い・`content_state.blocks`が無い等は正常系（記事でない投稿）として扱い、`None`を返すだけで警告は出さない（`legacy`欠如等の他の必須データと違い、記事の有無は投稿ごとに異なる任意情報のため）。
- **新規ネットワークリクエストを追加しない**：本機能は既に捕捉済みのGraphQLレスポンスの解析のみで完結させる。`ArticleRedirectScreenQuery`/`TweetResultByRestId`等の新規オペレーション対応は本計画のスコープ外。

## File Structure

- Modify: `scripts/schema.py` — `Post`に`article_title`/`article_text`（両方`Optional[str] = None`）を追加。`to_dict()`/`from_dict()`を更新（`from_dict()`は旧形式JSONL＝両キー欠如でも`None`にフォールバックすること）。
- Modify: `scripts/capture.py` — `_parse_article()`ヘルパーを追加し、`_parse_tweet_result()`から呼び出して`Post`に渡す。
- Modify: `scripts/tweet.py` — 標準出力表示ロジックを`_print_post()`関数へ切り出し（起点投稿・引用元投稿の表示で重複していた記述をDRY化）、記事同梱時はタイトル・本文もあわせて表示する。モジュールdocstringを更新。
- Test: `tests/test_schema.py`（新規）— `Post`の記事フィールドのto_dict/from_dict往復変換テスト。
- Test: `tests/test_capture_article.py`（新規）— `_parse_article`/`_parse_tweet_result`の記事抽出ロジックの単体テスト。
- Test: `tests/test_tweet_print.py`（新規）— `tweet.py`の`_print_post()`表示整形ロジックの単体テスト（Playwright非依存の純粋関数）。
- Modify: `README.md` — `tweet.py`セクションの説明追記、出力ファイルのPostスキーマ一覧に`article_title`/`article_text`を追加。
- Modify: `CLAUDE.md` — `capture.py`・`scripts/tweet.py`セクションの説明追記。

## Task 1: schema.py — Postに記事フィールドを追加

**Files:**
- Modify: `scripts/schema.py`
- Test: `tests/test_schema.py`

**Interfaces:**
- Produces: `Post.article_title: Optional[str] = None`、`Post.article_text: Optional[str] = None`（Task 2・3で使用）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_schema.py`を新規作成する:

```python
"""schema.pyのPost.to_dict()/from_dict()往復変換のテスト(記事フィールド追加分)。"""
from schema import Post


def _minimal_post(**overrides) -> Post:
    base = dict(
        id="1", url="https://x.com/alice/status/1", author_handle="alice",
        author_name="Alice", created_at="Sat Aug 08 00:00:00 +0000 2026",
        text="本文", like_count=1, repost_count=2, reply_count=3, quote_count=4,
        collected_at="2026-08-08T00:00:00+09:00",
    )
    base.update(overrides)
    return Post(**base)


def test_to_dict_includes_article_fields():
    post = _minimal_post(article_title="記事タイトル", article_text="本文1行目\n本文2行目")
    data = post.to_dict()
    assert data["article_title"] == "記事タイトル"
    assert data["article_text"] == "本文1行目\n本文2行目"


def test_to_dict_article_fields_default_none():
    post = _minimal_post()
    data = post.to_dict()
    assert data["article_title"] is None
    assert data["article_text"] is None


def test_from_dict_roundtrip_article_fields():
    post = _minimal_post(article_title="記事タイトル", article_text="本文")
    restored = Post.from_dict(post.to_dict())
    assert restored.article_title == "記事タイトル"
    assert restored.article_text == "本文"


def test_from_dict_missing_article_keys_defaults_to_none():
    """article_title/article_textキーを持たない旧形式JSONLでも壊れないこと。"""
    post = _minimal_post()
    data = post.to_dict()
    del data["article_title"]
    del data["article_text"]
    restored = Post.from_dict(data)
    assert restored.article_title is None
    assert restored.article_text is None
```

- [ ] **Step 2: テストが失敗することを確認する**

実行: `pytest tests/test_schema.py -v`
期待結果: `TypeError: Post.__init__() got an unexpected keyword argument 'article_title'` でFAIL

- [ ] **Step 3: schema.pyを修正する**

`scripts/schema.py`の`Post`dataclass定義（既存の`source_query: Optional[str] = None`の直後）に以下を追加する:

```python
    article_title: Optional[str] = None
    article_text: Optional[str] = None
```

`to_dict()`を以下のように変更する（`"text"`の直後に`article_title`/`article_text`を挿入。他のキーはそのまま）:

```python
    def to_dict(self) -> dict:
        """設計書記載の項目順でJSONL保存用の辞書に変換する"""
        return {
            "id": self.id,
            "url": self.url,
            "author_handle": self.author_handle,
            "author_name": self.author_name,
            "created_at": self.created_at,
            "text": self.text,
            "article_title": self.article_title,
            "article_text": self.article_text,
            "lang": self.lang,
            "like_count": self.like_count,
            "repost_count": self.repost_count,
            "reply_count": self.reply_count,
            "quote_count": self.quote_count,
            "media": [asdict(m) for m in self.media],
            "is_repost": self.is_repost,
            "quoted_id": self.quoted_id,
            "source_query": self.source_query,
            "collected_at": self.collected_at,
        }
```

`from_dict()`を以下のように変更する（`text=data["text"]`の直後に追加。`.get()`で欠如時は`None`にフォールバックする）:

```python
    @classmethod
    def from_dict(cls, data: dict) -> "Post":
        """to_dict()と同じ形の辞書からPostを復元する"""
        media = [MediaItem(**m) for m in data.get("media", [])]
        return cls(
            id=data["id"],
            url=data["url"],
            author_handle=data["author_handle"],
            author_name=data["author_name"],
            created_at=data["created_at"],
            text=data["text"],
            article_title=data.get("article_title"),
            article_text=data.get("article_text"),
            lang=data.get("lang"),
            like_count=data["like_count"],
            repost_count=data["repost_count"],
            reply_count=data["reply_count"],
            quote_count=data["quote_count"],
            media=media,
            is_repost=data.get("is_repost", False),
            quoted_id=data.get("quoted_id"),
            source_query=data.get("source_query"),
            collected_at=data["collected_at"],
        )
```

- [ ] **Step 4: テストが通ることを確認する**

実行: `pytest tests/test_schema.py -v`
期待結果: 4件全てPASS

- [ ] **Step 5: コミット**

```bash
git add scripts/schema.py tests/test_schema.py
git commit -m "schema.py: Postに記事(article_title/article_text)フィールドを追加"
```

## Task 2: capture.py — 埋め込み記事データの解析

**Files:**
- Modify: `scripts/capture.py`
- Test: `tests/test_capture_article.py`

**Interfaces:**
- Consumes: `Post.article_title`/`Post.article_text`（Task 1で追加済み）、既存の`_dig()`ヘルパー
- Produces: `_parse_article(result: dict) -> tuple[Optional[str], Optional[str]]`（`_parse_tweet_result`内部で使用。外部から直接呼ぶ想定はない）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_capture_article.py`を新規作成する:

```python
"""capture.pyの記事(X Article)埋め込みデータ抽出ロジックの単体テスト。

Playwright非依存。TweetDetail等のペイロード内、tweet_results.resultに
article.article_results.resultとして記事本文が同梱されているケースを模した
サンプルJSONを使う(実機確認済みの構造)。
"""
from capture import parse_timeline_payload


def _payload_with_tweet(tweet_result: dict) -> dict:
    """tweet_result 1件を含む最小限のタイムラインペイロードを組み立てる。"""
    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "entryId": "tweet-1",
                                        "content": {
                                            "entryType": "TimelineTimelineItem",
                                            "itemContent": {
                                                "itemType": "TimelineTweet",
                                                "tweet_results": {"result": tweet_result},
                                            },
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        }
    }


def _tweet_result_with_article(
    tweet_id: str,
    text: str = "https://t.co/dummy",
    title: str | None = None,
    blocks: list | None = None,
) -> dict:
    """legacy情報を持つ最小限のtweet_results.result風dictを組み立てる(記事同梱可)。"""
    result: dict = {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {
            "user_results": {
                "result": {"legacy": {"screen_name": "alice", "name": "Alice"}}
            }
        },
        "legacy": {
            "id_str": tweet_id,
            "full_text": text,
            "created_at": "Sat Aug 08 00:00:00 +0000 2026",
            "favorite_count": 1,
            "retweet_count": 2,
            "reply_count": 3,
            "quote_count": 4,
        },
    }
    if title is not None or blocks is not None:
        article_result: dict = {}
        if title is not None:
            article_result["title"] = title
        if blocks is not None:
            article_result["content_state"] = {"blocks": blocks}
        result["article"] = {"article_results": {"result": article_result}}
    return result


def _parse(tweet_result: dict):
    payload = _payload_with_tweet(tweet_result)
    posts = parse_timeline_payload(
        payload, "SearchTimeline", source_query="q", collected_at="2026-08-08T00:00:00+09:00"
    )
    assert len(posts) == 1
    return posts[0]


def test_parse_tweet_result_extracts_article_title_and_text():
    blocks = [{"text": "第1段落"}, {"text": "第2段落"}]
    post = _parse(_tweet_result_with_article("100", title="記事タイトル", blocks=blocks))
    assert post.article_title == "記事タイトル"
    assert post.article_text == "第1段落\n第2段落"


def test_parse_tweet_result_article_fields_none_when_no_article():
    post = _parse(_tweet_result_with_article("100"))
    assert post.article_title is None
    assert post.article_text is None


def test_parse_tweet_result_article_text_none_when_blocks_missing():
    post = _parse(_tweet_result_with_article("100", title="タイトルのみ"))
    assert post.article_title == "タイトルのみ"
    assert post.article_text is None


def test_parse_tweet_result_article_text_skips_non_dict_blocks():
    blocks = [{"text": "有効な段落"}, "不正な要素", {"no_text_key": True}]
    post = _parse(_tweet_result_with_article("100", title="タイトル", blocks=blocks))
    assert post.article_text == "有効な段落\n"
```

- [ ] **Step 2: テストが失敗することを確認する**

実行: `pytest tests/test_capture_article.py -v`
期待結果: `AttributeError: 'Post' object has no attribute 'article_title'`等でFAIL
（Task 1が先に完了していれば`article_title`自体は存在するので、この時点では「常に`None`」でテストの`assert post.article_title == "記事タイトル"`がFAILする形になる）

- [ ] **Step 3: capture.pyを修正する**

`scripts/capture.py`の`_parse_author`関数の直後に以下の関数を追加する:

```python
def _parse_article(result: dict) -> tuple[Optional[str], Optional[str]]:
    """resultのarticle.article_results.resultから(記事タイトル, 本文)を取り出す。

    記事(X Article)へのリンクを含まない通常の投稿では(None, None)を返す
    (これは異常系ではなく通常のケースのため警告は出さない)。本文は
    content_state.blocks[].textを改行で連結したプレーンテキスト(太字等の
    インライン装飾・箇条書き記号は失われる)。blocksが無い・空の場合は
    本文None(タイトルのみのケースを許容する)。
    """
    article = _dig(result, "article", "article_results", "result")
    if not isinstance(article, dict):
        return None, None
    title = article.get("title")
    blocks = _dig(article, "content_state", "blocks")
    text = None
    if isinstance(blocks, list):
        lines = [b.get("text", "") for b in blocks if isinstance(b, dict)]
        if lines:
            text = "\n".join(lines)
    return title, text
```

`_parse_tweet_result`関数内、`handle, name = _parse_author(result)`の行の直後に以下を追加する:

```python
    article_title, article_text = _parse_article(result)
```

`_parse_tweet_result`が返す`Post(...)`のコンストラクタ呼び出しに、`media=_parse_media(legacy),`の直後の行として以下を追加する:

```python
        article_title=article_title,
        article_text=article_text,
```

- [ ] **Step 4: テストが通ることを確認する**

実行: `pytest tests/test_capture_article.py tests/test_capture_quoted.py tests/test_schema.py -v`
期待結果: 全件PASS（`test_capture_quoted.py`が既存通り通ることも確認し、既存の引用元投稿抽出ロジックに影響がないことを担保する）

- [ ] **Step 5: コミット**

```bash
git add scripts/capture.py tests/test_capture_article.py
git commit -m "capture.py: 埋め込み記事(X Article)データの解析を追加"
```

## Task 3: tweet.py — 記事タイトル・本文の表示、docstring更新

**Files:**
- Modify: `scripts/tweet.py`
- Test: `tests/test_tweet_print.py`

**Interfaces:**
- Consumes: `Post.article_title`/`Post.article_text`（Task 1・2で追加済み）
- Produces: `_print_post(post: Post) -> None`（`run()`内部専用。テストからも直接呼ぶ）

`fetch_tweet`/`run`はPlaywright依存のため引き続き自動テスト対象外（既存方針を踏襲）。`_print_post`はPost→標準出力の純粋な整形処理のため、新規にテスト対象とする。

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_tweet_print.py`を新規作成する:

```python
"""tweet.pyの_print_post()(標準出力への整形表示)の単体テスト。Playwright非依存。"""
from schema import Post
from tweet import _print_post


def _post(**overrides) -> Post:
    base = dict(
        id="1", url="https://x.com/alice/status/1", author_handle="alice",
        author_name="Alice", created_at="Sat Aug 08 00:00:00 +0000 2026",
        text="本文", like_count=1, repost_count=2, reply_count=3, quote_count=4,
        collected_at="2026-08-08T00:00:00+09:00",
    )
    base.update(overrides)
    return Post(**base)


def test_print_post_without_article(capsys):
    _print_post(_post())
    out = capsys.readouterr().out
    assert "@alice (Alice) Sat Aug 08 00:00:00 +0000 2026" in out
    assert "本文" in out
    assert "記事:" not in out
    assert "URL: https://x.com/alice/status/1" in out


def test_print_post_with_article(capsys):
    _print_post(_post(article_title="記事タイトル", article_text="第1段落\n第2段落"))
    out = capsys.readouterr().out
    assert "--- 記事: 記事タイトル ---" in out
    assert "第1段落\n第2段落" in out


def test_print_post_with_article_title_but_no_text(capsys):
    _print_post(_post(article_title="タイトルのみ", article_text=None))
    out = capsys.readouterr().out
    assert "--- 記事: タイトルのみ ---" in out
    assert "(本文取得失敗)" in out
```

- [ ] **Step 2: テストが失敗することを確認する**

実行: `pytest tests/test_tweet_print.py -v`
期待結果: `ImportError: cannot import name '_print_post' from 'tweet'` でFAIL

- [ ] **Step 3: tweet.pyを修正する**

`scripts/tweet.py`に`_print_post`関数を追加する。`run`関数の直前（`def run(args: argparse.Namespace) -> int:`の直前）に挿入する:

```python
def _print_post(post: Post) -> None:
    """1件分の投稿情報を標準出力へ表示する。記事(X Article)へのリンクを含む
    投稿の場合、記事タイトル・本文もあわせて表示する(追加リクエストなしで
    capture.py側が既に解析済み)。"""
    print(f"@{post.author_handle} ({post.author_name}) {post.created_at}")
    print(post.text)
    if post.article_title:
        print(f"--- 記事: {post.article_title} ---")
        print(post.article_text or "(本文取得失敗)")
    print(f"いいね: {post.like_count} / RT: {post.repost_count} /"
          f" 返信: {post.reply_count} / 引用: {post.quote_count}")
    print(f"URL: {post.url}")
```

`run`関数内の表示部分（現在の以下の箇所）:

```python
        storage.append_jsonl(out_path, records)
        print(f"@{post.author_handle} ({post.author_name}) {post.created_at}")
        print(post.text)
        print(f"いいね: {post.like_count} / RT: {post.repost_count} /"
              f" 返信: {post.reply_count} / 引用: {post.quote_count}")
        print(f"URL: {post.url}")
        if quoted is not None:
            print("--- 引用元投稿 ---")
            print(f"@{quoted.author_handle} ({quoted.author_name}) {quoted.created_at}")
            print(quoted.text)
            print(f"いいね: {quoted.like_count} / RT: {quoted.repost_count} /"
                  f" 返信: {quoted.reply_count} / 引用: {quoted.quote_count}")
            print(f"URL: {quoted.url}")
        print(f"保存先: {out_path}")
```

を以下に置き換える:

```python
        storage.append_jsonl(out_path, records)
        _print_post(post)
        if quoted is not None:
            print("--- 引用元投稿 ---")
            _print_post(quoted)
        print(f"保存先: {out_path}")
```

- [ ] **Step 4: テストが通ることを確認する**

実行: `pytest tests/ -v`
期待結果: 全件PASS（`tests/test_tweet_print.py`の3件含め、既存分も回帰なし）

- [ ] **Step 5: モジュールdocstringを更新する**

`scripts/tweet.py`冒頭のdocstring、以下の行:

```
起点ツイートが引用ツイートである場合は、引用元投稿も2件目として同じファイルに保存される。
```

の直後に以下を追加する:

```
起点ツイートまたは引用元投稿がX Article(長文記事)へのリンクの場合、記事タイトル・
本文もあわせて取得し画面表示・JSONLへ保存する(TweetDetailのレスポンスに同梱されて
いるデータを解析するのみで、追加のGraphQLリクエストは発生しない)。
```

- [ ] **Step 6: コミット**

```bash
git add scripts/tweet.py tests/test_tweet_print.py
git commit -m "tweet.py: 記事(X Article)タイトル・本文の表示に対応、表示処理をDRY化"
```

## Task 4: ドキュメント更新（README.md / CLAUDE.md）

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

このタスクはコード変更を伴わないため、テストステップはない（既存のTask構成の中で最もドキュメントのみのタスクとして独立させる）。

- [ ] **Step 1: README.mdを更新する**

`tweet.py（単一ツイート取得）`セクション、以下の行:

```
対象ツイートが引用ツイートの場合、`quoted_status_result`として同梱されている引用元投稿も追加リクエストなしで同時に取得し、同じJSONLファイルに2件目のレコード(`source_query`は`quoted-by:<起点投稿id>`)として保存する。標準出力にも起点投稿に続けて引用元投稿を表示する。引用元が削除済み・非公開等で取得できない場合はエラーにせず、起点投稿のみを保存して正常終了する。
```

の直後に以下の段落を追加する:

```
起点投稿・引用元投稿いずれかがX Article（長文記事）へのリンクの場合、記事タイトル・本文もあわせて取得する。これも`TweetDetail`のレスポンスに既に同梱されているデータを解析するだけで、追加のGraphQLリクエストは発生しない。記事本文はプレーンテキスト化して保存する（太字等の装飾・箇条書き記号は失われる）。記事でない通常の投稿では従来通り影響しない。
```

「出力ファイルについて」セクションの以下の部分:

```
id, url, author_handle, author_name, created_at, text, lang,
like_count, repost_count, reply_count, quote_count,
media(type/url配列), is_repost, quoted_id, source_query, collected_at
```

を以下に置き換える:

```
id, url, author_handle, author_name, created_at, text, article_title, article_text, lang,
like_count, repost_count, reply_count, quote_count,
media(type/url配列), is_repost, quoted_id, source_query, collected_at
```

- [ ] **Step 2: CLAUDE.mdを更新する**

`capture.py`セクションの以下の行:

```
  - `parse_timeline_payload`/`parse_trends_payload`：GraphQLペイロードから`Post`/`TrendEntry`へ正規化。X側のレスポンス構造は非公開・無予告変更されるため、キー名を決め打ちせず「`instructions`という名のlistを再帰探索する」等の耐性を持たせている。想定キー欠如時は例外を投げず`logger.warning()`でそのエントリだけスキップし、全体は落とさない設計を踏襲すること。
```

の直後に以下を追加する:

```
  - `_parse_tweet_result`は投稿本体の正規化に加え、`result.article.article_results.result`（X Article＝長文記事へのリンクを含む投稿の場合に同梱される）を`_parse_article`で解析し`Post.article_title`/`Post.article_text`に格納する。新規GraphQLリクエストは発生しない（既に捕捉済みのレスポンスの解析のみ）。記事でない投稿では両方`None`のままで、これは異常系ではないため警告も出さない。`content_state.blocks[].text`を改行連結したプレーンテキストとして保存する（装飾・箇条書き記号は失われる）。
```

`scripts/tweet.py（単一ツイート取得）`セクションの以下の行:

```
対象ツイートが引用ツイートの場合、TweetDetailペイロードに同梱される`quoted_status_result.result`から引用元投稿も追加リクエストなしで正規化し(`capture.py`の`find_tweet_result`/`parse_quoted_post`)、同じJSONLへ2件目のレコード(`source_query`は`quoted-by:<起点投稿id>`)として保存する。この挙動は`tweet.py`のみが対象で、`parse_timeline_payload`を使う他の収集モジュール(`search.py`等)には影響しない。
```

の直後に以下を追加する:

```
起点投稿・引用元投稿いずれかがX Articleへのリンクの場合、記事タイトル・本文も`Post.article_title`/`Post.article_text`として同時に保存・標準出力表示される。この解析自体は`capture.py`の`_parse_tweet_result`に組み込まれているため、`tweet.py`専用ではなく`search.py`等の収集モジュールで収集した投稿にも同様に適用される（引用元投稿の取得自体が`tweet.py`専用なのは変わらない）。標準出力の表示は`tweet.py`の`_print_post`ヘルパーに集約した（起点投稿・引用元投稿の表示コードの重複を解消）。
```

- [ ] **Step 3: コミット**

```bash
git add README.md CLAUDE.md
git commit -m "docs: 埋め込み記事(X Article)解析の説明を追加"
```

## Self-Review Notes

- 実機調査で判明した「`TweetDetail`等に記事データが既に同梱されている」という前提に基づき、新規GraphQLリクエスト（`ArticleRedirectScreenQuery`/`TweetResultByRestId`）を発行する経路は本計画に含めていない（ユーザー確認済みのスコープ判断）。Article単体URL（`x.com/i/article/<id>`）への直接アクセス対応は引き続き将来課題。
- `_parse_article`は`_parse_tweet_result`に組み込むため、`search.py`/`user_tweets.py`/`bookmarks.py`/`tweet.py`のいずれの収集結果にも自動的に適用される。これは意図的な設計（Task②の`parse_quoted_post`のような`tweet.py`専用スコープとは異なり、記事解析はデータの正規化そのものであり全収集モジュール共通で正しい）。
- `digest.py`（Markdownダイジェスト生成）は本計画で変更しない。既存の`summarize_text(post.text)`は`article_text`を参照しないため、記事へのリンク投稿は引き続きリンクのみの要約になる。ダイジェストへの記事本文反映が必要になれば別途スコープを切る（YAGNI。今回のユーザー要望は「取得できるようにする」であり、ダイジェスト表示の変更までは要求されていない）。
- `media.py`（画像ダウンロード）も本計画で変更しない。`article.cover_media`（カバー画像）の取り込みは対象外（実機調査でも本文以外は未検証のため）。
