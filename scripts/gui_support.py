"""GUI（gui.py）用の補助関数。pywebview・Playwright非依存の純粋関数だけを置く（単体テスト対象）。

- 各収集スクリプトの実行コマンド（引数リスト）の組み立て
- 終了コードを非エンジニア向けの日本語メッセージに変換
- config.yamlのコメントを残したまま、監視アカウント・キーワード・CDP接続先を書き換える
- ログイン用Chromeの実行ファイル探索と起動コマンドの組み立て
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
CDP_PORT = 9222
CHROME_PROFILE_DIRNAME = "chrome-profile"

SNS_X = "x"
SNS_INSTAGRAM = "instagram"
SNS_THREADS = "threads"
SNS_TIKTOK = "tiktok"
SNS_LABELS = {
    SNS_X: "X",
    SNS_INSTAGRAM: "Instagram",
    SNS_THREADS: "Threads",
    SNS_TIKTOK: "TikTok",
}

KIND_POSTS = "posts"
KIND_PROFILES = "profiles"

# (SNS, 種類) → (スクリプト名, アカウント指定の引数名, --maxを受け付けるか)
ACCOUNT_SCRIPTS = {
    (SNS_X, KIND_POSTS): ("user_tweets.py", "--handle", True),
    (SNS_X, KIND_PROFILES): ("x_profiles.py", "--handle", False),
    (SNS_INSTAGRAM, KIND_POSTS): ("ig_user_posts.py", "--user", True),
    (SNS_THREADS, KIND_POSTS): ("th_user_posts.py", "--user", True),
    (SNS_THREADS, KIND_PROFILES): ("th_profiles.py", "--user", False),
    (SNS_TIKTOK, KIND_POSTS): ("tt_user_posts.py", "--user", True),
    (SNS_TIKTOK, KIND_PROFILES): ("tt_profiles.py", "--user", False),
}

LOGIN_SCRIPTS = {
    SNS_X: "login.py",
    SNS_INSTAGRAM: "ig_login.py",
    SNS_THREADS: "th_login.py",
    SNS_TIKTOK: "tt_login.py",
}

RELATED_SCRIPTS = {
    SNS_X: "related.py",
    SNS_INSTAGRAM: "ig_related.py",
    SNS_THREADS: "th_related.py",
    SNS_TIKTOK: "tt_related.py",
}

# config.yaml上の監視アカウントの場所（Noneはトップレベル）
WATCH_SECTIONS = {
    SNS_X: None,
    SNS_INSTAGRAM: "instagram",
    SNS_THREADS: "threads",
    SNS_TIKTOK: "tiktok",
}

EXIT_MESSAGES = {
    0: "完了しました。",
    1: "設定・入力・環境のエラーで止まりました。ログの内容（入力したURLやアカウント名、ログイン用Chromeが起動しているか等）を確認してください。",
    2: "ログインが切れています。「準備」タブからログインし直してから、もう一度実行してください。",
    3: "アクセスが多すぎるとSNS側に判断されたため（レートリミット）、途中で止めました。ここまでの分は保存済みです。数時間以上あけてから再実行してください。",
    4: "SNS側の確認画面（CAPTCHA等）が出たため、途中で止めました。ここまでの分は保存済みです。ブラウザで普通にSNSを開いて確認画面を解消し、時間をあけてから再実行してください。",
}


class InputError(ValueError):
    """GUIの入力値が不正なときの例外。メッセージはそのまま画面に出す。"""


def exit_message(code: Optional[int]) -> str:
    if code is None:
        return "中止しました。"
    return EXIT_MESSAGES.get(code, f"想定外の終了コード({code})で終わりました。ログを確認してください。")


def script_command(python: str, scripts_dir: Path | str, script: str, args: Iterable[str] = ()) -> list[str]:
    """`python -u scripts/<script> <args...>` の引数リストを返す。"""
    return [python, "-u", str(Path(scripts_dir) / script), *args]


def _parse_positive_int(value: str, label: str) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    if not value.isdigit() or int(value) <= 0:
        raise InputError(f"{label}は1以上の数字で入力してください。")
    return str(int(value))


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value: str, label: str) -> Optional[str]:
    value = (value or "").strip().replace("/", "-")
    if not value:
        return None
    if not _DATE_RE.match(value):
        raise InputError(f"{label}は 2026-11-01 のような形で入力してください。")
    return value


def normalize_account(value: str) -> str:
    """'@foo' や 'https://x.com/foo' のような入力からアカウント名だけを取り出す。"""
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        path = urlparse(value).path.strip("/")
        value = path.split("/")[0] if path else ""
    return value.lstrip("@").strip()


def build_search_args(query: str, from_handle: str = "", since: str = "", until: str = "",
                      mode: str = "live", max_items: str = "") -> list[str]:
    """search.py の引数を組み立てる。"""
    query = (query or "").strip()
    handle = normalize_account(from_handle)
    if not query and not handle:
        raise InputError("キーワードか投稿者のどちらかを入力してください。")
    args: list[str] = []
    if query:
        args += ["--query", query]
    if handle:
        args += ["--from", handle]
    since_v = _parse_date(since, "開始日")
    until_v = _parse_date(until, "終了日")
    if since_v and until_v and since_v > until_v:
        raise InputError("開始日が終了日より後になっています。")
    if since_v:
        args += ["--since", since_v]
    if until_v:
        args += ["--until", until_v]
    if mode not in ("live", "top"):
        raise InputError("並び順の指定が不正です。")
    args += ["--mode", mode]
    max_v = _parse_positive_int(max_items, "最大件数")
    if max_v:
        args += ["--max", max_v]
    return args


def build_account_command(sns: str, kind: str, account: str = "", max_items: str = "") -> tuple[str, list[str]]:
    """アカウント追跡（投稿・プロフィール）のスクリプト名と引数を返す。アカウント空欄なら設定の全件。"""
    key = (sns, kind)
    if key not in ACCOUNT_SCRIPTS:
        label = SNS_LABELS.get(sns, sns)
        raise InputError(f"{label}のフォロワー数取得には対応していません。")
    script, account_flag, accepts_max = ACCOUNT_SCRIPTS[key]
    args: list[str] = []
    name = normalize_account(account)
    if name:
        args += [account_flag, name]
    if accepts_max:
        max_v = _parse_positive_int(max_items, "最大件数")
        if max_v:
            args += ["--max", max_v]
    return script, args


def detect_sns_from_url(url: str) -> Optional[str]:
    host = (urlparse((url or "").strip()).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in ("x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"):
        return SNS_X
    if host == "instagram.com":
        return SNS_INSTAGRAM
    if host in ("threads.com", "threads.net"):
        return SNS_THREADS
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return SNS_TIKTOK
    return None


def build_related_command(url: str, top: str = "", max_items: str = "") -> tuple[str, list[str]]:
    """投稿URLからSNSを判別し、反応ランキング用スクリプト名と引数を返す。"""
    url = (url or "").strip()
    if not url:
        raise InputError("投稿のURLを入力してください。")
    sns = detect_sns_from_url(url)
    if sns is None:
        raise InputError("X・Instagram・Threads・TikTokの投稿URLを入力してください。")
    if sns not in RELATED_SCRIPTS:
        raise InputError("この投稿の反応ランキングには対応していません。")
    args = ["--url", url]
    top_v = _parse_positive_int(top, "上位件数")
    if top_v:
        args += ["--top", top_v]
    max_v = _parse_positive_int(max_items, "最大件数")
    if max_v:
        args += ["--max", max_v]
    return RELATED_SCRIPTS[sns], args


def build_digest_args(files: Iterable[str], title: str = "") -> list[str]:
    files = [f for f in files if f]
    if not files:
        raise InputError("まとめるファイルを選んでください。")
    args = list(files)
    title = (title or "").strip()
    if title:
        args += ["--title", title]
    return args


def build_media_args(files: Iterable[str]) -> list[str]:
    files = [f for f in files if f]
    if not files:
        raise InputError("画像を保存するファイルを選んでください。")
    return list(files)


def parse_lines(text: str) -> list[str]:
    """1行1件の入力をアカウント名・語のリストにする（空行・重複は除く、順序は保つ）。"""
    seen: set[str] = set()
    out: list[str] = []
    for line in (text or "").splitlines():
        for part in line.split(","):
            v = part.strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return out


def parse_account_lines(text: str) -> list[str]:
    return parse_lines("\n".join(normalize_account(v) for v in parse_lines(text)))


# ---- config.yaml のコメントを残したままの書き換え ----

def _flow_list(values: Iterable[str]) -> str:
    def quote(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return "[" + ", ".join(quote(v) for v in values) + "]"


def _find_section_range(lines: list[str], section: Optional[str]) -> tuple[int, int, str]:
    """セクション内の行範囲[start, end)とキーのインデントを返す。Noneはトップレベル全体。"""
    if section is None:
        return 0, len(lines), ""
    header = re.compile(rf"^{re.escape(section)}:\s*(#.*)?$")
    for i, line in enumerate(lines):
        if header.match(line.rstrip("\r\n")):
            end = len(lines)
            indent = None
            for j in range(i + 1, len(lines)):
                raw = lines[j].rstrip("\r\n")
                if not raw.strip() or raw.lstrip().startswith("#"):
                    continue
                if not raw.startswith((" ", "\t")):
                    end = j
                    break
                if indent is None:
                    indent = raw[: len(raw) - len(raw.lstrip())]
            return i + 1, end, indent or "  "
    raise KeyError(section)


def set_config_value(text: str, section: Optional[str], key: str, value_yaml: str) -> str:
    """config.yamlのテキスト中で、`section.key`（sectionがNoneならトップレベルのkey）の値を差し替える。

    値がブロック形式のリスト（次行以降の`- foo`）でもまとめて1行に置き換える。行末コメントは残す。
    キーが無ければセクションの末尾（トップレベルならファイル末尾）に追記する。
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    try:
        start, end, indent = _find_section_range(lines, section)
    except KeyError:
        suffix = "" if not text or text.endswith(("\n", "\r\n")) else newline
        return f"{text}{suffix}{section}:{newline}  {key}: {value_yaml}{newline}"

    key_re = re.compile(rf"^({re.escape(indent)}){re.escape(key)}:(\s*)(.*?)(\s+#.*)?$")
    for i in range(start, end):
        raw = lines[i].rstrip("\r\n")
        m = key_re.match(raw)
        if not m:
            continue
        comment = m.group(4) or ""
        eol = lines[i][len(raw):] or newline
        lines[i] = f"{indent}{key}:{' ' + value_yaml if value_yaml else ''}{comment}{eol}"
        # 次行以降のブロック形式リスト要素を取り除く
        j = i + 1
        while j < end:
            nxt = lines[j].rstrip("\r\n")
            stripped = nxt.lstrip()
            child_indent = len(nxt) - len(stripped)
            if stripped.startswith("- ") or stripped == "-":
                if child_indent >= len(indent):
                    del lines[j]
                    end -= 1
                    continue
            break
        return "".join(lines)

    insert_at = end
    while insert_at > start and not lines[insert_at - 1].strip():
        insert_at -= 1
    if insert_at > 0 and not lines[insert_at - 1].endswith(("\n", "\r\n")):
        lines[insert_at - 1] += newline
    lines.insert(insert_at, f"{indent}{key}:{' ' + value_yaml if value_yaml else ''}{newline}")
    return "".join(lines)


def set_watch_accounts(text: str, sns: str, accounts: Iterable[str]) -> str:
    return set_config_value(text, WATCH_SECTIONS[sns], "watch_accounts", _flow_list(accounts))


def set_interest_keywords(text: str, keywords: Iterable[str]) -> str:
    return set_config_value(text, None, "interest_keywords", _flow_list(keywords))


def set_cdp_urls(text: str, enabled: bool, url: str = DEFAULT_CDP_URL) -> str:
    """X（session.cdp_url）とTikTok（tiktok.cdp_url）のCDP接続先をまとめて設定・解除する。"""
    value = url if enabled else ""
    text = set_config_value(text, "session", "cdp_url", value)
    return set_config_value(text, "tiktok", "cdp_url", value)


def get_watch_accounts(config: dict, sns: str) -> list[str]:
    section = WATCH_SECTIONS[sns]
    node = config if section is None else (config.get(section) or {})
    return [str(v) for v in (node.get("watch_accounts") or [])]


def cdp_enabled(config: dict) -> bool:
    value = (config.get("session") or {}).get("cdp_url")
    return isinstance(value, str) and bool(value.strip())


# ---- ログイン用Chrome ----

def chrome_candidates(platform: str = sys.platform, env: Optional[dict] = None) -> list[str]:
    env = os.environ if env is None else env
    if platform.startswith("win"):
        roots = [env.get("PROGRAMFILES", r"C:\Program Files"),
                 env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 env.get("LOCALAPPDATA", "")]
        return [str(Path(r) / "Google" / "Chrome" / "Application" / "chrome.exe") for r in roots if r]
    if platform == "darwin":
        return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    return ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"]


def find_chrome(platform: str = sys.platform, env: Optional[dict] = None, exists=os.path.exists) -> Optional[str]:
    for path in chrome_candidates(platform, env):
        if exists(path):
            return path
    return None


def chrome_command(chrome_path: str, profile_dir: Path | str, port: int = CDP_PORT,
                   start_urls: Iterable[str] = ("https://x.com/login",)) -> list[str]:
    """デバッグポート付き・専用プロファイルでChromeを起動するコマンド。最近のChromeは専用プロファイル必須。"""
    return [chrome_path, f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}",
            "--no-first-run", "--no-default-browser-check", *start_urls]


# ---- 実行ログからの出力ファイル抽出 ----

OUTPUT_PREFIXES = ("保存先:", "上位Markdown:", "出力:")


def extract_output_path(line: str) -> Optional[str]:
    """スクリプトの標準出力1行から出力先パスを取り出す（`保存先: data/raw/x.jsonl` 等）。"""
    text = (line or "").strip()
    for prefix in OUTPUT_PREFIXES:
        if text.startswith(prefix):
            path = text[len(prefix):].strip()
            return path or None
    return None


# ---- JSONL → Excel用CSV ----

# 投稿（Post）の列の日本語見出し。これ以外のキーはキー名のまま後ろに並べる。
CSV_HEADERS = {
    "created_at_jst": "投稿日時(日本時間)",
    "author_handle": "アカウント",
    "author_name": "表示名",
    "text": "本文",
    "like_count": "いいね",
    "repost_count": "リポスト・シェア",
    "reply_count": "返信・コメント",
    "quote_count": "引用",
    "url": "URL",
    "media": "画像・動画URL",
    "source_query": "収集条件",
}
CSV_HIDDEN_KEYS = {"created_at"}


def _to_jst_text(value) -> str:
    from datetime import timedelta, timezone

    from pick import parse_created_at

    parsed = parse_created_at(value) if value else None
    if not parsed:
        return ""
    return parsed.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")


def _cell(key: str, value) -> str:
    import json

    if value is None:
        return ""
    if key == "media" and isinstance(value, list):
        return "\n".join(str(m.get("url", "")) if isinstance(m, dict) else str(m) for m in value)
    if isinstance(value, bool):
        return "はい" if value else ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def records_to_rows(records: Iterable[dict]) -> tuple[list[str], list[list[str]]]:
    """JSONLのレコード群を（見出し, 行）にする。投稿はよく見る列を先頭に、それ以外は出現順。"""
    records = [r for r in records if isinstance(r, dict)]
    keys: list[str] = []
    if any("created_at" in r for r in records):
        keys.append("created_at_jst")
    for preferred in CSV_HEADERS:
        if preferred != "created_at_jst" and any(preferred in r for r in records):
            keys.append(preferred)
    for r in records:
        for k in r:
            if k not in keys and k not in CSV_HIDDEN_KEYS:
                keys.append(k)
    rows = []
    for r in records:
        row = []
        for k in keys:
            if k == "created_at_jst":
                row.append(_to_jst_text(r.get("created_at")))
            else:
                row.append(_cell(k, r.get(k)))
        rows.append(row)
    return [CSV_HEADERS.get(k, k) for k in keys], rows


def read_jsonl_records(paths: Iterable[str | Path]) -> list[dict]:
    import json

    out: list[dict] = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    return out


def write_csv(paths: Iterable[str | Path], out_path: str | Path) -> int:
    """JSONLをExcelでそのまま開けるCSV（UTF-8 BOM付き）にする。書いた行数を返す。"""
    import csv

    paths = list(paths)
    if not paths:
        raise InputError("CSVにするファイルを選んでください。")
    headers, rows = records_to_rows(read_jsonl_records(paths))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    return len(rows)


def default_csv_path(jsonl_paths: list[str | Path]) -> Path:
    first = Path(jsonl_paths[0])
    name = first.stem if len(jsonl_paths) == 1 else f"{first.stem}_ほか{len(jsonl_paths) - 1}件"
    return first.with_name(f"{name}.csv")


# ---- ログイン状況（画面左下の表示用） ----

DEFAULT_STORAGE_PATHS = {
    SNS_X: "session/x_storage_state.json",
    SNS_INSTAGRAM: "session/instagram_storage_state.json",
    SNS_THREADS: "session/threads_storage_state.json",
    SNS_TIKTOK: "session/tiktok_storage_state.json",
}
STORAGE_SECTIONS = {SNS_X: "session", SNS_INSTAGRAM: "instagram", SNS_THREADS: "threads", SNS_TIKTOK: "tiktok"}


def _section_cdp(config: dict, section: str) -> bool:
    value = (config.get(section) or {}).get("cdp_url")
    return isinstance(value, str) and bool(value.strip())


def login_status(config: dict, root: Path | str, chrome_alive: bool) -> list[dict]:
    """SNSごとのログイン状況。state は ok（使える）/ warn（要対応）/ off（未ログイン）。

    ログイン用Chrome方式（CDP）のSNSは、Chromeが起動しているかだけを見る（中のログイン状態は確かめない）。
    それ以外はstorage_stateファイルの有無で判断する（期限切れは実行時に分かる）。
    """
    out = []
    for sns in (SNS_X, SNS_INSTAGRAM, SNS_THREADS, SNS_TIKTOK):
        section = STORAGE_SECTIONS[sns]
        if sns in (SNS_X, SNS_TIKTOK) and _section_cdp(config, section):
            state, text = ("ok", "ログイン用Chrome") if chrome_alive else ("warn", "Chrome未起動")
        else:
            path = (config.get(section) or {}).get("storage_state_path") or DEFAULT_STORAGE_PATHS[sns]
            exists = (Path(root) / path).exists()
            state, text = ("ok", "ログイン済み") if exists else ("off", "未ログイン")
        out.append({"sns": sns, "label": SNS_LABELS[sns], "state": state, "text": text})
    return out


# ---- ログイン不要の投稿取得（FxTwitter） ----

FX_MAX_URLS = 50


def build_fx_args(text: str) -> list[str]:
    """1行1件の投稿URL（またはID）から fx_tweet.py の引数を作る。"""
    from fx_tweet import parse_status_id

    lines = parse_lines(text)
    if not lines:
        raise InputError("投稿のURLを1行に1つずつ入力してください。")
    if len(lines) > FX_MAX_URLS:
        raise InputError(f"一度に取得できるのは{FX_MAX_URLS}件までです。")
    args: list[str] = []
    for line in lines:
        status_id = parse_status_id(line)
        if status_id is None:
            raise InputError(f"Xの投稿URLとして読めません: {line[:80]}")
        args += ["--id", status_id]
    return args
