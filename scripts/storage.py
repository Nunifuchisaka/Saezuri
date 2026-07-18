"""saezuri データ保存ユーティリティ

JSONL形式での生データ書き込み・読み込み、および state/ 配下の
既読ID(seen_ids.json)・アカウント別前回取得ID(<account>_last_id.json)の
読み書き・差分判定を提供する。収集モジュール(search.py 等)から import して使う想定。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional


def append_jsonl(path: str | Path, records: Iterable[dict]) -> int:
    """recordsを1行1レコードのJSONLとしてpathに追記する。書き込んだ件数を返す。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False))
            f.write("\n")
            count += 1
    return count


def read_jsonl(path: str | Path) -> list[dict]:
    """pathからJSONLを読み込み、レコードのリストを返す。ファイルが存在しない場合は空リストを返す。"""
    path = Path(path)
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def _seen_ids_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / "seen_ids.json"


def load_seen_ids(state_dir: str | Path) -> set[str]:
    """state_dir/seen_ids.jsonから既読IDの集合を読み込む。存在しない場合は空集合を返す。"""
    path = _seen_ids_path(state_dir)
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_ids(state_dir: str | Path, seen_ids: Iterable[str]) -> None:
    """既読IDの集合をstate_dir/seen_ids.jsonに保存する(全量上書き)。"""
    path = _seen_ids_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(sorted(set(seen_ids)), f, ensure_ascii=False, indent=2)


def filter_unseen(records: Iterable[dict], seen_ids: set[str], id_key: str = "id") -> list[dict]:
    """seen_idsに含まれないレコードのみを抽出する(既読ID差分判定)。"""
    return [r for r in records if r.get(id_key) not in seen_ids]


def _last_id_path(state_dir: str | Path, account: str) -> Path:
    return Path(state_dir) / f"{account}_last_id.json"


def load_last_id(state_dir: str | Path, account: str) -> Optional[str]:
    """state_dir/<account>_last_id.jsonから前回取得済み最新IDを読み込む。存在しない場合はNoneを返す。"""
    path = _last_id_path(state_dir, account)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f).get("last_id")


def save_last_id(state_dir: str | Path, account: str, last_id: str) -> None:
    """account分の前回最新IDをstate_dir/<account>_last_id.jsonに保存する。"""
    path = _last_id_path(state_dir, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump({"last_id": last_id}, f, ensure_ascii=False, indent=2)
