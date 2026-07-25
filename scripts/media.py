"""saezuri 画像ダウンロード

収集済みのJSONL(data/raw/*.jsonl)に記録されたメディアURLのうち、画像(type="photo")
だけを画像CDN(pbs.twimg.com)から直接ダウンロードして保存する。

digest.pyと同じくXのページへは一切アクセスしない(JSONL読み込みとHTTP GETのみ)ため、
playwright等の収集系依存は持たない。ダウンロードは標準ライブラリのurllib.requestで
逐次実行し、1枚ごとに短いランダム待機を挟む(並列化・自動リトライはしない)。

動画(video/animated_gif)はダウンロード対象外で、件数のみサマリーに出す。
保存先に同名(拡張子違いを含む)のファイルが既にある場合はダウンロードせずスキップするため、
同じJSONLを何度流しても二重取得にはならない。

使用例:
    python scripts/media.py data/raw/related_20260725_003.jsonl
    python scripts/media.py data/raw/*.jsonl --out data/media/foo --max 50 --dry-run

終了コード: 0=正常 / 1=設定・環境エラー / 3=レートリミット(429)検知
(3の場合も検知時点までにダウンロード済みの分は残したまま終了する)
"""
from __future__ import annotations

import argparse
import logging
import mimetypes
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import storage
from digest import classify_record, load_config, resolve_input_paths
from schema import Post

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_MEDIA_DIR = "data/media"

EXIT_ERROR = 1
EXIT_RATE_LIMITED = 3

# 画像1枚ごとの待機秒数の範囲。config.yamlのwait_seconds(スクロール用・3〜15秒)は
# CDNからの単純な画像取得には長すぎるため流用しない。
WAIT_SECONDS_RANGE = (0.5, 2.0)

DOWNLOAD_TIMEOUT_SECONDS = 30
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
CHUNK_SIZE = 64 * 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ダウンロード対象とするMediaItem.type(動画・GIFは対象外)
PHOTO_TYPE = "photo"

# 原寸化の対象ホストと、除去するサイズ指定サフィックス
PBS_HOST = "pbs.twimg.com"
SIZE_SUFFIXES = (":orig", ":large", ":medium", ":small", ":thumb")

ALLOWED_SCHEMES = ("http", "https")

CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
}

FALLBACK_EXT = ".jpg"

# ファイル名に使ってよい文字(Windowsで問題になる文字を落とすためのホワイトリスト)
_UNSAFE_FILENAME_RE = re.compile(r"[^0-9A-Za-z._-]")
_EXT_RE = re.compile(r"\.[0-9A-Za-z]{1,5}$")
_FORMAT_VALUE_RE = re.compile(r"^[0-9A-Za-z]{1,5}$")


class RateLimitedError(RuntimeError):
    """画像CDNからレートリミット(429)を返されたことを表す例外"""


@dataclass
class DownloadTask:
    """ダウンロード1枚分の予定"""

    post_id: str
    index: int
    url: str
    out_dir: Path

    @property
    def stem(self) -> str:
        """拡張子を除いた保存ファイル名(<post_id>_<連番2桁>)"""
        return f"{sanitize_filename(self.post_id)}_{self.index:02d}"


# --- ネットワーク非依存の純関数(単体importで確認可能) -----------------------

def sanitize_filename(value: str) -> str:
    """ファイル名に使えない文字を落とす。空になる場合は"unknown"を返す。"""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", value or "").strip("._")
    return cleaned or "unknown"


def to_original_url(url: str) -> str:
    """pbs.twimg.comの画像URLを原寸(name=orig)版に変換する。

    末尾の :large / :medium / :small / :thumb サフィックスを除去し、
    クエリのnameをorigに置き換える(無ければ付与する)。format等の他のクエリは維持する。
    pbs.twimg.com以外のホストは変換せずそのまま返す。
    """
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != PBS_HOST:
        return url

    path = parts.path
    for suffix in SIZE_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break

    query_items = parse_qsl(parts.query, keep_blank_values=True)
    replaced = False
    for i, (key, _value) in enumerate(query_items):
        if key == "name":
            query_items[i] = (key, "orig")
            replaced = True
    if not replaced:
        query_items.append(("name", "orig"))

    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query_items), parts.fragment))


def is_downloadable_url(url: str) -> bool:
    """URLスキームがhttp/httpsかどうかを判定する(それ以外は取得しない)。"""
    return urlsplit(url).scheme.lower() in ALLOWED_SCHEMES


def extension_from_content_type(content_type: str | None) -> str | None:
    """Content-Typeヘッダから拡張子を決める。判定できなければNone。"""
    main = (content_type or "").split(";")[0].strip().lower()
    if not main:
        return None
    if main in CONTENT_TYPE_EXT:
        return CONTENT_TYPE_EXT[main]
    return mimetypes.guess_extension(main)


def extension_from_url(url: str) -> str | None:
    """URLのパス拡張子、無ければformatクエリから拡張子を決める。判定できなければNone。"""
    parts = urlsplit(url)
    match = _EXT_RE.search(parts.path)
    if match:
        return match.group(0).lower()
    for key, value in parse_qsl(parts.query):
        if key == "format" and _FORMAT_VALUE_RE.match(value):
            return "." + value.lower()
    return None


def resolve_extension(content_type: str | None, url: str) -> str:
    """Content-Type優先、URL由来をフォールバックに拡張子を決める(いずれも不明なら.jpg)。"""
    return extension_from_content_type(content_type) or extension_from_url(url) or FALLBACK_EXT


def is_image_content_type(content_type: str | None) -> bool:
    """Content-Typeが image/ で始まるかを判定する。"""
    return (content_type or "").split(";")[0].strip().lower().startswith("image/")


def load_posts(path: Path) -> list[Post]:
    """1つのJSONLからPost行だけを読み込む。

    トレンド行・解釈できない行は例外を投げずスキップする(全体は落とさない)。
    """
    posts: list[Post] = []
    for record in storage.read_jsonl(path):
        kind = classify_record(record)
        if kind == "trend":
            logger.debug("%s: トレンド行のためスキップします", path)
            continue
        if kind != "post":
            logger.warning("%s: 判別不能な行をスキップします: %s", path, sorted(record.keys()))
            continue
        try:
            posts.append(Post.from_dict(record))
        except (KeyError, TypeError) as exc:
            logger.warning("%s: 行をPostとして解釈できませんでした: %s", path, exc)
    return posts


def collect_tasks(
    posts: list[Post], out_dir: Path, seen_urls: set[str]
) -> tuple[list[DownloadTask], int, int, int]:
    """Post群から画像ダウンロード予定を組み立てる。

    連番はそのPost内のメディアの並び順(動画も1つと数える)を使う。
    seen_urlsは実行内での重複排除用で、この関数内で更新する。
    戻り値は(予定リスト, 画像を持つPost数, 動画スキップ数, 重複スキップ数)。
    """
    tasks: list[DownloadTask] = []
    photo_posts = 0
    video_skipped = 0
    duplicated = 0
    for post in posts:
        has_photo = False
        for index, item in enumerate(post.media, start=1):
            if item.type != PHOTO_TYPE:
                video_skipped += 1
                continue
            has_photo = True
            url = to_original_url(item.url)
            if url in seen_urls:
                duplicated += 1
                continue
            seen_urls.add(url)
            tasks.append(DownloadTask(post_id=post.id, index=index, url=url, out_dir=out_dir))
        if has_photo:
            photo_posts += 1
    return tasks, photo_posts, video_skipped, duplicated


def resolve_out_dir(base: Path, stem: str, single_input: bool, explicit: bool) -> Path:
    """1入力ファイル分の保存先ディレクトリを決める。

    --out指定かつ入力1件のときだけそのディレクトリ直下、それ以外は<base>/<入力stem>/。
    """
    if explicit and single_input:
        return base
    return base / sanitize_filename(stem)


def find_existing(out_dir: Path, stem: str) -> Path | None:
    """既にダウンロード済みのファイル(拡張子違いを含む)があれば返す。"""
    if not out_dir.exists():
        return None
    for path in sorted(out_dir.glob(f"{stem}.*")):
        if path.suffix == ".part" or not path.is_file():
            continue
        return path
    return None


# --- ダウンロード処理 --------------------------------------------------------

def download_image(url: str, out_dir: Path, stem: str) -> Path:
    """1枚をダウンロードして<out_dir>/<stem>.<拡張子>に保存し、保存先パスを返す。

    一時ファイル(.part)へ書き切ってからリネームするため、中断時に壊れたファイルは残らない。
    取得失敗(画像でない・サイズ上限超過・HTTP/接続エラー)はOSError系/ValueErrorとして
    呼び出し元に伝える。429の場合はRateLimitedErrorを送出する(リトライはしない)。
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        response = urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimitedError(
                f"画像CDNからレートリミット(429)を返されました: {url}"
            ) from exc
        raise

    with response:
        content_type = response.headers.get("Content-Type")
        if not is_image_content_type(content_type):
            raise ValueError(f"画像ではないContent-Typeのため保存しません({content_type!r})")

        extension = resolve_extension(content_type, url)
        final_path = out_dir / f"{stem}{extension}"
        if final_path.exists():
            return final_path

        temp_path = out_dir / f"{stem}{extension}.part"
        total = 0
        try:
            with temp_path.open("wb") as f:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError(
                            f"サイズ上限({MAX_DOWNLOAD_BYTES}バイト)を超えたため中断しました"
                        )
                    f.write(chunk)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    temp_path.replace(final_path)
    return final_path


def sleep_between_downloads() -> None:
    """機械的な連続アクセスを避けるための短いランダム待機。"""
    time.sleep(random.uniform(*WAIT_SECONDS_RANGE))


# --- エントリポイント --------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="収集済みJSONLに記録された画像URLから画像をダウンロードする(動画は対象外)"
    )
    parser.add_argument("inputs", nargs="+",
                        help="入力JSONLファイルパス(複数指定可、globパターン可)")
    parser.add_argument("--out", default=None,
                        help="保存先ディレクトリ(省略時はdata/media/<入力JSONLのstem>/。"
                             "入力が複数の場合は指定先の下に入力ファイルごとのサブディレクトリを作る)")
    parser.add_argument("--max", type=int, default=None,
                        help="対象にする画像の最大枚数(先頭からN枚。既存スキップ分も1枚と数える。省略時は無制限)")
    parser.add_argument("--dry-run", action="store_true",
                        help="ダウンロードせず、対象URLと保存予定パスを一覧表示するだけ")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="設定ファイルのパス(既定: config.yaml)")
    return parser


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)

    input_paths = resolve_input_paths(args.inputs)
    if not input_paths:
        print("エラー: 入力ファイルが見つかりません。", file=sys.stderr)
        return EXIT_ERROR

    if args.out:
        base_dir = Path(args.out)
    else:
        base_dir = Path((config.get("output") or {}).get("media_dir", DEFAULT_MEDIA_DIR))

    single_input = len(input_paths) == 1
    seen_urls: set[str] = set()
    tasks: list[DownloadTask] = []
    total_posts = 0
    photo_posts = 0
    video_skipped = 0
    duplicated = 0
    for path in input_paths:
        posts = load_posts(path)
        out_dir = resolve_out_dir(base_dir, path.stem, single_input, explicit=bool(args.out))
        file_tasks, file_photo_posts, file_videos, file_dups = collect_tasks(
            posts, out_dir, seen_urls
        )
        total_posts += len(posts)
        photo_posts += file_photo_posts
        video_skipped += file_videos
        duplicated += file_dups
        tasks.extend(file_tasks)

    found = len(tasks)
    if args.max is not None and args.max >= 0:
        tasks = tasks[: args.max]

    downloaded = 0
    existing = 0
    failed = 0
    rate_limited: RateLimitedError | None = None
    fetched_any = False

    for task in tasks:
        if not is_downloadable_url(task.url):
            logger.warning("http/https以外のURLのためスキップします: %s", task.url)
            failed += 1
            continue

        already = find_existing(task.out_dir, task.stem)
        if already is not None:
            logger.debug("既に存在するためスキップします: %s", already)
            existing += 1
            continue

        if args.dry_run:
            extension = extension_from_url(task.url) or FALLBACK_EXT
            print(f"[dry-run] {task.url} -> {task.out_dir / (task.stem + extension)}")
            continue

        try:
            task.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"エラー: 保存先ディレクトリを作成できません({task.out_dir}): {exc}",
                  file=sys.stderr)
            return EXIT_ERROR

        if fetched_any:
            sleep_between_downloads()
        fetched_any = True

        try:
            saved = download_image(task.url, task.out_dir, task.stem)
        except RateLimitedError as exc:
            rate_limited = exc
            break
        except (OSError, ValueError) as exc:
            logger.warning("ダウンロードに失敗しました(スキップ): %s: %s", task.url, exc)
            failed += 1
            continue
        downloaded += 1
        logger.info("保存しました: %s", saved)

    out_dirs = sorted({str(task.out_dir) for task in tasks})

    limited = "" if found == len(tasks) else f"(全{found}枚のうち--maxで{len(tasks)}枚に制限)"
    print(f"入力ファイル: {len(input_paths)}件 / ポスト: {total_posts}件 /"
          f" 画像を持つポスト: {photo_posts}件 / 対象画像: {len(tasks)}枚{limited}")
    if args.dry_run:
        print(f"既存スキップ: {existing}枚 / 重複スキップ: {duplicated}枚 /"
              f" 動画スキップ: {video_skipped}枚（dry-runのためダウンロードはしていません）")
    else:
        print(f"ダウンロード: {downloaded}枚 / 既存スキップ: {existing}枚 / 失敗: {failed}枚 /"
              f" 重複スキップ: {duplicated}枚 / 動画スキップ: {video_skipped}枚")
    for out_dir in out_dirs:
        print(f"保存先: {out_dir}")

    if rate_limited is not None:
        print(f"エラー: {rate_limited}", file=sys.stderr)
        print("検知時点までのダウンロード分は保存済みです。残りは処理していません。"
              "時間を置いてから再実行してください(既存分はスキップされます)。", file=sys.stderr)
        return EXIT_RATE_LIMITED
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_arg_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
