"""saezuri 収集済みJSONLからの投稿抽出

収集済みの data/raw/*.jsonl から、アカウント・キーワード・期間などで
投稿を絞り込み、Markdown(月見出し→一覧)またはJSONLで出力する。
Playwright非依存で、Xへのアクセスは一切行わない。

使用例:
    python scripts/pick.py data/raw/search_*.jsonl --handle aichan_nel --exclude-reposts --since 2026-01-01 --top-per month --top 3
    python scripts/pick.py data/raw/*.jsonl --keyword VRChat --keyword 配信 --format jsonl
    python scripts/pick.py data/raw/related_*.jsonl --kind reply --top 10

--since/--until はJST基準の日付で、両端とも含む(--until 2026-03-31 は3/31分まで)。
終了コード: 0=正常 / 1=入力エラー
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import storage
from schema import Post

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
X_DATETIME_FORMAT = "%a %b %d %H:%M:%S %z %Y"
SUMMARY_LENGTH = 100

# 関連収集(related.py / ig_related.py / th_related.py)のsource_query: <接頭辞>:<起点>:<種別>
KINDS = ("reply", "quote", "comment")
_KIND_SOURCE_RE = re.compile(r"^(?:related|ig-related|th-related):[^:]+:(\w+)$")


def parse_date_arg(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"日付はYYYY-MM-DD形式で指定してください: {value}")


def parse_created_at(value: str) -> datetime | None:
    """"Mon Sep 21 03:28:32 +0000 2026"形式(およびISO 8601)をtz付きdatetimeに変換する。失敗時はNone。"""
    try:
        return datetime.strptime(value, X_DATETIME_FORMAT)
    except (ValueError, TypeError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def to_jst(post: Post) -> datetime | None:
    parsed = parse_created_at(post.created_at)
    return parsed.astimezone(JST) if parsed else None


def engagement(post: Post) -> int:
    return post.like_count + post.repost_count + post.reply_count + post.quote_count


def load_posts(paths: list[str]) -> list[Post]:
    """JSONLからPostを読み込む(id重複は先勝ちで除去。Post以外・解釈不能な行はスキップ)。"""
    posts: list[Post] = []
    seen: set[str] = set()
    for path in paths:
        for record in storage.read_jsonl(path):
            if "id" not in record or "author_handle" not in record:
                continue
            try:
                post = Post.from_dict(record)
            except (KeyError, TypeError) as exc:
                logger.warning("%s: 解釈できない行をスキップしました: %s", path, exc)
                continue
            if post.id in seen:
                continue
            seen.add(post.id)
            posts.append(post)
    return posts


def post_kind(post: Post) -> str | None:
    """関連収集のsource_query末尾から種別(reply/quote/comment等)を返す。関連収集以外はNone。"""
    match = _KIND_SOURCE_RE.match(post.source_query or "")
    return match.group(1) if match else None


def matches_keywords(post: Post, keywords: list[str]) -> bool:
    """keywordsのいずれかがtext/記事タイトル/記事本文に含まれるか(大文字小文字無視)。空ならTrue。"""
    if not keywords:
        return True
    haystack = "\n".join(filter(None, (post.text, post.article_title, post.article_text))).lower()
    return any(keyword.lower() in haystack for keyword in keywords)


def filter_posts(
    posts: list[Post],
    handle: str | None = None,
    keywords: list[str] | None = None,
    since: date | None = None,
    until: date | None = None,
    exclude_reposts: bool = False,
    kinds: list[str] | None = None,
) -> list[Post]:
    handle_norm = handle.lstrip("@").lower() if handle else None
    result = []
    for post in posts:
        if handle_norm and post.author_handle.lower() != handle_norm:
            continue
        if exclude_reposts and post.is_repost:
            continue
        if kinds and post_kind(post) not in kinds:
            continue
        if not matches_keywords(post, keywords or []):
            continue
        if since is not None or until is not None:
            jst = to_jst(post)
            if jst is None:
                logger.warning("created_atを解釈できないためスキップします: id=%s", post.id)
                continue
            if since is not None and jst.date() < since:
                continue
            if until is not None and jst.date() > until:
                continue
        result.append(post)
    return result


def _sort_key(post: Post) -> datetime:
    return to_jst(post) or datetime.min.replace(tzinfo=JST)


def month_key(post: Post) -> str:
    jst = to_jst(post)
    return jst.strftime("%Y-%m") if jst else "日時不明"


def select_top(posts: list[Post], top_per: str, top: int | None) -> list[Post]:
    """top_per=month: 月(JST)ごとにエンゲージメント上位top件 / none: topがあれば全体の上位top件。
    結果は投稿日時の昇順で返す。"""
    if top is None:
        return sorted(posts, key=_sort_key)
    if top_per == "month":
        groups: dict[str, list[Post]] = defaultdict(list)
        for post in posts:
            groups[month_key(post)].append(post)
        picked = [p for group in groups.values() for p in sorted(group, key=engagement, reverse=True)[:top]]
    else:
        picked = sorted(posts, key=engagement, reverse=True)[:top]
    return sorted(picked, key=_sort_key)


def _summary(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:SUMMARY_LENGTH] + ("…" if len(flat) > SUMMARY_LENGTH else "")


def render_markdown(posts: list[Post]) -> str:
    lines: list[str] = []
    current = None
    for post in posts:
        month = month_key(post)
        if month != current:
            if current is not None:
                lines.append("")
            lines.append(f"## {month}")
            lines.append("")
            current = month
        jst = to_jst(post)
        day = jst.strftime("%Y-%m-%d") if jst else "日時不明"
        lines.append(
            f"- {day} / エンゲージメント{engagement(post)}"
            f"(like {post.like_count} / repost {post.repost_count}"
            f" / reply {post.reply_count} / quote {post.quote_count})"
        )
        lines.append(f"  {_summary(post.text)}")
        lines.append(f"  {post.url}")
    return "\n".join(lines) + ("\n" if lines else "")


def render_jsonl(posts: list[Post]) -> str:
    return "".join(json.dumps(p.to_dict(), ensure_ascii=False) + "\n" for p in posts)


def expand_paths(patterns: list[str]) -> list[str]:
    """ワイルドカードを含み実在しないパターンはglob展開する(PowerShellはシェル側で展開しないため)。"""
    paths: list[str] = []
    for pattern in patterns:
        if not Path(pattern).exists() and any(c in pattern for c in "*?["):
            paths.extend(sorted(glob.glob(pattern)))
        else:
            paths.append(pattern)
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="収集済みJSONLから投稿を絞り込んで抽出する")
    parser.add_argument("files", nargs="+", help="入力JSONLファイル(複数可・ワイルドカード可)")
    parser.add_argument("--handle", default=None, help="author_handleで絞る(@は除去・大文字小文字無視)")
    parser.add_argument("--keyword", action="append", default=[],
                        help="text/記事本文の部分一致(複数指定はOR・大文字小文字無視)")
    parser.add_argument("--since", type=parse_date_arg, default=None, metavar="YYYY-MM-DD",
                        help="この日(JST)以降")
    parser.add_argument("--until", type=parse_date_arg, default=None, metavar="YYYY-MM-DD",
                        help="この日(JST)まで(含む)")
    parser.add_argument("--exclude-reposts", action="store_true", help="リポストを除外する")
    parser.add_argument("--kind", action="append", choices=KINDS, default=[],
                        help="関連収集の種別で絞る(source_query末尾で判別。複数指定はOR)")
    parser.add_argument("--top-per", choices=("month", "none"), default="none",
                        help="month=月ごとに上位--top件 / none=全体(--top指定時は全体の上位)。既定: none")
    parser.add_argument("--top", type=int, default=None, metavar="N", help="エンゲージメント上位N件")
    parser.add_argument("--format", choices=("md", "jsonl"), default="md", help="出力形式。既定: md")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_arg_parser().parse_args(argv)
    if args.top_per == "month" and args.top is None:
        print("エラー: --top-per monthには--topが必要です", file=sys.stderr)
        return 1
    if args.top is not None and args.top < 1:
        print("エラー: --topは1以上で指定してください", file=sys.stderr)
        return 1
    paths = expand_paths(args.files)
    missing = [p for p in paths if not Path(p).exists()]
    if missing or not paths:
        print(f"エラー: 入力ファイルが見つかりません: {missing or args.files}", file=sys.stderr)
        return 1

    posts = filter_posts(
        load_posts(paths), args.handle, args.keyword, args.since, args.until, args.exclude_reposts,
        args.kind,
    )
    picked = select_top(posts, args.top_per, args.top)
    output = render_jsonl(picked) if args.format == "jsonl" else render_markdown(picked)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(output)
    print(f"抽出: {len(picked)}件", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
