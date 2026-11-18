"""GUI画面（scripts/webui/）とPythonをつなぐAPI。pywebviewのjs_apiとして画面に公開する。

pywebviewには依存しない（ウィンドウ操作が要る処理は`dialogs`経由で受け取る）ので、単体テストできる。
収集は既存スクリプトをsubprocessで1本ずつ実行するだけで、実行中は次の実行を受け付けない。
画面からの呼び出しは全て {"ok": bool, ...} の辞書を返す（例外は投げない）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import urllib.request
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Protocol

import yaml

import gui_support as g

ROOT = Path(__file__).resolve().parent.parent

SHORT_STATUS = {
    None: "中止しました",
    0: "完了しました",
    1: "エラーで止まりました",
    2: "ログインが切れています",
    3: "アクセス過多のため止めました",
    4: "確認画面が出たため止めました",
}

# SNS側に止められた（終了コード3・4）あと、同じ種類の収集を画面から再実行できるようになるまでの秒数。
# すぐ繰り返すとアカウントを凍結されやすいため、案内文だけに頼らず画面側でも待たせる。
# 画面を開き直すと消える簡易な歯止めで、コマンドラインからの実行は止めない。
COOLDOWN_SECONDS = 30 * 60
COOLDOWN_CODES = (3, 4)
# 同じ歯止めを共有する操作のまとまり（ブラウザでSNSを開く収集はまとめて待たせる）
COOLDOWN_GROUPS = {"search": "sns", "account": "sns", "related": "sns", "fx": "fx", "media": "media"}

BROWSER_CHOICES = [
    {"value": "chromium", "label": "標準（追加インストール不要・おすすめ）"},
    {"value": "brave", "label": "Brave（インストール済みの場合）"},
]

# 画面の「やること」→ (表示名, 引数を組み立てる関数)
TASKS: dict[str, tuple[str, Callable[[dict], tuple[str, list[str]]]]] = {
    "search": ("Xを検索", lambda p: ("search.py", g.build_search_args(
        p.get("query", ""), p.get("from", ""), p.get("since", ""), p.get("until", ""),
        p.get("mode", "live"), p.get("max", "")))),
    "account": ("アカウントを追う", lambda p: g.build_account_command(
        p.get("sns", g.SNS_X), p.get("kind", g.KIND_POSTS), p.get("account", ""), p.get("max", ""))),
    "related": ("反応ランキング", lambda p: g.build_related_command(
        p.get("url", ""), p.get("top", ""), p.get("max", ""))),
    "fx": ("投稿の取得（ログイン不要）", lambda p: ("fx_tweet.py", g.build_fx_args(p.get("urls", "")))),
    "digest": ("まとめ作成", lambda p: ("digest.py", g.build_digest_args(p.get("files") or [], p.get("title", "")))),
    "media": ("画像の保存", lambda p: ("media.py", g.build_media_args(p.get("files") or []))),
    "login": ("ログイン", lambda p: _login_command(p.get("sns", ""))),
}


def _login_command(sns: str) -> tuple[str, list[str]]:
    if sns not in g.LOGIN_SCRIPTS:
        raise g.InputError("ログインするSNSが不正です。")
    return g.LOGIN_SCRIPTS[sns], []


class Dialogs(Protocol):
    def open_files(self, initial_dir: str) -> list[str]: ...

    def save_file(self, initial_dir: str, filename: str) -> Optional[str]: ...


def python_for_scripts() -> str:
    """スクリプト実行用のPython。pythonw.exeで起動された場合もコンソール版のpython.exeを使う。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return str(exe)


def open_in_os(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # noqa: S606 - 利用者のPCでファイルを開くだけ
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def cdp_alive(url: str = g.DEFAULT_CDP_URL, timeout: float = 0.5) -> bool:
    """ログイン用Chromeがデバッグポートで待ち受けているか。"""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/json/version", timeout=timeout) as res:
            return res.status == 200
    except (OSError, ValueError):
        return False


class Job:
    """実行中（または直前）のスクリプト1本。出力は行のリストに溜め、画面が差分を取りに来る。"""

    def __init__(self, title: str, proc: subprocess.Popen,
                 accept: Callable[[str], bool] = lambda path: True, task: str = ""):
        self.title = title
        self.task = task
        self.proc = proc
        self._accept = accept
        self.lines: list[str] = []
        self.outputs: list[str] = []
        self.code: Optional[int] = None
        self.done = False
        self.stopped = False
        self.counted = False
        self.started_at = datetime.now()
        self._lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            with self._lock:
                self.lines.append(line)
                path = g.extract_output_path(line)
                # 標準出力には他人の投稿本文も流れる。出力先フォルダの中だと確かめられたものだけ覚える
                if path and self._accept(path):
                    self.outputs.append(path)
        code = self.proc.wait()
        with self._lock:
            self.code = None if self.stopped else code
            self.done = True

    def snapshot(self, since: int) -> dict:
        with self._lock:
            return {
                "title": self.title,
                "lines": self.lines[since:],
                "next": len(self.lines),
                "done": self.done,
                "code": self.code,
                "outputs": list(self.outputs),
                "elapsed": int((datetime.now() - self.started_at).total_seconds()),
            }


class Api:
    def __init__(self, root: Path = ROOT, dialogs: Optional[Dialogs] = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 opener: Callable[[Path], None] = open_in_os,
                 cdp_check: Callable[[], bool] = cdp_alive,
                 clock: Callable[[], float] = time.monotonic):
        self._root = Path(root)
        self._dialogs = dialogs
        self._popen = popen
        self._opener = opener
        self._cdp_check = cdp_check
        self._job: Optional[Job] = None
        self._last_jsonl: list[str] = []
        self._clock = clock
        self._cooldown_until: dict[str, float] = {}

    # pywebviewはアンダースコアで始まらない属性を画面に公開するため、内部用は_付きにする

    @property
    def _config_path(self) -> Path:
        return self._root / "config.yaml"

    def _load_config(self) -> dict:
        try:
            with self._config_path.open(encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            return {}

    def _abs(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self._root / p

    def _running(self) -> bool:
        return self._job is not None and not self._job.done

    def _is_allowed_output(self, path: str) -> bool:
        return g.is_allowed_output(path, self._root, g.output_dirs(self._load_config(), self._root))

    def _cooldown_minutes(self, task: str) -> int:
        """この操作をあと何分待つ必要があるか（待たなくてよければ0）。"""
        group = COOLDOWN_GROUPS.get(task)
        remaining = self._cooldown_until.get(group, 0.0) - self._clock() if group else 0.0
        return max(0, int(-(-remaining // 60)))

    def _note_result(self, job: Job) -> None:
        group = COOLDOWN_GROUPS.get(job.task)
        if group and job.code in COOLDOWN_CODES:
            self._cooldown_until[group] = max(self._cooldown_until.get(group, 0.0),
                                              self._clock() + COOLDOWN_SECONDS)

    # ---- 状態 ----

    def get_state(self) -> dict:
        config = self._load_config()
        return {
            "ok": True,
            "accounts": {sns: g.get_watch_accounts(config, sns) for sns in g.SNS_LABELS},
            "keywords": [str(v) for v in (config.get("interest_keywords") or [])],
            "use_cdp": g.cdp_enabled(config),
            "browser": config.get("browser") or "chromium",
            "browser_choices": BROWSER_CHOICES,
            "login": self.get_login_status()["status"],
            "running": self._running(),
            "last_files": list(self._last_jsonl),
            "config_found": self._config_path.exists(),
        }

    def get_login_status(self) -> dict:
        config = self._load_config()
        alive = self._cdp_check() if g.cdp_enabled(config) else False
        status = g.login_status(config, self._root, alive)
        return {"ok": True, "status": status}

    # ---- 実行 ----

    def run(self, task: str, params: Optional[dict] = None) -> dict:
        if self._running():
            return {"ok": False, "error": "実行中の処理があります。終わってから実行してください。"}
        if task not in TASKS:
            return {"ok": False, "error": "不明な操作です。"}
        wait = self._cooldown_minutes(task)
        if wait:
            return {"ok": False, "error": f"SNS側に止められた直後のため、あと約{wait}分は実行できません。"
                                          "すぐに繰り返すとアカウントを凍結されやすくなります。時間をあけてください。"}
        title, build = TASKS[task]
        try:
            script, args = build(params or {})
        except g.InputError as exc:
            return {"ok": False, "error": str(exc)}
        if task == "login":
            title = f"{g.SNS_LABELS[params['sns']]}にログイン"
        cmd = g.script_command(python_for_scripts(), self._root / "scripts", script, args)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        kwargs = {}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = self._popen(cmd, cwd=self._root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, env=env, text=True, encoding="utf-8",
                               errors="replace", bufsize=1, **kwargs)
        except OSError as exc:
            return {"ok": False, "error": f"起動できませんでした: {exc}"}
        self._job = Job(title, proc, accept=self._is_allowed_output, task=task)
        return {"ok": True, "title": title}

    def poll(self, since: int = 0) -> dict:
        if self._job is None:
            return {"ok": True, "idle": True}
        snap = self._job.snapshot(int(since or 0))
        if snap["done"]:
            if not self._job.counted:
                self._job.counted = True
                self._note_result(self._job)
            snap["message"] = g.exit_message(snap["code"])
            snap["status"] = SHORT_STATUS.get(snap["code"], f"エラーで止まりました（終了コード{snap['code']}）")
            jsonl = [str(self._abs(p)) for p in snap["outputs"] if p.endswith(".jsonl")]
            if jsonl:
                self._last_jsonl = jsonl
        snap["ok"] = True
        snap["last_files"] = list(self._last_jsonl)
        return snap

    def stop(self) -> dict:
        if not self._running():
            return {"ok": False, "error": "実行中の処理はありません。"}
        self._job.stopped = True
        self._job.proc.terminate()
        return {"ok": True}

    # ---- ファイル ----

    def open_path(self, path: str) -> dict:
        # 画面から渡されたパスは、出力先フォルダの中だと確かめてから開く（存在確認より先に行う）
        if not self._is_allowed_output(path):
            return {"ok": False, "error": "収集結果のフォルダの外にあるため開けません。"}
        target = self._abs(path)
        if not target.exists():
            return {"ok": False, "error": f"まだありません: {target}"}
        self._opener(target)
        return {"ok": True}

    def open_folder(self, name: str) -> dict:
        folders = {"raw": "data/raw", "digest": "digest", "media": "data/media"}
        if name not in folders:
            return {"ok": False, "error": "不明なフォルダです。"}
        return self.open_path(folders[name])

    def choose_files(self) -> dict:
        if self._dialogs is None:
            return {"ok": False, "error": "ファイル選択が使えません。"}
        raw = self._root / "data" / "raw"
        files = self._dialogs.open_files(str(raw if raw.exists() else self._root))
        return {"ok": True, "files": list(files or [])}

    def export_csv(self, files: Optional[list] = None, open_after: bool = True) -> dict:
        files = [f for f in (files or []) if f]
        if not files:
            return {"ok": False, "error": "CSVにするファイルを選んでください。"}
        default = g.default_csv_path(files)
        out: Optional[str] = str(default)
        if self._dialogs is not None:
            out = self._dialogs.save_file(str(default.parent), default.name)
            if not out:
                return {"ok": False, "cancelled": True}
        try:
            count = g.write_csv(files, out)
        except (OSError, g.InputError) as exc:
            return {"ok": False, "error": f"CSVを作れませんでした: {exc}"}
        if open_after:
            self._opener(Path(out))
        return {"ok": True, "path": out, "rows": count}

    # ---- ログイン用Chrome ----

    def launch_chrome(self) -> dict:
        chrome = g.find_chrome()
        if not chrome:
            return {"ok": False, "error": "Google Chromeが見つかりませんでした。Chromeをインストールしてから押してください。"}
        cmd = g.chrome_command(chrome, self._root / g.CHROME_PROFILE_DIRNAME,
                               start_urls=["https://x.com/login", "https://www.tiktok.com/login"])
        try:
            self._popen(cmd, cwd=self._root)
        except OSError as exc:
            return {"ok": False, "error": f"Chromeを起動できませんでした: {exc}"}
        enabled_now = False
        if not g.cdp_enabled(self._load_config()):
            result = self._write_config(lambda text: g.set_cdp_urls(text, True))
            enabled_now = result["ok"]
        return {"ok": True, "enabled_cdp": enabled_now}

    # ---- 設定 ----

    def _write_config(self, transform: Callable[[str], str]) -> dict:
        try:
            text = self._config_path.read_text(encoding="utf-8")
            new_text = transform(text)
            yaml.safe_load(new_text)  # 壊れたYAMLは書き込まない
            self._config_path.with_name("config.yaml.bak").write_text(text, encoding="utf-8")
            self._config_path.write_text(new_text, encoding="utf-8")
        except (OSError, yaml.YAMLError) as exc:
            return {"ok": False, "error": f"設定を保存できませんでした: {exc}"}
        return {"ok": True}

    def save_settings(self, settings: dict) -> dict:
        if self._running():
            return {"ok": False, "error": "実行中は設定を保存できません。終わってから保存してください。"}
        settings = settings or {}
        try:
            accounts = {sns: g.parse_account_lines((settings.get("accounts") or {}).get(sns, ""))
                        for sns in g.SNS_LABELS}
        except g.InputError as exc:
            return {"ok": False, "error": str(exc)}
        keywords = g.parse_lines(settings.get("keywords", ""))
        use_cdp = bool(settings.get("use_cdp"))
        browser = settings.get("browser") or "chromium"
        if browser not in {c["value"] for c in BROWSER_CHOICES}:
            return {"ok": False, "error": "ブラウザの指定が不正です。"}

        def transform(text: str) -> str:
            for sns, values in accounts.items():
                text = g.set_watch_accounts(text, sns, values)
            text = g.set_interest_keywords(text, keywords)
            text = g.set_config_value(text, None, "browser", browser)
            return g.set_cdp_urls(text, use_cdp)

        result = self._write_config(transform)
        if result["ok"]:
            result["state"] = self.get_state()
        return result
