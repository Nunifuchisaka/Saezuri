"""saezuri 起点投稿へのリプライ・引用・コメントのいいね数ランキング

related.py(X) / ig_related.py(Instagram) / th_related.py(Threads)が共通で使う。
収集済みのPostから「起点投稿への直接の返信/コメント、または引用」を選び、
いいね数のみの降順で上位N件を取り出してMarkdownに書き出す。
Playwright非依存で、SNSへのアクセスは一切行わない。

起点投稿者本人による返信・自己引用はランキングから除外する(JSONLには残る)。
Markdownは digest.py と同じ著作権配慮で、本文は先頭100字の要約+出典URLのみとし全文は載せない。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable, Optional

from pick import _summary, to_jst
from schema import Post

DEFAULT_TOP = 10


def rank_by_likes(
    posts: list[Post],
    is_target: Callable[[Post], bool],
    origin_id: str,
    origin_handle: Optional[str],
    top: int,
) -> list[Post]:
    """is_targetを満たす投稿(起点自身・起点投稿者本人を除く)をいいね数の降順で上位top件返す。

    いいね数が同じ場合は収集順を保つ。origin_handleが不明(None)なら本人除外は行わない。
    """
    origin_norm = origin_handle.lstrip("@").lower() if origin_handle else None
    targets = [
        post for post in posts
        if post.id != origin_id
        and not (origin_norm and post.author_handle.lower() == origin_norm)
        and is_target(post)
    ]
    return sorted(targets, key=lambda p: p.like_count, reverse=True)[:top]


def build_sections(
    posts: list[Post],
    specs: list[tuple[str, Callable[[Post], bool]]],
    origin_id: str,
    origin_handle: Optional[str],
    top: int,
) -> list[tuple[str, list[Post], int]]:
    """specs=[(種別の見出し, 対象判定), ...]ごとに(見出し, 上位top件, 対象件数)を返す。"""
    sections = []
    for label, is_target in specs:
        targets = rank_by_likes(posts, is_target, origin_id, origin_handle, top=len(posts))
        sections.append((label, targets[:top], len(targets)))
    return sections


def render_line(rank: int, post: Post) -> str:
    """順位・いいね数・投稿者・日時(JST)・本文先頭100字・URLの1行を作る。"""
    jst = to_jst(post)
    when = jst.strftime("%Y-%m-%d %H:%M") if jst else "日時不明"
    return (
        f"{rank}. いいね {post.like_count} / @{post.author_handle} / {when}"
        f" / {_summary(post.text)} / {post.url}"
    )


def render_top_markdown(
    title: str,
    origin_url: str,
    origin_handle: Optional[str],
    generated_at: str,
    sections: list[tuple[str, list[Post], int]],
    notes: Optional[list[str]] = None,
) -> str:
    """sections=[(種別の見出し, 上位の投稿リスト, 対象件数), ...]からMarkdown全体を作る。"""
    lines = [
        f"# {title}",
        "",
        f"- 起点: {origin_url}",
        f"- 起点投稿者: @{origin_handle}" if origin_handle else "- 起点投稿者: 不明(本人除外なし)",
        f"- 生成日時: {generated_at}",
        "- 並び: いいね数の降順(起点への直接の返信/コメント・引用のみ。起点投稿者本人の投稿は除外)",
        "- 本文は先頭100字の要約のみ。全文は各URLを参照",
    ]
    lines.extend(f"- {note}" for note in notes or [])
    for label, posts, total in sections:
        lines.extend(["", f"## {label}(対象{total}件中 上位{len(posts)}件)", ""])
        if not posts:
            lines.append("(該当なし)")
            continue
        lines.extend(render_line(rank, post) for rank, post in enumerate(posts, start=1))
    return "\n".join(lines) + "\n"


def top_markdown_path(digest_dir: str | Path, sns: str, origin_key: str, today: date | None = None) -> Path:
    """digest_dir/top_<sns>_<起点ID等>_<日付>.md のパスを返す(同日の再実行は上書き)。"""
    today = today or date.today()
    return Path(digest_dir) / f"top_{sns}_{origin_key}_{today.strftime('%Y%m%d')}.md"


def write_top_markdown(path: str | Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path
