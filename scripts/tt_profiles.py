"""saezuri TikTokアカウントのプロフィール情報(フォロワー数等)取得

config.yamlのtiktok.watch_accountsに列挙したユーザー名それぞれについて
tiktok.com/@<username> を1回だけ開き、ページ埋め込みJSON(<script type="application/json">、
__UNIVERSAL_DATA_FOR_REHYDRATION__)または /api/user/detail/ のレスポンスの
userInfo(user+stats)からフォロワー数・フォロー数・いいね合計・動画数・表示名・自己紹介等を取得する。
スクロールはしない。差分管理(state)も行わず、実行ごとの時点スナップショットとして追記する。
結果は標準出力に表示し、data/raw/tt_profiles_<日付>.jsonl に追記する。

事前に tt_login.py で手動ログインしておくこと。

使用例:
    python scripts/tt_profiles.py                    # tiktok.watch_accounts全件
    python scripts/tt_profiles.py --user some_brand  # 特定の1アカウントのみ

終了コード: 0=正常 / 1=設定・環境エラー(取得できないアカウントがあった場合も1) /
2=セッション失効 / 3=レートリミット(429)検知 / 4=チャレンジ(CAPTCHA)検知
(2・3・4の場合も検知時点までの取得分は保存し、残りのアカウントは処理しない。リトライはしない)

注意: 取得したJSONLはPost/TrendEntryではないため、digest.pyには渡さないこと。
"""
from __future__ import annotations

import argparse
import json
import logging
import random
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
from tiktok import (
    TikTokCapture,
    get_tiktok_watch_accounts,
    new_tiktok_context,
    require_tiktok_login,
)

logger = logging.getLogger(__name__)

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4

# ページ表示後、埋め込みJSONを読む前に待つ時間(描画・ハイドレーション待ち)
EMBEDDED_WAIT_MS = 3000

SOURCE_EMBEDDED_JSON = "embedded_json"
SOURCE_API = "user_detail_api"

# プロフィール(followerCount)を含む埋め込みJSONだけを取り出すJS
PROFILE_SCRIPTS_JS = """
() => Array.from(document.querySelectorAll('script[type="application/json"]'))
  .map(el => el.textContent || '')
  .filter(text => text.includes('followerCount'))
"""


@dataclass
class TtProfile:
    """TikTokアカウント1件分のプロフィールスナップショット"""

    handle: str
    user_id: str
    name: str
    description: str
    followers_count: int
    following_count: int
    likes_count: int
    videos_count: int
    followers_source: str
    collected_at: str
    is_verified: Optional[bool] = None
    is_private: Optional[bool] = None
    url: Optional[str] = None
    profile_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _profile_url(handle: str) -> str:
    return f"https://www.tiktok.com/@{handle}"


def _find_user_infos(payload: Any) -> list[dict]:
    """payloadから「user(uniqueIdを持つdict)とstats/statsV2(dict)を持つdict」を再帰探索で集める。

    ルートキー(__DEFAULT_SCOPE__等)は非公開仕様で変わりうるため決め打ちしない。
    """
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            user = node.get("user")
            if (
                isinstance(user, dict)
                and isinstance(user.get("uniqueId"), str)
                and any(isinstance(node.get(key), dict) for key in ("stats", "statsV2"))
            ):
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _count(info: dict, key: str) -> Optional[int]:
    """stats(数値)を優先し、無ければstatsV2(文字列の数値)から整数を読む。読めなければNone。"""
    for container in ("stats", "statsV2"):
        value = (info.get(container) or {}).get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
    return None


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _bool_or_none(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def parse_profile_payload(
    payload: Any,
    operation: str = "",
    handle: Optional[str] = None,
    collected_at: Optional[str] = None,
) -> Optional[TtProfile]:
    """埋め込みJSON・user_detail APIのペイロード1件からTtProfileを抽出する。

    handle指定時は大文字小文字を無視して一致するユーザーだけを採る。
    対象ユーザーが居ない・followerCountが整数として読めない場合は(後者のみ警告を出して)None。
    フォロー数・いいね合計・動画数が読めない場合は0にする。
    """
    collected_at = collected_at or now_iso()
    infos = _find_user_infos(payload)
    if handle:
        wanted = handle.lstrip("@").lower()
        infos = [i for i in infos if i["user"]["uniqueId"].lower() == wanted]
    if not infos:
        return None

    info = infos[0]
    user = info["user"]
    followers = _count(info, "followerCount")
    if followers is None:
        logger.warning("%s: followerCountが数値ではありません(handle=%s, API構造変更の可能性)",
                       operation, user["uniqueId"])
        return None

    bio_link = user.get("bioLink")
    unique_id = user["uniqueId"]
    return TtProfile(
        handle=unique_id,
        user_id=str(user.get("id") or ""),
        name=_str_or_none(user.get("nickname")) or "",
        description=_str_or_none(user.get("signature")) or "",
        followers_count=followers,
        following_count=_count(info, "followingCount") or 0,
        likes_count=_count(info, "heartCount") or 0,
        videos_count=_count(info, "videoCount") or 0,
        followers_source=operation if operation else SOURCE_EMBEDDED_JSON,
        collected_at=collected_at,
        is_verified=_bool_or_none(user.get("verified")),
        is_private=_bool_or_none(user.get("privateAccount")),
        url=_str_or_none(bio_link.get("link")) if isinstance(bio_link, dict) else None,
        profile_url=_profile_url(unique_id),
    )


def pick_profile_scripts(texts: Iterable[Any]) -> list[str]:
    """埋め込みJSONの文字列群から、プロフィール(followerCount)を含むものだけを返す。"""
    return [t for t in texts if isinstance(t, str) and "followerCount" in t]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TikTokアカウントのプロフィール情報(フォロワー数等)取得")
    parser.add_argument("--user", default=None,
                        help="特定の1アカウントだけ実行(@は不要。省略時はtiktok.watch_accounts全件)")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def resolve_users(args: Any, config: dict) -> list[str]:
    """対象ユーザー名のリストを決める(--user指定が優先、@は除去)。"""
    if args.user:
        return [args.user.strip().lstrip("@")]
    return get_tiktok_watch_accounts(config)


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


def collect_profile(context, username: str) -> tuple[Optional[TtProfile], Optional[Exception]]:
    """1ユーザー分を1回だけ開いて取得する。ブロック・失効検知時は(None, 例外)を返す。"""
    collected_at = now_iso()
    page = context.new_page()
    capture = TikTokCapture()
    capture.attach(page)
    try:
        page.goto(_profile_url(username), wait_until="domcontentloaded")
        page.wait_for_timeout(EMBEDDED_WAIT_MS)
        capture.raise_if_blocked(page)
        for payload in _load_profile_payloads(page):
            profile = parse_profile_payload(
                payload, SOURCE_EMBEDDED_JSON, handle=username, collected_at=collected_at
            )
            if profile is not None:
                return profile, None
        for endpoint, payload in capture.drain():
            if endpoint != "user_detail":
                continue
            profile = parse_profile_payload(payload, SOURCE_API, handle=username, collected_at=collected_at)
            if profile is not None:
                return profile, None
        capture.raise_if_blocked(page)
        return None, None
    except (RateLimitedError, ChallengeDetectedError, SessionExpiredError) as exc:
        return None, exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("TikTokCaptureのdetachに失敗しました: %s", exc)
        page.close()


def format_profile(profile: TtProfile) -> str:
    return (f"@{profile.handle} ({profile.name}): フォロワー {profile.followers_count:,}"
            f" / いいね合計 {profile.likes_count:,} / 動画 {profile.videos_count:,}"
            f" [{profile.followers_source}]")


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)

    users = resolve_users(args, config)
    if not users:
        print("エラー: 対象アカウントがありません。config.yamlのtiktok.watch_accountsを設定するか"
              "--userを指定してください。", file=sys.stderr)
        return EXIT_ERROR

    out_path = raw_dir / f"tt_profiles_{datetime.now().strftime('%Y%m%d')}.jsonl"
    profiles: list[TtProfile] = []
    missing: list[str] = []
    blocked: Exception | None = None

    try:
        with sync_playwright() as playwright:
            browser = launch_browser(
                playwright, browser_name, headless=not args.headed,
                executable_path=get_browser_executable_path(config),
            )
            try:
                context = new_tiktok_context(browser, config)
                require_tiktok_login(context)
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
