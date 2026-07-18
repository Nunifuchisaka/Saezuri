"""saezuri JSONL→Markdownダイジェスト生成

data/raw/*.jsonl (Post/TrendEntry共通スキーマ)を読み込み、人が読むための
Markdownダイジェストを生成する。Xへの実アクセスは行わない(JSONL読み込みと
文字列整形のみ)ため、playwright等の収集系依存は持たない。

各行は "trend_word" キーの有無でTrendEntry、"id"+"author_handle" の有無で
Postと判定し、それぞれ別セクションにまとめる(混在するJSONLも扱える)。

エンゲージメント(like+repost+reply+quote)順に並べた「注目ポスト」セクションでは
著作権配慮のため本文の全文転載はせず、要約(先頭一定文字)+出典URLのみを載せる。

使用例:
    python scripts/digest.py data/raw/search_20260718.jsonl --title "VRChat アップデート"
    python scripts/digest.py data/raw/*.jsonl --output digest/my_digest.md
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from datetime import datetime
from pathlib import Path

import storage
from schema import Post, TrendEntry

DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_DIGEST_DIR = "digest"

# 「注目ポスト」セクションに載せる最大件数
TOP_POSTS_DEFAULT = 20

# 本文要約の最大文字数(全文転載回避のための切り詰め)
SUMMARY_MAX_CHARS = 100


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    """config.yamlを読み込んで辞書として返す。

    PyYAML未インストール・ファイル未存在等の場合は警告を出し空辞書(既定値扱い)を返す。
    """
    try:
        import yaml
    except ImportError:
        print("警告: PyYAMLが見つからないためconfig.yamlを読み込めません。既定値で動作します。",
              file=sys.stderr)
        return {}
    path = Path(path)
    if not path.exists():
        print(f"警告: 設定ファイルが見つかりません({path})。既定値で動作します。", file=sys.stderr)
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_input_paths(patterns: list[str]) -> list[Path]:
    """globパターン・単純パスの混在リストを実在ファイルパスのリストに展開する(重複除去)。"""
    paths: list[Path] = []
    seen: set[str] = set()
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if not matches and Path(pattern).exists():
            matches = [pattern]
        for match in matches:
            key = str(Path(match).resolve())
            if key in seen:
                continue
            seen.add(key)
            paths.append(Path(match))
    return paths


def classify_record(record: dict) -> str:
    """1行分の辞書がPost/TrendEntryいずれの形式かを判定する。"""
    if "trend_word" in record:
        return "trend"
    if "id" in record and "author_handle" in record:
        return "post"
    return "unknown"


def load_records(paths: list[Path]) -> tuple[list[Post], list[TrendEntry]]:
    """入力JSONL群を読み込み、Post/TrendEntryのリストへ振り分ける。

    解釈できない行は警告を出してスキップし、全体は落とさない。
    """
    posts: list[Post] = []
    trends: list[TrendEntry] = []
    for path in paths:
        for record in storage.read_jsonl(path):
            kind = classify_record(record)
            if kind == "post":
                try:
                    posts.append(Post.from_dict(record))
                except (KeyError, TypeError) as exc:
                    print(f"警告: {path} の行をPostとして解釈できませんでした: {exc}", file=sys.stderr)
            elif kind == "trend":
                try:
                    trends.append(TrendEntry.from_dict(record))
                except (KeyError, TypeError) as exc:
                    print(f"警告: {path} の行をTrendEntryとして解釈できませんでした: {exc}", file=sys.stderr)
            else:
                print(f"警告: {path} に判別不能な行があります(スキップ): {sorted(record.keys())}",
                      file=sys.stderr)
    return posts, trends


def engagement_score(post: Post) -> int:
    """注目ポストの並べ替えに使うエンゲージメント合計値。"""
    return post.like_count + post.repost_count + post.reply_count + post.quote_count


def summarize_text(text: str, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """本文を1行の要約に整形する(改行つぶし+一定文字数で切り詰め)。全文転載はしない。"""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[:max_chars].rstrip() + "…"


def _minmax(values: list[str]) -> tuple[str | None, str | None]:
    values = [v for v in values if v]
    if not values:
        return None, None
    return min(values), max(values)


def format_yaml_list(items: list[str]) -> str:
    """tags等のフラットな文字列リストをYAMLのインラインリスト表記にする。"""
    if not items:
        return "[]"
    escaped = [item.replace('"', '\\"') for item in items]
    return "[" + ", ".join(f'"{item}"' for item in escaped) + "]"


def build_source(title: str | None, posts: list[Post], input_paths: list[Path]) -> str:
    """frontmatterのsourceを構成する(--title優先、無ければsource_query、それも無ければ入力ファイル名)。"""
    if title:
        return title
    queries = sorted({p.source_query for p in posts if p.source_query})
    if queries:
        return "、".join(queries)
    return "、".join(path.stem for path in input_paths)


def build_frontmatter(tags: list[str], source: str, created: str) -> str:
    escaped_source = source.replace('"', '\\"')
    lines = [
        "---",
        "type: reference",
        f"tags: {format_yaml_list(tags)}",
        f'source: "{escaped_source}"',
        f"created: {created}",
        "---",
    ]
    return "\n".join(lines)


def build_collection_section(posts: list[Post], trends: list[TrendEntry]) -> str:
    queries = sorted({p.source_query for p in posts if p.source_query})
    query_line = "、".join(queries) if queries else "(該当なし。トレンドのみ、またはsource_query未記録)"

    created_min, created_max = _minmax([p.created_at for p in posts])
    period_line = f"{created_min} 〜 {created_max}" if created_min else "(該当なし。ポストデータなし)"

    collected_min, collected_max = _minmax(
        [p.collected_at for p in posts] + [t.collected_at for t in trends]
    )
    collected_line = f"{collected_min} 〜 {collected_max}" if collected_min else "(不明)"

    lines = [
        "## 収集条件",
        "",
        f"- クエリ: {query_line}",
        f"- 期間: {period_line}",
        f"- 件数: ポスト {len(posts)}件 / トレンド語 {len(trends)}件",
        f"- 収集日時: {collected_line}",
        "",
    ]
    return "\n".join(lines)


def build_notable_posts_section(posts: list[Post], top_n: int = TOP_POSTS_DEFAULT) -> str:
    lines = ["## 注目ポスト", ""]
    if not posts:
        lines.append("(該当するポストデータなし)")
        lines.append("")
        return "\n".join(lines)

    ranked = sorted(posts, key=engagement_score, reverse=True)[:top_n]
    for post in ranked:
        summary = summarize_text(post.text)
        score = engagement_score(post)
        lines.append(
            f"- **{post.created_at}** {post.author_name}（@{post.author_handle}）: "
            f"{summary} — [出典]({post.url})（エンゲージメント: {score}）"
        )
    lines.append("")
    return "\n".join(lines)


def build_trends_section(trends: list[TrendEntry]) -> str:
    lines = ["## トレンド語", ""]
    if not trends:
        lines.append("(該当するトレンドデータなし)")
        lines.append("")
        return "\n".join(lines)

    # trend_word重複は収集日時が最も新しいものを代表として残す
    latest: dict[str, TrendEntry] = {}
    for entry in trends:
        current = latest.get(entry.trend_word)
        if current is None or (entry.collected_at or "") >= (current.collected_at or ""):
            latest[entry.trend_word] = entry

    ordered = sorted(
        latest.values(),
        key=lambda e: (e.rank if e.rank is not None else float("inf"), -(e.tweet_volume or 0)),
    )
    for entry in ordered:
        volume = f"{entry.tweet_volume}件" if entry.tweet_volume is not None else "不明"
        rank = entry.rank if entry.rank is not None else "-"
        lines.append(f"- **{entry.trend_word}** — 言及数: {volume}（rank: {rank}, 収集: {entry.collected_at}）")
    lines.append("")
    return "\n".join(lines)


def build_raw_links_section(input_paths: list[Path], output_path: Path) -> str:
    lines = ["## 生データ", ""]
    output_dir = output_path.resolve().parent
    for path in input_paths:
        rel = os.path.relpath(path.resolve(), output_dir).replace("\\", "/")
        lines.append(f"- [{path.name}]({rel})")
    lines.append("")
    return "\n".join(lines)


def build_digest(
    posts: list[Post],
    trends: list[TrendEntry],
    input_paths: list[Path],
    output_path: Path,
    title: str | None,
    tags: list[str],
) -> str:
    """各セクションを組み立て、ダイジェストMarkdown全文を返す。"""
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    source = build_source(title, posts, input_paths)
    heading = title or source

    sections = [
        build_frontmatter(tags, source, created),
        "",
        f"# {heading}",
        "",
        build_collection_section(posts, trends),
        build_notable_posts_section(posts),
        build_trends_section(trends),
        build_raw_links_section(input_paths, output_path),
    ]
    return "\n".join(sections).rstrip() + "\n"


def resolve_output_path(args: argparse.Namespace, config: dict) -> Path:
    if args.output:
        return Path(args.output)
    digest_dir = Path((config.get("output") or {}).get("digest_dir", DEFAULT_DIGEST_DIR))
    filename = f"digest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    return digest_dir / filename


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JSONL(Post/TrendEntry)からMarkdownダイジェストを生成する")
    parser.add_argument("inputs", nargs="+",
                        help="入力JSONLファイルパス(複数指定可、globパターン可)")
    parser.add_argument("--output", default=None,
                        help="出力Markdownファイルパス(省略時はconfig.yamlのoutput.digest_dir配下に日時付きで自動生成)")
    parser.add_argument("--title", default=None,
                        help="frontmatterのsourceやドキュメント見出しに使う説明(例: 検索クエリ名)")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="設定ファイルのパス(既定: config.yaml)")
    return parser


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)

    input_paths = resolve_input_paths(args.inputs)
    if not input_paths:
        print("エラー: 入力ファイルが見つかりません。", file=sys.stderr)
        return 1

    posts, trends = load_records(input_paths)
    output_path = resolve_output_path(args, config)
    tags = [str(k) for k in (config.get("interest_keywords") or [])]

    markdown = build_digest(posts, trends, input_paths, output_path, args.title, tags)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    print(f"入力ファイル: {len(input_paths)}件 / ポスト: {len(posts)}件 / トレンド語: {len(trends)}件")
    print(f"出力: {output_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
