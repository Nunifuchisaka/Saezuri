"""saezuri ログイン不要の投稿取得（FxTwitter API）

投稿URL（またはID）を渡すと、第三者が運営する公開API FxTwitter（https://api.fxtwitter.com 、
オープンソース: https://github.com/FxEmbed/FxEmbed 、MITライセンス）から、その投稿の本文・いいね等の数・
画像URLを取得してJSONLに保存する。

- Xへのログインもブラウザも使わない（サブアカウントの凍結リスクが無い）。HTTPのGETを1件ずつ送るだけ。
- 外部に送るのは投稿IDだけ（Cookie・アカウント情報・config.yamlの中身は送らない）。User-Agentは汎用の名前。
- 取れるのは「指定した投稿1件（とその引用元）」だけ。検索・返信一覧・アカウントの投稿一覧は取れない。
- FxTwitterは非公式・無保証のサービスなので、止まったり仕様が変わったりしうる。応答は信用しないデータとして扱い、
  想定外の形ならその投稿だけ警告してスキップする。
- 他の収集と同じく、自動リトライはしない。429（アクセス過多）を受けたら即中断し、取得済み分を保存して終了コード3。
- 複数指定時は1件ごとに1〜3秒のランダム待機を入れる（FxTwitterの制限は1分1000件/IPだが、控えめにする）。

結果は data/raw/fx_<日付>_<連番3桁>.jsonl（Post形式）に保存する。表示回数（views）はPostスキーマに無いため
標準出力に表示するだけで保存しない。

使い方:
    python scripts/fx_tweet.py --url https://x.com/foo/status/123 [--url ...]
    python scripts/fx_tweet.py --id 123 --id 456
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import storage
from schema import MediaItem, Post

logger = logging.getLogger("fx_tweet")

API_BASE = "https://api.fxtwitter.com/2/status/"
USER_AGENT = "saezuri/1.0 (personal research tool)"
TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
SOURCE_QUERY = "fxtwitter"
WAIT_RANGE = (1.0, 3.0)

STATUS_URL_RE = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:x|twitter|fxtwitter|fixupx|vxtwitter)\.com/(?:[^/?#]+|i(?:/web)?)/status(?:es)?/(\d{2,20})",
    re.IGNORECASE)
ID_RE = re.compile(r"^\d{2,20}$")
HANDLE_RE = re.compile(r"^[0-9A-Za-z_]{1,30}$")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_RATE_LIMITED = 3


class RateLimitedError(Exception):
    """FxTwitterから429を受けた。"""


class FetchError(Exception):
    """1件の取得に失敗した（その投稿だけスキップする）。"""


def parse_status_id(value: str) -> Optional[str]:
    """投稿URLまたは数字のIDから投稿IDを取り出す。読めなければNone。"""
    value = (value or "").strip()
    if ID_RE.match(value):
        return value
    m = STATUS_URL_RE.match(value)
    return m.group(1) if m else None


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _str(value) -> Optional[str]:
    return value if isinstance(value, str) else None


def _media_items(media) -> list[MediaItem]:
    if not isinstance(media, dict):
        return []
    items = media.get("all")
    if not isinstance(items, list):
        items = [*(media.get("photos") or []), *(media.get("videos") or [])]
    out: list[MediaItem] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = _str(item.get("url"))
        kind = _str(item.get("type")) or "photo"
        if not url or not url.startswith("https://"):
            continue
        out.append(MediaItem(type="animated_gif" if kind == "gif" else kind, url=url))
    return out


def parse_fx_status(status: dict, collected_at: str, source_query: str = SOURCE_QUERY) -> Optional[Post]:
    """FxTwitter APIの`status`オブジェクト1件をPostにする。必須項目が欠けていればNone。"""
    if not isinstance(status, dict):
        return None
    post_id = _str(status.get("id"))
    author = status.get("author") if isinstance(status.get("author"), dict) else {}
    handle = _str(author.get("screen_name"))
    text = _str(status.get("text"))
    if not post_id or not ID_RE.match(post_id) or not handle or not HANDLE_RE.match(handle) or text is None:
        logger.warning("FxTwitterの応答に必要な項目がありません(id/author.screen_name/text)。スキップします")
        return None
    replying = status.get("replying_to") if isinstance(status.get("replying_to"), dict) else {}
    quote = status.get("quote") if isinstance(status.get("quote"), dict) else {}
    article = status.get("article") if isinstance(status.get("article"), dict) else {}
    quoted_id = _str(quote.get("id"))
    return Post(
        id=post_id,
        url=f"https://x.com/{handle}/status/{post_id}",
        author_handle=handle,
        author_name=_str(author.get("name")) or handle,
        created_at=_str(status.get("created_at")) or "",
        text=text,
        like_count=_int(status.get("likes")),
        repost_count=_int(status.get("reposts")),
        reply_count=_int(status.get("replies")),
        quote_count=_int(status.get("quotes")),
        collected_at=collected_at,
        lang=_str(status.get("lang")),
        media=_media_items(status.get("media")),
        quoted_id=quoted_id if quoted_id and ID_RE.match(quoted_id) else None,
        source_query=source_query,
        article_title=_str(article.get("title")),
        article_text=_str(article.get("preview_text")),
        in_reply_to_id=_str(replying.get("status")),
    )


def parse_fx_payload(payload: dict, collected_at: str) -> tuple[list[Post], Optional[int]]:
    """APIの応答全体から、投稿本体と（あれば）引用元のPost、表示回数を返す。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), dict):
        return [], None
    status = payload["status"]
    posts: list[Post] = []
    main = parse_fx_status(status, collected_at)
    if main is None:
        return [], None
    posts.append(main)
    quote = status.get("quote")
    if isinstance(quote, dict) and quote.get("id"):
        quoted = parse_fx_status(quote, collected_at, source_query=f"{SOURCE_QUERY}:quoted-by:{main.id}")
        if quoted is not None:
            posts.append(quoted)
    views = status.get("views")
    return posts, views if isinstance(views, int) and not isinstance(views, bool) else None


def fetch_status_json(status_id: str, opener: Callable = urllib.request.urlopen) -> dict:
    """1件分のJSONを取得する。429はRateLimitedError、それ以外の失敗はFetchError。"""
    if not ID_RE.match(status_id):
        raise FetchError(f"投稿IDが不正です: {status_id}")
    request = urllib.request.Request(API_BASE + status_id,
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as res:
            body = res.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimitedError("FxTwitterからアクセス過多(429)と返されました") from exc
        if exc.code in (401, 404):
            raise FetchError(f"投稿 {status_id} は非公開・削除済み・存在しないため取得できません(HTTP {exc.code})") from exc
        raise FetchError(f"投稿 {status_id} の取得に失敗しました(HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"FxTwitterに接続できませんでした: {exc}") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise FetchError(f"投稿 {status_id} の応答が大きすぎます")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FetchError(f"投稿 {status_id} の応答をJSONとして読めませんでした") from exc
    if not isinstance(data, dict):
        raise FetchError(f"投稿 {status_id} の応答が想定外の形です")
    return data


def next_out_path(raw_dir: Path, today: Optional[str] = None) -> Path:
    today = today or datetime.now().strftime("%Y%m%d")
    max_seq = 0
    for path in Path(raw_dir).glob(f"fx_{today}_*.jsonl"):
        suffix = path.stem.rsplit("_", 1)[-1]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return Path(raw_dir) / f"fx_{today}_{max_seq + 1:03d}.jsonl"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ログイン不要で投稿1件ずつの本文・数値を取得(FxTwitter API)")
    parser.add_argument("--url", action="append", default=[], help="投稿URL(複数指定可)")
    parser.add_argument("--id", action="append", default=[], help="投稿ID(複数指定可)")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    return parser


def _raw_dir(config_path: str) -> Path:
    try:
        import yaml

        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        return Path((config.get("output") or {}).get("raw_dir") or "data/raw")
    except (OSError, ImportError, ValueError):
        return Path("data/raw")


def _print_post(post: Post, views: Optional[int]) -> None:
    head = " ".join(post.text.split())
    head = head[:60] + ("…" if len(head) > 60 else "")
    views_text = f" / 表示 {views:,}" if views is not None else ""
    print(f"@{post.author_handle}: いいね {post.like_count:,} / リポスト {post.repost_count:,} / "
          f"返信 {post.reply_count:,} / 引用 {post.quote_count:,}{views_text}")
    # 本文は他人が自由に書ける文字列。行頭に置くと「保存先:」等の見出しに見せかけられるので必ず字下げして出す
    print(f"  本文: {head}")
    print(f"  {post.url}")


def run(args: argparse.Namespace, fetch: Callable[[str], dict] = fetch_status_json,
        sleep: Callable[[float], None] = time.sleep) -> int:
    targets: list[str] = []
    for value in [*args.url, *args.id]:
        status_id = parse_status_id(value)
        if status_id is None:
            print(f"エラー: 投稿URLまたはIDとして読めません: {value}", file=sys.stderr)
            return EXIT_ERROR
        if status_id not in targets:
            targets.append(status_id)
    if not targets:
        print("エラー: --url か --id で投稿を指定してください。", file=sys.stderr)
        return EXIT_ERROR

    collected_at = datetime.now(timezone.utc).isoformat()
    records: list[dict] = []
    seen: set[str] = set()
    failed = 0
    succeeded = 0
    rate_limited = False
    for i, status_id in enumerate(targets):
        if i > 0:
            sleep(random.uniform(*WAIT_RANGE))
        try:
            payload = fetch(status_id)
        except RateLimitedError as exc:
            print(f"エラー: {exc}", file=sys.stderr)
            rate_limited = True
            break
        except FetchError as exc:
            print(f"警告: {exc}", file=sys.stderr)
            failed += 1
            continue
        posts, views = parse_fx_payload(payload, collected_at)
        if not posts:
            print(f"警告: 投稿 {status_id} の応答を読めませんでした（FxTwitterの仕様変更の可能性）", file=sys.stderr)
            failed += 1
            continue
        succeeded += 1
        _print_post(posts[0], views)
        for post in posts:
            if post.id not in seen:
                seen.add(post.id)
                records.append(post.to_dict())

    out_path = None
    if records:
        raw_dir = _raw_dir(args.config)
        raw_dir.mkdir(parents=True, exist_ok=True)
        out_path = next_out_path(raw_dir)
        storage.append_jsonl(out_path, records)
    print(f"取得: {succeeded}件 / 失敗: {failed}件 / 保存: {len(records)}件")
    if out_path:
        print(f"保存先: {out_path}")
    if rate_limited:
        print("ここまでの取得分は保存済みです。時間をあけてから再実行してください。", file=sys.stderr)
        return EXIT_RATE_LIMITED
    return EXIT_OK if not failed else EXIT_ERROR


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return run(build_arg_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
