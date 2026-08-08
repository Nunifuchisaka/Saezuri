# 引用元投稿の自動取得 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `tweet.py`で単一ツイートを取得する際、対象が引用ツイートであれば同じTweetDetailレスポンスから引用元投稿も同時に正規化し、起点投稿と同じJSONLファイルへ2件目のレコードとして保存する。

**Architecture:** `capture.py`のTweetDetailペイロードパース処理に、`quoted_status_result.result`を検出してPostへ正規化する専用ヘルパー(`find_tweet_result`/`parse_quoted_post`)を追加する。既存の`parse_timeline_payload`(search.py/user_tweets.py/bookmarks.py等が使う共通パス)には手を入れず、`tweet.py`側だけが新ヘルパーを呼び出すことで、この機能を`tweet.py`専用に閉じ込める(設計書「対象は`tweet.py`のみ」を満たす)。

**Tech Stack:** Python 3.11、Playwright(既存)、pytest(本タスクで新規導入)。

## Global Constraints

- オンデマンド実行専用：cron等の定期実行コードを追加しない。
- 自動ログイン厳禁：ID/PW自動入力ロジックを追加しない(本タスクでは触れない領域)。
- エラー時に自動リトライ・自動突破をしない：429/チャレンジ画面検知時は検知時点までの収集分を保存して中断する既存の`fetch_tweet`の挙動を維持する。
- 終了コード契約を変えない：`0`=正常 / `1`=設定・環境エラーまたは対象ツイート未発見 / `2`=セッション失効 / `3`=レートリミット / `4`=チャレンジ画面。
- パーサーは想定キー欠如時に例外を投げず`logger.warning()`でその1件をスキップする既存方針を踏襲する(`capture.py`のモジュールdocstring参照)。
- 全コマンドはプロジェクトルートから相対パスで実行する前提を崩さない。
- 引用元投稿が見つからない・削除済み等で取得できない場合はエラーにせず、起点投稿のみの結果として正常終了する(終了コード`0`のまま)。

---

## File Structure

- Modify: `scripts/capture.py` — `_resolve_tweet_result`のTweetWithVisibilityResults展開ロジックを共通ヘルパーへ切り出し、引用元投稿抽出用の`_resolve_quoted_result`/`find_tweet_result`/`parse_quoted_post`を追加する。
- Modify: `scripts/tweet.py` — `fetch_tweet`が引用元Postも合わせて返すようにし、`run()`で起点投稿と同じJSONLへ2件目として保存・標準出力に追記表示する。
- Create: `tests/test_capture_quoted.py` — `capture.py`の引用元投稿抽出ロジックのpytest単体テスト(サンプルJSONペイロードを使用、Playwright非依存)。
- Create: `tests/__init__.py` — 空ファイル。`tests/`配下を`scripts/`と同階層からimportできるようにするための`conftest.py`と併せて追加。
- Create: `tests/conftest.py` — `scripts/`をimportパスに追加する設定(プロジェクトルート直下に`scripts/`があり、`tests/`から素の`import capture`ができるようにするため)。
- Modify: `requirements.txt` — `pytest`を追加する。
- Modify: `README.md` — `tweet.py`セクションに引用元投稿自動取得の説明を追記する。
- Modify: `CLAUDE.md` — `tweet.py`の説明段落に引用元投稿自動取得の挙動を追記する。

---

### Task 1: pytestの導入とテスト実行基盤の整備

**Files:**
- Modify: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Consumes: なし(このプロジェクトで初めてのテスト基盤導入)
- Produces: `tests/`配下から`import capture`のように`scripts/`直下のモジュールを素のモジュール名でimportできる状態。以降のタスクはこれを前提にテストを書く。

- [ ] **Step 1: 仮想環境にpytestをインストールする**

Run (PowerShellの場合、プロジェクトルートで仮想環境を有効化した上で):

```
pip install pytest
```

- [ ] **Step 2: インストールされた実際のバージョンを確認する**

Run:

```
pip show pytest
```

表示された`Version:`の値を次のStepで使う(例: `8.3.4`ならその値をそのまま使う。実際にインストールされたバージョンをそのまま`requirements.txt`に書く。バージョンを推測で書かない)。

- [ ] **Step 3: `requirements.txt`にpytestを追記する**

`requirements.txt`の末尾に、Step 2で確認した実際のバージョンを使って1行追加する:

```
playwright==1.61.0
PyYAML==6.0.3
pytest==<Step 2で確認した実際のバージョン>
```

- [ ] **Step 4: `tests/__init__.py`を作成する**

空ファイルとして作成する(中身なし)。

- [ ] **Step 5: `tests/conftest.py`を作成する**

```python
"""pytest共通設定。tests/配下からscripts/直下のモジュールを素のモジュール名でimportできるようにする。"""
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
```

- [ ] **Step 6: pytestが動作することを確認する**

Run: `pytest --collect-only`
Expected: エラーなく終了する(この時点ではテストファイルがまだ無いため`collected 0 items`等の表示でよい)。

- [ ] **Step 7: コミット**

```bash
git add requirements.txt tests/__init__.py tests/conftest.py
git commit -m "test: pytest導入とテスト実行基盤を追加"
```

---

### Task 2: capture.py に引用元投稿抽出ロジックを追加する

**Files:**
- Modify: `scripts/capture.py:295-305`（`_resolve_tweet_result`をヘルパー切り出しで置き換え、直後に`_resolve_quoted_result`を追加）
- Modify: `scripts/capture.py:400`付近（`parse_timeline_payload`の直後に`find_tweet_result`と`parse_quoted_post`を追加）
- Test: `tests/test_capture_quoted.py`

**Interfaces:**
- Consumes: `capture.py`の既存プライベート関数`_dig`、`_find_instructions`、`_iter_item_contents`、`_parse_tweet_result`（`(result: dict, operation: str, source_query: Optional[str], collected_at: str) -> Optional[Post]`）。`schema.Post`。
- Produces:
  - `find_tweet_result(payload: dict, tweet_id: str) -> Optional[dict]` — payload中からidが一致するtweet_results.resultの生dictを返す。Task 3(`tweet.py`)が使う。
  - `parse_quoted_post(result: dict, origin_id: str, operation: str, collected_at: str) -> Optional[Post]` — resultのquoted_status_resultをPostへ正規化する。Task 3が使う。

- [ ] **Step 1: 失敗するテストを書く(`tests/test_capture_quoted.py`)**

```python
"""capture.pyの引用元投稿(quoted_status_result)抽出ロジックの単体テスト。

Playwright非依存。TweetDetailペイロードの構造を模したサンプルJSONを使う。
"""
from capture import find_tweet_result, parse_quoted_post


def _payload_with_tweet(tweet_result: dict) -> dict:
    """tweet_result 1件を含む最小限のTweetDetailペイロードを組み立てる。"""
    return {
        "data": {
            "threaded_conversation_with_injections_v2": {
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


def _tweet_result(
    tweet_id: str,
    text: str,
    handle: str = "alice",
    name: str = "Alice",
    quoted: dict | None = None,
    wrap_visibility: bool = False,
) -> dict:
    """legacy情報を持つ最小限のtweet_results.result風dictを組み立てる。"""
    result = {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {
            "user_results": {
                "result": {"legacy": {"screen_name": handle, "name": name}}
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
    if quoted is not None:
        result["legacy"]["quoted_status_id_str"] = quoted["rest_id"]
        if wrap_visibility:
            result["quoted_status_result"] = {
                "result": {"__typename": "TweetWithVisibilityResults", "tweet": quoted}
            }
        else:
            result["quoted_status_result"] = {"result": quoted}
    return result


def test_find_tweet_result_returns_matching_result():
    payload = _payload_with_tweet(_tweet_result("100", "起点ツイート"))
    result = find_tweet_result(payload, "100")
    assert result is not None
    assert result["rest_id"] == "100"


def test_find_tweet_result_returns_none_when_not_found():
    payload = _payload_with_tweet(_tweet_result("100", "起点ツイート"))
    assert find_tweet_result(payload, "999") is None


def test_parse_quoted_post_returns_post_when_present():
    quoted = _tweet_result("200", "引用元の本文", handle="bob", name="Bob")
    origin = _tweet_result("100", "起点ツイート", quoted=quoted)

    post = parse_quoted_post(origin, origin_id="100", operation="TweetDetail",
                              collected_at="2026-08-08T00:00:00+09:00")

    assert post is not None
    assert post.id == "200"
    assert post.text == "引用元の本文"
    assert post.author_handle == "bob"
    assert post.source_query == "quoted-by:100"
    assert post.collected_at == "2026-08-08T00:00:00+09:00"


def test_parse_quoted_post_returns_none_when_absent():
    origin = _tweet_result("100", "起点ツイート")
    post = parse_quoted_post(origin, origin_id="100", operation="TweetDetail",
                              collected_at="2026-08-08T00:00:00+09:00")
    assert post is None


def test_parse_quoted_post_handles_visibility_results_nesting():
    quoted = _tweet_result("300", "閲覧制限付き引用元", handle="carol", name="Carol")
    origin = _tweet_result("100", "起点ツイート", quoted=quoted, wrap_visibility=True)

    post = parse_quoted_post(origin, origin_id="100", operation="TweetDetail",
                              collected_at="2026-08-08T00:00:00+09:00")

    assert post is not None
    assert post.id == "300"
    assert post.author_handle == "carol"


def test_parse_quoted_post_returns_none_when_quoted_legacy_missing():
    origin = _tweet_result("100", "起点ツイート")
    origin["quoted_status_result"] = {"result": {"__typename": "Tweet", "rest_id": "400"}}

    post = parse_quoted_post(origin, origin_id="100", operation="TweetDetail",
                              collected_at="2026-08-08T00:00:00+09:00")

    assert post is None
```

- [ ] **Step 2: テストを実行し失敗を確認する**

Run: `pytest tests/test_capture_quoted.py -v`
Expected: `ImportError: cannot import name 'find_tweet_result' from 'capture'`（`parse_quoted_post`も同様）でFAIL

- [ ] **Step 3: `capture.py:295-305`の`_resolve_tweet_result`を共通ヘルパー切り出しに書き換える**

`scripts/capture.py`の以下の箇所を:

```python
def _resolve_tweet_result(item_content: dict) -> Optional[dict]:
    """itemContentからtweet_results.resultを取り出す。ツイートでなければNone。"""
    result = _dig(item_content, "tweet_results", "result")
    if not isinstance(result, dict):
        return None
    # 閲覧制限付きツイートは1段ネストが深い
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet")
        if not isinstance(result, dict):
            return None
    return result
```

次のように置き換える:

```python
def _unwrap_visibility_results(result: Any) -> Optional[dict]:
    """TweetWithVisibilityResults(閲覧制限付きツイート)の1段ネストを吸収する。"""
    if not isinstance(result, dict):
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet")
        if not isinstance(result, dict):
            return None
    return result


def _resolve_tweet_result(item_content: dict) -> Optional[dict]:
    """itemContentからtweet_results.resultを取り出す。ツイートでなければNone。"""
    return _unwrap_visibility_results(_dig(item_content, "tweet_results", "result"))


def _resolve_quoted_result(result: dict) -> Optional[dict]:
    """resultのquoted_status_result.resultを取り出す。引用ポストでなければNone。"""
    return _unwrap_visibility_results(_dig(result, "quoted_status_result", "result"))
```

- [ ] **Step 4: `parse_timeline_payload`の直後に`find_tweet_result`と`parse_quoted_post`を追加する**

`scripts/capture.py`の`parse_timeline_payload`関数(現行398-400行目付近の`return posts`で終わる関数)の直後、`_VOLUME_RE = re.compile(...)`の手前に以下を追加する:

```python
def find_tweet_result(payload: dict, tweet_id: str) -> Optional[dict]:
    """payload中からidがtweet_idに一致するtweet_results.resultの生dictを探す。

    parse_timeline_payload()は正規化後のPostしか返さないため、quoted_status_result
    等の正規化前データを参照したい呼び出し元(tweet.pyの引用元投稿取得)向けに用意する。
    見つからなければNone。
    """
    instructions = _find_instructions(payload)
    for item_content in _iter_item_contents(instructions):
        if item_content.get("itemType") not in (None, "TimelineTweet"):
            continue
        result = _resolve_tweet_result(item_content)
        if result is None:
            continue
        legacy = result.get("legacy") or _dig(result, "tweet", "legacy")
        if not isinstance(legacy, dict):
            continue
        post_id = legacy.get("id_str") or result.get("rest_id")
        if post_id and str(post_id) == tweet_id:
            return result
    return None


def parse_quoted_post(
    result: dict,
    origin_id: str,
    operation: str,
    collected_at: str,
) -> Optional[Post]:
    """resultのquoted_status_resultを検出し、引用元投稿をPostへ正規化する。

    起点投稿(result)が引用ツイートでない場合、または引用元が削除済み等で
    必須キーを欠く場合はNone(後者は_parse_tweet_result内でlogger.warning済み)。
    tweet.py専用(parse_timeline_payloadを使う他の収集モジュールはこの関数を呼ばない)。
    """
    quoted_result = _resolve_quoted_result(result)
    if quoted_result is None:
        return None
    return _parse_tweet_result(
        quoted_result, operation, f"quoted-by:{origin_id}", collected_at
    )
```

- [ ] **Step 5: テストを再実行しパスを確認する**

Run: `pytest tests/test_capture_quoted.py -v`
Expected: 6件すべてPASS

- [ ] **Step 6: 既存の収集モジュールに影響が出ていないことを確認する**

Run: `python -c "import capture"`（構文エラー・importエラーがないことのみ確認。Playwright起動は行わない）
Expected: エラーなく終了する

- [ ] **Step 7: コミット**

```bash
git add scripts/capture.py tests/test_capture_quoted.py
git commit -m "feat: capture.pyに引用元投稿(quoted_status_result)の抽出ロジックを追加"
```

---

### Task 3: tweet.py で引用元投稿を同時取得・保存・表示する

**Files:**
- Modify: `scripts/tweet.py:30-37`（import追加）
- Modify: `scripts/tweet.py:89-133`（`fetch_tweet`関数）
- Modify: `scripts/tweet.py:136-195`（`run`関数）

**Interfaces:**
- Consumes: `capture.find_tweet_result(payload: dict, tweet_id: str) -> Optional[dict]`、`capture.parse_quoted_post(result: dict, origin_id: str, operation: str, collected_at: str) -> Optional[Post]`（Task 2で追加）。
- Produces: `fetch_tweet(...)`の戻り値を`tuple[Post | None, Post | None, Exception | None]`（起点Post、引用元Post、ブロック検知例外）に変更する。この関数は`tweet.py`内でのみ呼ばれるため、シグネチャ変更による他モジュールへの影響はない。

- [ ] **Step 1: importに`find_tweet_result`と`parse_quoted_post`を追加する**

`scripts/tweet.py:30-37`の以下を:

```python
from capture import (
    ChallengeDetectedError,
    GraphQLCapture,
    RateLimitedError,
    get_wait_seconds_range,
    now_iso,
    parse_timeline_payload,
)
```

次のように書き換える:

```python
from capture import (
    ChallengeDetectedError,
    GraphQLCapture,
    RateLimitedError,
    find_tweet_result,
    get_wait_seconds_range,
    now_iso,
    parse_quoted_post,
    parse_timeline_payload,
)
```

- [ ] **Step 2: `fetch_tweet`を引用元投稿も返すように書き換える**

`scripts/tweet.py:89-133`の`fetch_tweet`関数全体を次のように置き換える:

```python
def fetch_tweet(
    context,
    goto_url: str,
    tweet_id: str,
    wait_range: tuple[float, float],
    max_rounds: int,
) -> tuple[Post | None, Post | None, Exception | None]:
    """TweetDetailを傍受し、tweet_idに一致するPostが見つかるまでドレインを繰り返す。

    スクロールはせず(会話スレッド全体の収集は範囲外)、起点ツイートが見つかった
    時点で即座に打ち切る。起点ツイートが引用ツイートの場合、同じレスポンス内に
    同梱されているquoted_status_resultから引用元投稿も追加リクエストなしで
    正規化して同時に返す。
    戻り値は(見つかった起点Post または None, 引用元Post または None(引用でない・
    取得失敗時),ブロック検知例外 または None)。
    """
    collected_at = now_iso()
    buffer: list[Post] = []
    quoted: Post | None = None

    def parse(payload: dict, op: str) -> list:
        nonlocal quoted
        posts = parse_timeline_payload(
            payload, op, source_query=f"tweet:{tweet_id}", collected_at=collected_at
        )
        buffer.extend(posts)
        if quoted is None:
            raw_result = find_tweet_result(payload, tweet_id)
            if raw_result is not None:
                quoted = parse_quoted_post(raw_result, tweet_id, op, collected_at)
        return posts

    page = context.new_page()
    capture = GraphQLCapture(operations=("TweetDetail",))
    capture.attach(page)
    try:
        page.goto(goto_url, wait_until="domcontentloaded")
        for _ in range(max_rounds):
            capture.raise_if_blocked(page)
            for operation, payload in capture.drain():
                parse(payload, operation)
            capture.raise_if_blocked(page)
            found = find_post(buffer, tweet_id)
            if found is not None:
                return found, quoted, None
            page.wait_for_timeout(random.uniform(*wait_range) * 1000)
        return find_post(buffer, tweet_id), quoted, None
    except (RateLimitedError, ChallengeDetectedError) as exc:
        return find_post(buffer, tweet_id), quoted, exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("GraphQLCaptureのdetachに失敗しました: %s", exc)
        page.close()
```

- [ ] **Step 3: `run`を引用元投稿の保存・表示に対応させる**

`scripts/tweet.py:136-195`の`run`関数全体を次のように置き換える:

```python
def run(args: argparse.Namespace) -> int:
    try:
        tweet_id, handle = resolve_start(args.url, args.id)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR

    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)

    post: Post | None = None
    quoted: Post | None = None
    blocked: Exception | None = None

    try:
        with sync_playwright() as playwright:
            browser = launch_browser(
                playwright, browser_name, headless=not args.headed,
                executable_path=get_browser_executable_path(config),
            )
            try:
                context = new_context(browser, config)
                require_login(context)
                post, quoted, blocked = fetch_tweet(
                    context,
                    build_status_url(tweet_id, handle),
                    tweet_id,
                    wait_range,
                    args.max_rounds,
                )
            finally:
                browser.close()
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SessionExpiredError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_SESSION_EXPIRED

    out_path = None
    if post is not None:
        out_path = next_tweet_out_path(raw_dir)
        records = [post.to_dict()]
        if quoted is not None:
            records.append(quoted.to_dict())
        elif post.quoted_id:
            logger.info(
                "起点ツイート(id=%s)は引用ツイートですが、引用元(id=%s)の取得に"
                "失敗しました(削除・非公開の可能性)。起点投稿のみ保存します。",
                post.id, post.quoted_id,
            )
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
    else:
        print(f"エラー: ツイートが見つかりませんでした(id={tweet_id})。"
              "削除・非公開・鍵アカウントの可能性があります。", file=sys.stderr)

    if blocked is not None:
        exit_code = EXIT_RATE_LIMITED if isinstance(blocked, RateLimitedError) else EXIT_CHALLENGE
        print(f"エラー: {blocked}", file=sys.stderr)
        return exit_code

    return 0 if post is not None else EXIT_ERROR
```

- [ ] **Step 4: 構文・import確認**

Run: `python -c "import ast; ast.parse(open('scripts/tweet.py', encoding='utf-8').read())"`
Expected: エラーなく終了する

Run: `pytest tests/test_capture_quoted.py -v`
Expected: 6件すべてPASS（Task 2のテストに影響がないことの再確認）

- [ ] **Step 5: 手動QA(実機確認、モック困難なため)**

以下はPlaywrightでの実ブラウザ操作を伴うため自動テスト化せず、手動で確認する:

1. `python scripts/login.py`でログイン済みであることを確認する
2. 引用ツイートであることが分かっている投稿のURLを用意し、以下を実行する:
   ```
   python scripts/tweet.py --url <引用ツイートのURL> --headed
   ```
3. 標準出力に起点投稿に続けて「--- 引用元投稿 ---」以降が表示されることを確認する
4. 保存先JSONL(`data/raw/tweet_<日付>_<連番>.jsonl`)を開き、2行(起点投稿・引用元投稿)保存されていることを確認する。2行目の`source_query`が`quoted-by:<起点投稿id>`になっていることを確認する
5. 引用ではない通常の投稿のURLでも実行し、従来通り1行のみ保存され、エラーにならず正常終了(終了コード0)することを確認する
6. 削除済み・非公開など引用元が取得できないケースがあれば、エラー終了せず起点投稿のみ保存されることを確認する(コンソールログにINFOメッセージが出ることも確認する)

- [ ] **Step 6: コミット**

```bash
git add scripts/tweet.py
git commit -m "feat: tweet.pyで引用元投稿を同時取得・保存・表示する"
```

---

### Task 4: ドキュメント更新

**Files:**
- Modify: `README.md`（`tweet.py`セクション、153-169行目付近）
- Modify: `CLAUDE.md`（`tweet.py`の説明段落）

**Interfaces:**
- Consumes: Task 3で確定した`tweet.py`の実際の挙動(引用元投稿の保存・表示形式)
- Produces: なし(ドキュメントのみ)

- [ ] **Step 1: `README.md`の`tweet.py`セクションに引用元投稿自動取得の説明を追記する**

`README.md`の「### tweet.py（単一ツイート取得）」セクション、既存の説明文の後(オプション表のさらに後、`本文・投稿者・エンゲージメント数を標準出力に表示し...`の段落)に以下を追記する:

```markdown
対象ツイートが引用ツイートの場合、`quoted_status_result`として同梱されている引用元投稿も追加リクエストなしで同時に取得し、同じJSONLファイルに2件目のレコード(`source_query`は`quoted-by:<起点投稿id>`)として保存する。標準出力にも起点投稿に続けて引用元投稿を表示する。引用元が削除済み・非公開等で取得できない場合はエラーにせず、起点投稿のみを保存して正常終了する。
```

- [ ] **Step 2: `CLAUDE.md`の`tweet.py`の説明段落に同様の追記をする**

`CLAUDE.md`の「### `scripts/tweet.py`（単一ツイート取得）」段落の末尾に以下の文を追記する:

```markdown
対象ツイートが引用ツイートの場合、TweetDetailペイロードに同梱される`quoted_status_result.result`から引用元投稿も追加リクエストなしで正規化し(`capture.py`の`find_tweet_result`/`parse_quoted_post`)、同じJSONLへ2件目のレコード(`source_query`は`quoted-by:<起点投稿id>`)として保存する。この挙動は`tweet.py`のみが対象で、`parse_timeline_payload`を使う他の収集モジュール(`search.py`等)には影響しない。
```

- [ ] **Step 3: コミット**

```bash
git add README.md CLAUDE.md
git commit -m "docs: tweet.pyの引用元投稿自動取得についてREADME/CLAUDE.mdに追記"
```

---

## Self-Review Notes

- 設計書②の全項目に対応済み: TweetDetail同梱データからの取得(追加リクエストなし)＝Task2, 対象は`tweet.py`のみ＝Task2で共通パーサーを変更せず専用関数を追加、同一JSONLへ2件目として保存＝Task3, `source_query`は`quoted-by:<起点投稿id>`＝Task2/3, 標準出力への追記表示＝Task3, 取得失敗時のエラーなし正常終了＝Task3, `quoted_status_result`を含むペイロードのパース単体テスト＝Task2。
- プレースホルダーなし: 全Stepに実コードを記載済み。
- 型・シグネチャの一貫性: `find_tweet_result(payload: dict, tweet_id: str) -> Optional[dict]`と`parse_quoted_post(result: dict, origin_id: str, operation: str, collected_at: str) -> Optional[Post]`はTask2で定義した名称のままTask3で使用している。`fetch_tweet`の戻り値`tuple[Post | None, Post | None, Exception | None]`はTask3のStep2/Step3で一貫している。
