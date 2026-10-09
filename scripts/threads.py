"""saezuri Threads収集 共通基盤

th_login.py / th_user_posts.py から import して使う。Threads(www.threads.com)は
Instagramと同じ基盤で動いており、投稿ノードもInstagramのv1形式とほぼ同じ構造のため、
instagram.pyのレスポンス捕捉クラス・メディア抽出ロジックを再利用する。

提供するもの:
- Threads用storage_stateのパス解決・BrowserContext生成・ログイン状態検証
  (get_threads_storage_state_path / new_threads_context / require_threads_login)
- 内部APIのエンドポイント判定 (extract_threads_endpoint_name)
- レスポンス捕捉クラス (ThreadsCapture)。プロフィールの初回表示分はAPIではなく
  HTML内の<script type="application/json">に埋め込まれて届くため、inject()で
  そのJSONも同じ経路(drain())に流せるようにしている
- 捕捉JSONをschema.Postへ正規化するパーサー (parse_threads_payload)

Threadsの内部API仕様は非公開かつ予告なく変わるため、パーサーはベストエフォート。
ルートキーを決め打ちせず「Threads投稿らしいdict」を再帰探索で拾い、想定キーが
欠けている場合は例外を投げずlogger.warning()を出してその1件だけスキップする。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from typing import Any, Iterable, Optional

from playwright.sync_api import BrowserContext, Page

from capture import now_iso
from instagram import (
    InstagramCapture,
    _dig,
    _parse_v1_media_list,
    _timestamp_to_iso,
    _to_int,
    load_embedded_json,
    pk_to_shortcode,
)
from schema import Post
from session import SessionExpiredError

logger = logging.getLogger(__name__)

THREADS_HOME_URL = "https://www.threads.com/"
THREADS_LOGIN_URL = "https://www.threads.com/login"

DEFAULT_STORAGE_STATE_PATH = "session/threads_storage_state.json"

# ログイン済みブラウザにのみ付与されるCookie名(Instagramと同名だがドメインが別)
SESSION_COOKIE_NAME = "sessionid"
# 旧ドメイン(threads.net)からの移行期も考慮し両方を受け付ける
THREADS_COOKIE_DOMAINS = ("threads.com", "threads.net")

LOGIN_URL_KEYWORDS = ("/login",)
CHALLENGE_URL_KEYWORDS = ("/challenge", "/checkpoint", "/accounts/suspended", "/accounts/disabled")

RELOGIN_MESSAGE = (
    "Threadsのセッションが失効しています。th_login.pyを再実行して手動ログインし直してください。"
)

# 捕捉対象の内部APIエンドポイント。スクロール時の追加読み込みはgraphql/query等で届く。
# 他用途のクエリも同じURLを使うため広めに拾い、パーサー側で投稿だけを抽出する
ENDPOINT_PATTERNS = (
    (re.compile(r"https://(?:www\.)?threads\.(?:com|net)/graphql/query"), "graphql_query"),
    (re.compile(r"https://(?:www\.)?threads\.(?:com|net)/api/graphql"), "api_graphql"),
)

# ページ埋め込みJSONのうち、投稿データを含みうるものだけを取り出すための目印
# (巨大なページで無関係なJSONまでPython側に渡さないよう、この文字列を含むものに絞る)
EMBEDDED_JSON_MARKER = "text_post_app_info"

# 投稿URL(threads.com/@<username>/post/<code>)からユーザー名・codeを取り出す
POST_URL_RE = re.compile(r"threads\.(?:com|net)/@([^/?#]+)/post/([A-Za-z0-9_-]+)")


# ---------------------------------------------------------------------------
# セッション
# ---------------------------------------------------------------------------

def get_threads_config(config: dict) -> dict:
    """config.yamlのthreadsセクションを返す。欠如時は空辞書。"""
    section = config.get("threads")
    return section if isinstance(section, dict) else {}


def get_threads_storage_state_path(config: dict) -> Path:
    """Threads用storage_stateファイルのパスを返す(X用・Instagram用とは別ファイル)。"""
    path = get_threads_config(config).get("storage_state_path") or DEFAULT_STORAGE_STATE_PATH
    return Path(path)


def get_threads_watch_accounts(config: dict) -> list[str]:
    """config.yamlのthreads.watch_accountsを@除去済みのリストで返す。"""
    accounts = get_threads_config(config).get("watch_accounts") or []
    return [str(a).strip().lstrip("@") for a in accounts if str(a).strip()]


def new_threads_context(browser, config: dict, **kwargs) -> BrowserContext:
    """Threads用storage_stateをロードしてBrowserContextを生成する。

    th_login.py未実行でファイルが存在しない場合はFileNotFoundErrorを送出する。
    """
    storage_state_path = get_threads_storage_state_path(config)
    if not storage_state_path.exists():
        raise FileNotFoundError(
            f"Threadsのstorage_stateファイルが見つかりません: {storage_state_path}\n"
            "先にth_login.pyを実行して手動ログインしてください。"
        )
    return browser.new_context(storage_state=str(storage_state_path), **kwargs)


def has_threads_session_cookie(cookies: Iterable[dict]) -> bool:
    """Cookie一覧にThreadsのログインセッションCookie(sessionid)が含まれるか。"""
    for cookie in cookies:
        domain = cookie.get("domain") or ""
        if (
            cookie.get("name") == SESSION_COOKIE_NAME
            and cookie.get("value")
            and any(d in domain for d in THREADS_COOKIE_DOMAINS)
        ):
            return True
    return False


def is_threads_login_url(url: str) -> bool:
    return any(keyword in url for keyword in LOGIN_URL_KEYWORDS)


def is_threads_challenge_url(url: str) -> bool:
    return any(keyword in url for keyword in CHALLENGE_URL_KEYWORDS)


def verify_threads_login(context: BrowserContext) -> bool:
    """threads.comを開き、ログイン画面へ飛ばされずsessionid Cookieが残っているかで判定する。"""
    page = context.new_page()
    try:
        page.goto(THREADS_HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        current_url = page.url
    finally:
        page.close()
    if is_threads_login_url(current_url):
        return False
    return has_threads_session_cookie(context.cookies())


def require_threads_login(context: BrowserContext) -> None:
    """verify_threads_login()がFalseならSessionExpiredErrorを送出する。自動再ログインはしない。"""
    if not verify_threads_login(context):
        raise SessionExpiredError(RELOGIN_MESSAGE)


# ---------------------------------------------------------------------------
# レスポンス捕捉
# ---------------------------------------------------------------------------

def extract_threads_endpoint_name(url: str) -> Optional[str]:
    """捕捉対象の内部APIエンドポイントURLなら名前を、対象外ならNoneを返す。"""
    for pattern, name in ENDPOINT_PATTERNS:
        if pattern.match(url):
            return name
    return None


class ThreadsCapture(InstagramCapture):
    """ThreadsのAPIレスポンスを溜めるバッファ(InstagramCaptureの判定条件をThreads用に差し替えたもの)。

    429・チャレンジ要求・ログイン画面遷移の検知はInstagramCaptureと共通。
    加えて、ページ埋め込みJSONをinject()で差し込み、次のdrain()で一緒に返す。
    """

    service_name = "Threads"
    relogin_message = RELOGIN_MESSAGE

    def endpoint_name(self, url: str) -> Optional[str]:
        return extract_threads_endpoint_name(url)

    def is_login_url(self, url: str) -> bool:
        return is_threads_login_url(url)

    def is_challenge_url(self, url: str) -> bool:
        return is_threads_challenge_url(url)


def load_embedded_payloads(page: Page) -> list[dict]:
    """ページ内の<script type="application/json">から投稿データを含むJSONを取り出す。

    読み出し・パースに失敗したものは警告を出してスキップする(全体は落とさない)。
    """
    return load_embedded_json(page, EMBEDDED_JSON_MARKER)


# ---------------------------------------------------------------------------
# パーサー
# ---------------------------------------------------------------------------

def _is_threads_post(node: dict) -> bool:
    """Threads投稿ノードらしいか(Instagramのv1形式+text_post_app_info)。"""
    return (
        isinstance(node.get("code"), str)
        and "taken_at" in node
        and ("pk" in node or "id" in node)
        and isinstance(node.get("text_post_app_info"), dict)
    )


def iter_threads_posts(payload: Any) -> Iterable[dict]:
    """ペイロードからThreads投稿ノードを再帰探索で順に取り出す。

    投稿ノードを見つけたらその内側(引用元投稿・カルーセル子要素)には潜らない。
    スレッド(連投)はthread_items[]に投稿ノードが並ぶため、それぞれが1件として拾われる。
    """
    if isinstance(payload, dict):
        if _is_threads_post(payload):
            yield payload
            return
        for value in payload.values():
            yield from iter_threads_posts(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from iter_threads_posts(value)


def _post_pk(node: Any) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    pk = node.get("pk") or str(node.get("id") or "").split("_")[0]
    return str(pk) if pk else None


def _has_media(node: dict) -> bool:
    """画像・動画・カルーセルのいずれかを持つか(テキストのみ投稿でメディア警告を出さないため)。"""
    return bool(
        node.get("carousel_media")
        or _dig(node, "image_versions2", "candidates")
        or node.get("video_versions")
    )


def _parse_threads_node(
    node: dict,
    endpoint: str,
    source_query: Optional[str],
    collected_at: str,
    target_handle: Optional[str],
) -> Optional[Post]:
    code = node["code"]
    post_id = _post_pk(node)
    if not post_id:
        logger.warning("%s: pk/idが見つからないため1件スキップします(code=%s)", endpoint, code)
        return None
    handle = _dig(node, "user", "username")
    if not handle:
        logger.warning("%s: 投稿者(username)が見つからないため1件スキップします(code=%s)", endpoint, code)
        return None
    info = node["text_post_app_info"]
    share_info = info.get("share_info") if isinstance(info.get("share_info"), dict) else {}
    # 対象アカウントのプロフィールに他人の投稿が並ぶ=そのアカウントによるリポスト
    is_repost = bool(target_handle) and handle.lower() != target_handle.lower()
    return Post(
        id=post_id,
        url=f"https://www.threads.com/@{handle}/post/{code}",
        author_handle=handle,
        author_name=_dig(node, "user", "full_name") or "",
        created_at=_timestamp_to_iso(node.get("taken_at")),
        text=_dig(node, "caption", "text") or "",
        like_count=_to_int(node.get("like_count")),
        repost_count=_to_int(info.get("repost_count")),
        reply_count=_to_int(info.get("direct_reply_count")),
        quote_count=_to_int(info.get("quote_count")),
        media=_parse_v1_media_list(node) if _has_media(node) else [],
        is_repost=is_repost,
        quoted_id=_post_pk(share_info.get("quoted_post")),
        source_query=source_query,
        collected_at=collected_at,
    )


def parse_threads_payload(
    payload: dict,
    endpoint: str = "",
    source_query: Optional[str] = None,
    collected_at: Optional[str] = None,
    target_handle: Optional[str] = None,
) -> list[Post]:
    """ThreadsのAPIペイロード(またはページ埋め込みJSON)1件からPostのリストを抽出する。

    - target_handle: 収集対象アカウント。投稿者がこれと異なる投稿はis_repost=Trueにする
    - like_countはいいね数、reply_countは直接の返信数、repost_count/quote_countは
      Threadsのリポスト数・引用数。quoted_idには引用元投稿のIDを入れる
    """
    collected_at = collected_at or now_iso()
    posts: list[Post] = []
    for node in iter_threads_posts(payload):
        post = _parse_threads_node(node, endpoint, source_query, collected_at, target_handle)
        if post is not None:
            posts.append(post)
    return posts


# ---------------------------------------------------------------------------
# 起点投稿へのリプライ・引用(th_related.py用)
# ---------------------------------------------------------------------------

def parse_threads_post_url(url: str) -> tuple[str, str]:
    """Threadsの投稿URLから(ユーザー名, code)を取り出す。解釈できないURLはValueError。"""
    match = POST_URL_RE.search(url)
    if match is None:
        raise ValueError(f"Threadsの投稿URLとして解釈できません: {url!r}")
    return match.group(1), match.group(2)


def build_threads_post_url(username: str, code: str) -> str:
    return f"https://www.threads.com/@{username}/post/{code}"


def _iter_posts_with_replies(payload: Any) -> Iterable[dict]:
    """iter_threads_postsと同じだが、見つけた投稿ノードのdirect_replies配下にだけは潜る。

    起点ノード自体が返信一覧(text_post_app_info.direct_replies)を抱えた形で届いても
    返信を取りこぼさないため。引用元投稿・カルーセル子要素には潜らない。
    """
    if isinstance(payload, dict):
        if _is_threads_post(payload):
            yield payload
            replies = payload["text_post_app_info"].get("direct_replies")
            if replies is not None:
                yield from _iter_posts_with_replies(replies)
            return
        for value in payload.values():
            yield from _iter_posts_with_replies(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _iter_posts_with_replies(value)


def parse_threads_replies(
    payload: dict,
    endpoint: str = "",
    origin_pk: str = "",
    origin_handle: str = "",
    source_query: Optional[str] = None,
    collected_at: Optional[str] = None,
) -> list[Post]:
    """投稿ページの返信(埋め込みJSON・BarcelonaPostPageDirectRepliesRefetchQuery)をPostへ正規化する。

    is_replyが真の投稿ノードだけを返す(起点自身・おすすめ等の非返信は除く)。
    Threadsの返信ノードには返信先の投稿IDが無いため、reply_level == 1 かつ
    reply_to_author が起点投稿者のものだけ in_reply_to_id に起点pkを補う(ペイロードに無い値を補っている)。
    それ以外(孫返信等)は親が特定できないため in_reply_to_id = None。
    """
    collected_at = collected_at or now_iso()
    origin_norm = origin_handle.lstrip("@").lower()
    posts: list[Post] = []
    for node in _iter_posts_with_replies(payload):
        if _post_pk(node) == origin_pk:
            continue
        info = node["text_post_app_info"]
        if not info.get("is_reply"):
            continue
        post = _parse_threads_node(node, endpoint, source_query, collected_at, None)
        if post is None:
            continue
        reply_to = (_dig(info, "reply_to_author", "username") or "").lower()
        if info.get("reply_level") == 1 and origin_norm and reply_to == origin_norm:
            post.in_reply_to_id = origin_pk
        posts.append(post)
    return posts


def find_threads_post(
    payloads: list[dict],
    code: Optional[str] = None,
    pk: Optional[str] = None,
    endpoint: str = "embedded",
    source_query: Optional[str] = None,
    collected_at: Optional[str] = None,
) -> Optional[Post]:
    """payloads中からcode(またはpk)が一致する投稿ノードを探してPostにする。無ければNone。"""
    collected_at = collected_at or now_iso()
    for payload in payloads:
        for node in iter_threads_posts(payload):
            if (code is not None and node.get("code") == code) or (pk is not None and _post_pk(node) == pk):
                return _parse_threads_node(node, endpoint, source_query, collected_at, None)
    return None


@dataclass
class ThreadsQuoteRef:
    """「アクティビティを見る」の引用一覧の1件(いいね数は無いので投稿を開いて取り直す)。"""

    pk: str
    username: str
    shortcode: str

    @property
    def url(self) -> str:
        return build_threads_post_url(self.username, self.shortcode)


def _is_quote_activity(node: dict) -> bool:
    return isinstance(node.get("destination"), str) and _dig(node, "extra", "icon_name") == "quote"


def _iter_quote_activities(payload: Any) -> Iterable[dict]:
    if isinstance(payload, dict):
        if _is_quote_activity(payload):
            yield payload
            return
        for value in payload.values():
            yield from _iter_quote_activities(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _iter_quote_activities(value)


def parse_feedback_quote_items(payload: dict, endpoint: str = "") -> list[ThreadsQuoteRef]:
    """引用一覧(BarcelonaFeedbackHubTabQuery / DialogQueryのpost_activity)から引用投稿の参照を取り出す。

    destination(media?id=<pk>_<ユーザーID>&shortcode=<code>)からpkとshortcodeを読む。
    shortcodeが無ければpkから変換する。投稿者(actor.username)が無い項目は警告してスキップする。
    """
    refs: list[ThreadsQuoteRef] = []
    for node in _iter_quote_activities(payload):
        query = parse_qs(urlsplit(node["destination"]).query)
        pk = (query.get("id") or [""])[0].split("_")[0] or str(node.get("pk") or "")
        username = _dig(node, "actor", "username")
        if not pk or not username:
            logger.warning("%s: 引用一覧の項目にpk/投稿者が無いため1件スキップします", endpoint)
            continue
        shortcode = (query.get("shortcode") or [""])[0]
        if not shortcode:
            try:
                shortcode = pk_to_shortcode(pk)
            except ValueError:
                logger.warning("%s: 引用投稿のpkが数値でないため1件スキップします(pk=%s)", endpoint, pk)
                continue
        refs.append(ThreadsQuoteRef(pk=pk, username=username, shortcode=shortcode))
    return refs
