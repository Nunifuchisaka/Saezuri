"""saezuri Xアカウントのプロフィール情報(フォロワー数等)取得

config.yamlのwatch_accountsに列挙したハンドルそれぞれについて x.com/<handle> を
1回だけ開き、UserByScreenNameオペレーションのGraphQLレスポンスを傍受して
フォロワー数・フォロー数・投稿数・表示名・プロフィール文等を取得する。
スクロールはしない。差分管理(state)も行わず、実行ごとの時点スナップショットとして追記する。
結果は標準出力に表示し、data/raw/x_profiles_<日付>.jsonl に追記する。

使用例:
    python scripts/x_profiles.py                     # watch_accounts全件
    python scripts/x_profiles.py --handle some_user  # 特定の1アカウントのみ

終了コード: 0=正常 / 1=設定・環境エラー(取得できないアカウントがあった場合も1) /
2=セッション失効 / 3=レートリミット(429)検知 / 4=チャレンジ画面検知
(3・4の場合も検知時点までの取得分は保存し、残りのアカウントは処理しない。リトライはしない)

注意: 取得したJSONLはPost/TrendEntryではないため、digest.pyには渡さないこと。
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import sync_playwright

import storage
from capture import (
    ChallengeDetectedError,
    GraphQLCapture,
    RateLimitedError,
    _dig,
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
    new_context,
    require_login,
)

logger = logging.getLogger(__name__)

EXIT_ERROR = 1
EXIT_SESSION_EXPIRED = 2
EXIT_RATE_LIMITED = 3
EXIT_CHALLENGE = 4

PROFILE_OPERATION = "UserByScreenName"

# 1アカウントあたりの待ち受け: POLL_INTERVAL_MS間隔でMAX_POLL_ROUNDS回まで(約12秒)。
# 超えても取れなければ諦める(リトライ・再アクセスはしない)
POLL_INTERVAL_MS = 1500
MAX_POLL_ROUNDS = 8


@dataclass
class XProfile:
    """Xアカウント1件分のプロフィールスナップショット"""

    handle: str
    user_id: str
    name: str
    description: str
    followers_count: int
    collected_at: str
    following_count: Optional[int] = None
    tweet_count: Optional[int] = None
    media_count: Optional[int] = None
    location: str = ""
    url: Optional[str] = None
    created_at: Optional[str] = None
    is_blue_verified: Optional[bool] = None
    verified_type: Optional[str] = None
    protected: Optional[bool] = None
    category: list[str] = field(default_factory=list)
    profile_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _to_int(value: Any) -> Optional[int]:
    """数値(boolは除く)または数字のみの文字列をintにする。それ以外はNone。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _first_int(user: dict, *paths: tuple[str, ...]) -> Optional[int]:
    """複数の候補パスのうち最初に数値で見つかったものを返す。"""
    for path in paths:
        number = _to_int(_dig(user, *path))
        if number is not None:
            return number
    return None


def _first_str(user: dict, *paths: tuple[str, ...]) -> Optional[str]:
    for path in paths:
        value = _dig(user, *path)
        if isinstance(value, str) and value:
            return value
    return None


def _first_bool(user: dict, *paths: tuple[str, ...]) -> Optional[bool]:
    for path in paths:
        value = _dig(user, *path)
        if isinstance(value, bool):
            return value
    return None


def _screen_name(user: dict) -> Optional[str]:
    return _first_str(user, ("core", "screen_name"), ("legacy", "screen_name"), ("screen_name",))


def _find_user_nodes(payload: Any) -> list[dict]:
    """payloadから「rest_idとscreen_nameを持つdict」を再帰探索で集める。

    UserByScreenNameのルートキー(data.user.result等)は非公開仕様で変わりうるため、
    キー名を決め打ちせず、ユーザーらしいノードの形で探す。見つけたノードの内側は掘らない。
    """
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("rest_id") is not None and _screen_name(node):
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _profile_url(user: dict) -> Optional[str]:
    urls = _dig(user, "profile_bio", "entities", "url", "urls")
    if isinstance(urls, list) and urls and isinstance(urls[0], dict):
        expanded = urls[0].get("expanded_url")
        if isinstance(expanded, str) and expanded:
            return expanded
    return _first_str(user, ("website", "url"), ("legacy", "url"))


def _categories(user: dict) -> list[str]:
    categories = _dig(user, "professional", "category")
    if not isinstance(categories, list):
        return []
    return [c["name"] for c in categories if isinstance(c, dict) and isinstance(c.get("name"), str)]


def parse_profile_payload(
    payload: dict,
    operation: str = "",
    handle: Optional[str] = None,
    collected_at: Optional[str] = None,
) -> Optional[XProfile]:
    """UserByScreenName系ペイロード1件からXProfileを抽出する。

    handle指定時は大文字小文字を無視して一致するユーザーだけを採る。
    フォロワー数が見つからない場合・対象ユーザーが居ない場合は警告を出してNone。
    新形式(relationship_counts / tweet_counts / core)と旧形式(legacy配下)の両方を試す。
    """
    collected_at = collected_at or now_iso()
    users = _find_user_nodes(payload)
    if handle:
        wanted = handle.lstrip("@").lower()
        users = [u for u in users if (_screen_name(u) or "").lower() == wanted]
    if not users:
        logger.warning("%s: 対象ユーザーが見つかりません(handle=%s)", operation, handle)
        return None

    user = users[0]
    screen_name = _screen_name(user) or ""
    followers = _first_int(
        user,
        ("relationship_counts", "followers"),
        ("legacy", "followers_count"),
        ("legacy", "normal_followers_count"),
        ("followers_count",),
    )
    if followers is None:
        logger.warning("%s: フォロワー数が見つかりません(handle=%s, API構造変更の可能性)", operation, screen_name)
        return None

    return XProfile(
        handle=screen_name,
        user_id=str(user.get("rest_id")),
        name=_first_str(user, ("core", "name"), ("legacy", "name")) or "",
        description=_first_str(user, ("profile_bio", "description"), ("legacy", "description")) or "",
        followers_count=followers,
        collected_at=collected_at,
        following_count=_first_int(
            user, ("relationship_counts", "following"), ("legacy", "friends_count")
        ),
        tweet_count=_first_int(user, ("tweet_counts", "tweets"), ("legacy", "statuses_count")),
        media_count=_first_int(user, ("tweet_counts", "media_tweets"), ("legacy", "media_count")),
        location=_first_str(user, ("location", "location"), ("legacy", "location")) or "",
        url=_profile_url(user),
        created_at=_first_str(user, ("core", "created_at"), ("legacy", "created_at")),
        is_blue_verified=_first_bool(user, ("is_blue_verified",)),
        verified_type=_first_str(user, ("verification", "verified_type")),
        protected=_first_bool(user, ("privacy", "protected"), ("legacy", "protected")),
        category=_categories(user),
        profile_url=f"https://x.com/{screen_name}",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Xアカウントのプロフィール情報(フォロワー数等)取得(UserByScreenName傍受)")
    parser.add_argument("--handle", default=None,
                        help="特定の1アカウントだけ実行(@は不要。省略時はwatch_accounts全件)")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス(既定: config.yaml)")
    parser.add_argument("--browser", choices=SUPPORTED_BROWSERS, default=None,
                        help="起動するブラウザエンジン(省略時はconfig.yamlのbrowser)")
    parser.add_argument("--headed", action="store_true", help="ブラウザを可視状態で起動する(デバッグ用)")
    return parser


def resolve_handles(args: argparse.Namespace, config: dict) -> list[str]:
    """対象ハンドルのリストを決める(--handle指定が優先、@は除去)。"""
    if args.handle:
        handles = [args.handle]
    else:
        handles = list(config.get("watch_accounts") or [])
    return [str(h).lstrip("@") for h in handles if str(h).strip()]


def collect_profile(context, handle: str) -> tuple[Optional[XProfile], Optional[Exception]]:
    """1ハンドル分を1回だけ開いて取得する。ブロック検知時は(None, 例外)を返す。"""
    collected_at = now_iso()
    page = context.new_page()
    capture = GraphQLCapture(operations=(PROFILE_OPERATION,))
    capture.attach(page)
    try:
        page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded")
        for _ in range(MAX_POLL_ROUNDS):
            page.wait_for_timeout(POLL_INTERVAL_MS)
            capture.raise_if_blocked(page)
            for operation, payload in capture.drain():
                profile = parse_profile_payload(
                    payload, operation, handle=handle, collected_at=collected_at
                )
                if profile is not None:
                    return profile, None
        return None, None
    except (RateLimitedError, ChallengeDetectedError) as exc:
        return None, exc
    finally:
        try:
            capture.detach(page)
        except Exception as exc:
            logger.warning("GraphQLCaptureのdetachに失敗しました: %s", exc)
        page.close()


def format_profile(profile: XProfile) -> str:
    def show(value: Optional[int]) -> str:
        return f"{value:,}" if value is not None else "不明"

    return (f"@{profile.handle} ({profile.name}): フォロワー {show(profile.followers_count)}"
            f" / フォロー {show(profile.following_count)} / 投稿 {show(profile.tweet_count)}")


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    raw_dir = Path(config["output"]["raw_dir"])
    wait_range = get_wait_seconds_range(config)
    browser_name = args.browser or get_browser_name(config)

    handles = resolve_handles(args, config)
    if not handles:
        print("エラー: 対象アカウントがありません。config.yamlのwatch_accountsを設定するか--handleを指定してください。",
              file=sys.stderr)
        return EXIT_ERROR

    out_path = raw_dir / f"x_profiles_{datetime.now().strftime('%Y%m%d')}.jsonl"
    profiles: list[XProfile] = []
    missing: list[str] = []
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
                for index, handle in enumerate(handles):
                    if index:
                        time.sleep(random.uniform(*wait_range))
                    profile, blocked = collect_profile(context, handle)
                    if blocked is not None:
                        break
                    if profile is None:
                        missing.append(handle)
                        print(f"@{handle}: プロフィール情報を取得できませんでした")
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
