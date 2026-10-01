"""saezuri Threadsアカウントのプロフィール情報(フォロワー数等)取得

config.yamlのthreads.watch_accountsに列挙したユーザー名それぞれについて
threads.com/@<username> を1回だけ開き、ページ埋め込みJSON(<script type="application/json">)の
userノードからフォロワー数(follower_count=正確値)・表示名・プロフィール文・認証済みか等を取得する。
埋め込みJSONに無かった場合のみ、ページの表示テキスト(「フォロワー4.3万人」等)から概数を取得し、
followers_is_approx=Trueで区別する。スクロールはしない。差分管理(state)も行わず、
実行ごとの時点スナップショットとして追記する。
結果は標準出力に表示し、data/raw/th_profiles_<日付>.jsonl に追記する。

事前に th_login.py で手動ログインしておくこと。

使用例:
    python scripts/th_profiles.py                    # threads.watch_accounts全件
    python scripts/th_profiles.py --user some_brand  # 特定の1アカウントのみ

終了コード: 0=正常 / 1=設定・環境エラー(取得できないアカウントがあった場合も1) /
2=セッション失効 / 3=レートリミット(429)検知 / 4=チャレンジ画面検知
(2・3・4の場合も検知時点までの取得分は保存し、残りのアカウントは処理しない。リトライはしない)

注意: 取得したJSONLはPost/TrendEntryではないため、digest.pyには渡さないこと。
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from playwright.sync_api import sync_playwright

import storage
from capture import (
    ChallengeDetectedError,
    RateLimitedError,
    get_wait_seconds_range,
    now_iso,
)
from session import (
    SUPPORTED_BROWSERS,
    SessionExpiredError,
    get_browser_executable_path,
    get_browser_name,
    launch_browser,
    load_config,
)
from threads import (
    ThreadsCapture,
    get_threads_watch_accounts,
    new_threads_context,
    require_threads_login,
)

logger = logging.getLogger(__name__)

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4

# ページ表示後、埋め込みJSONを読む前に待つ時間(描画・ハイドレーション待ち)
EMBEDDED_WAIT_MS = 3000

SOURCE_EMBEDDED_JSON = "embedded_json"
SOURCE_PAGE_TEXT = "page_text"

# プロフィールのuserノード(follower_countを持つ)を含む埋め込みJSONだけを取り出すJS
# (投稿が0件のアカウントでもtext_post_app_infoは無いため、目印はfollower_count)
PROFILE_SCRIPTS_JS = """
() => Array.from(document.querySelectorAll('script[type="application/json"]'))
  .map(el => el.textContent || '')
  .filter(text => text.includes('follower_count'))
"""

BODY_TEXT_JS = "() => document.body ? document.body.innerText : ''"


@dataclass
class ThProfile:
    """Threadsアカウント1件分のプロフィールスナップショット"""

    handle: str
    user_id: str
    name: str
    description: str
    followers_count: int
    followers_is_approx: bool
    followers_source: str
    collected_at: str
    is_verified: Optional[bool] = None
    is_private: Optional[bool] = None
    url: Optional[str] = None
    profile_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 表示テキスト→数値
# ---------------------------------------------------------------------------

_UNIT_MULTIPLIERS = {"万": 10_000, "億": 100_000_000, "k": 1_000, "m": 1_000_000}
_COUNT_RE = re.compile(r"^\s*(\d[\d,]*(?:\.\d+)?)\s*([万億KkMm]?)\s*人?\s*$")
_FOLLOWER_JA_RE = re.compile(r"フォロワー\s*(\d[\d,]*(?:\.\d+)?\s*[万億]?)\s*人?")
_FOLLOWER_EN_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?\s*[KkMm]?)\s*followers", re.IGNORECASE)


def parse_follower_text(text: Optional[str]) -> Optional[int]:
    """「4.3万人」「1.2億」「1,234人」「500人」「43.7K」等の表示テキストを整数にする。

    単位付きの値は表示上の丸めがあるため概数。数値として解釈できなければNone。
    """
    if not isinstance(text, str):
        return None
    match = _COUNT_RE.match(text)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    unit = match.group(2).lower()
    return round(number * _UNIT_MULTIPLIERS.get(unit, 1))


def find_follower_text(page_text: Optional[str]) -> Optional[str]:
    """ページの表示テキストから「フォロワー4.3万人」「43.7K followers」の数値部分を抜き出す。"""
    if not isinstance(page_text, str):
        return None
    for pattern in (_FOLLOWER_JA_RE, _FOLLOWER_EN_RE):
        match = pattern.search(page_text)
        if match:
            return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# 正規化
# ---------------------------------------------------------------------------

def _profile_url(handle: str) -> str:
    return f"https://www.threads.com/@{handle}"


def _find_user_nodes(payload: Any) -> list[dict]:
    """payloadから「usernameとfollower_countキーを持つdict」を再帰探索で集める。

    ルートキー(data.user等)は非公開仕様で変わりうるため決め打ちしない。見つけたノードの内側は掘らない。
    """
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("username"), str) and "follower_count" in node:
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _bool_or_none(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _first_bio_link(user: dict) -> Optional[str]:
    links = user.get("bio_links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and isinstance(link.get("url"), str) and link["url"]:
                return link["url"]
    return None


def parse_profile_payload(
    payload: Any,
    operation: str = "",
    handle: Optional[str] = None,
    collected_at: Optional[str] = None,
) -> Optional[ThProfile]:
    """ページ埋め込みJSON1件からThProfile(正確値)を抽出する。

    handle指定時は大文字小文字を無視して一致するユーザーだけを採る。
    対象ユーザーが居ない・follower_countが整数でない場合は警告を出してNone。
    """
    collected_at = collected_at or now_iso()
    users = _find_user_nodes(payload)
    if handle:
        wanted = handle.lstrip("@").lower()
        users = [u for u in users if u["username"].lower() == wanted]
    if not users:
        return None

    user = users[0]
    followers = user.get("follower_count")
    if isinstance(followers, bool) or not isinstance(followers, int):
        logger.warning("%s: follower_countが数値ではありません(handle=%s, API構造変更の可能性)",
                       operation, user["username"])
        return None

    username = user["username"]
    return ThProfile(
        handle=username,
        user_id=str(user.get("pk") or user.get("id") or ""),
        name=_str_or_none(user.get("full_name")) or "",
        description=_str_or_none(user.get("biography")) or "",
        followers_count=followers,
        followers_is_approx=False,
        followers_source=SOURCE_EMBEDDED_JSON,
        collected_at=collected_at,
        is_verified=_bool_or_none(user.get("is_verified")),
        is_private=_bool_or_none(user.get("text_post_app_is_private")),
        url=_first_bio_link(user),
        profile_url=_profile_url(username),
    )


def build_profile_from_text(
    handle: str,
    page_text: Optional[str],
    collected_at: Optional[str] = None,
) -> Optional[ThProfile]:
    """埋め込みJSONに無い場合の代替。表示テキストのフォロワー表記から概数のThProfileを作る。"""
    count = parse_follower_text(find_follower_text(page_text))
    if count is None:
        return None
    return ThProfile(
        handle=handle,
        user_id="",
        name="",
        description="",
        followers_count=count,
        followers_is_approx=True,
        followers_source=SOURCE_PAGE_TEXT,
        collected_at=collected_at or now_iso(),
        profile_url=_profile_url(handle),
    )


def pick_profile_scripts(texts: Iterable[str]) -> list[str]:
    """埋め込みJSONの文字列群から、プロフィール(follower_count)を含むものだけを返す。"""
    return [t for t in texts if isinstance(t, str) and "follower_count" in t]


# ---------------------------------------------------------------------------
# 収集
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Threadsアカウントのプロフィール情報(フォロワー数等)取得")
    parser.add_argument("--user", default=None,
                        help="特定の1アカウントだけ実行(@は不要。省略時はthreads.watch_accounts全件)")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def resolve_users(args: Any, config: dict) -> list[str]:
    """対象ユーザー名のリストを決める(--user指定が優先、@は除去)。"""
    if args.user:
        return [args.user.strip().lstrip("@")]
    return get_threads_watch_accounts(config)


def _load_profile_payloads(page) -> list[dict]:
    try:
        texts = page.evaluate(PROFILE_SCRIPTS_JS)
    except Exception as exc:
        logger.warning("embedded: ページ埋め込みJSONの読み出しに失敗しました: %s", exc)
        return []
    payloads: list[dict] = []
    for text in pick_profile_scripts(texts or []):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            logger.warning("embedded: JSONのパースに失敗したため1件スキップします: %s", exc)
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def collect_profile(context, username: str) -> tuple[Optional[ThProfile], Optional[Exception]]:
    """1ユーザー分を1回だけ開いて取得する。ブロック・失効検知時は(None, 例外)を返す。"""
    collected_at = now_iso()
    page = context.new_page()
    capture = ThreadsCapture()
    capture.attach(page)
    try:
        page.goto(_profile_url(username), wait_until="domcontentloaded")
        page.wait_for_timeout(EMBEDDED_WAIT_MS)
        capture.raise_if_blocked(page)
        for payload in _load_profile_payloads(page):
            profile = parse_profile_payload(
                payload, "embedded", handle=username, collected_at=collected_at
            )
            if profile is not None:
                return profile, None
        try:
            page_text = page.evaluate(BODY_TEXT_JS)
        except Exception as exc:
            logger.warning("page_text: 表示テキストの読み出しに失敗しました: %s", exc)
            page_text = ""
        return build_profile_from_text(username, page_text, collected_at=collected_at), None
    except (RateLimitedError, ChallengeDetectedError, SessionExpiredError) as exc:
        return None, exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("ThreadsCaptureのdetachに失敗しました: %s", exc)
        page.close()


def format_profile(profile: ThProfile) -> str:
    approx = "(概数)" if profile.followers_is_approx else ""
    return (f"@{profile.handle} ({profile.name}): フォロワー {profile.followers_count:,}{approx}"
            f" [{profile.followers_source}]")


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)

    users = resolve_users(args, config)
    if not users:
        print("エラー: 対象アカウントがありません。config.yamlのthreads.watch_accountsを設定するか"
              "--userを指定してください。", file=sys.stderr)
        return EXIT_ERROR

    out_path = raw_dir / f"th_profiles_{datetime.now().strftime('%Y%m%d')}.jsonl"
    profiles: list[ThProfile] = []
    missing: list[str] = []
    blocked: Exception | None = None

    try:
        with sync_playwright() as playwright:
            browser = launch_browser(
                playwright, browser_name, headless=not args.headed,
                executable_path=get_browser_executable_path(config),
            )
            try:
                context = new_threads_context(browser, config)
                require_threads_login(context)
                for index, username in enumerate(users):
                    if index:
                        time.sleep(random.uniform(*wait_range))
                    profile, blocked = collect_profile(context, username)
                    if blocked is not None:
                        break
                    if profile is None:
                        missing.append(username)
                        print(f"@{username}: プロフィール情報を取得できませんでした")
                        continue
                    profiles.append(profile)
                    print(format_profile(profile))
            finally:
                browser.close()
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SessionExpiredError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_SESSION_EXPIRED

    if profiles:
        written = storage.append_jsonl(out_path, [p.to_dict() for p in profiles])
        print(f"合計 保存: {written}件")
        print(f"保存先: {out_path}")
    if blocked is not None:
        print(f"エラー: {blocked}", file=sys.stderr)
        print("検知時点までの取得分は保存済みです。残りのアカウントは処理していません。", file=sys.stderr)
        if isinstance(blocked, SessionExpiredError):
            return EXIT_SESSION_EXPIRED
        return EXIT_RATE_LIMITED if isinstance(blocked, RateLimitedError) else EXIT_CHALLENGE
    if missing:
        print(f"エラー: 取得できなかったアカウント: {', '.join(missing)}", file=sys.stderr)
        return EXIT_ERROR
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_arg_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
