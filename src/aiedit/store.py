"""data/ 以下のファイルによる簡易データベース。GitHub Actionsがコミットして永続化する。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .common import DATA_DIR, now_utc


def _path(*parts: str) -> Path:
    p = DATA_DIR.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def read_json(rel: str, default: Any = None) -> Any:
    p = _path(rel)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(rel: str, obj: Any) -> None:
    _path(rel).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(rel: str, obj: dict) -> None:
    with _path(rel).open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(rel: str) -> list[dict]:
    p = _path(rel)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---- 状態（停止スイッチなど） ----

def state() -> dict:
    return read_json("state.json", {"halted": False, "halt_reason": "", "generated_slots": []})


def save_state(s: dict) -> None:
    s["generated_slots"] = s.get("generated_slots", [])[-60:]
    write_json("state.json", s)


def halt(reason: str) -> None:
    s = state()
    s["halted"] = True
    s["halt_reason"] = reason
    s["halted_at"] = now_utc().isoformat()
    save_state(s)


def resume() -> None:
    s = state()
    s["halted"] = False
    s["halt_reason"] = ""
    save_state(s)


# ---- 下書き ----

def save_draft(draft: dict) -> None:
    write_json(f"drafts/{draft['id']}.json", draft)


def load_draft(draft_id: str) -> dict | None:
    if not draft_id.replace("-", "").replace("_", "").isalnum():
        return None
    return read_json(f"drafts/{draft_id}.json")


# ---- 投稿履歴 ----

def posts(include_deleted: bool = False) -> list[dict]:
    """投稿の記録。削除した投稿は、分析・まとめ・重複判定の対象から外す。"""
    rows = read_jsonl("posts.jsonl")
    return rows if include_deleted else [p for p in rows if not p.get("deleted")]


def mark_post_deleted(tweet_id: str) -> None:
    rows = read_jsonl("posts.jsonl")
    for p in rows:
        if p["tweet_id"] == tweet_id:
            p["deleted"] = now_utc().isoformat()
    _path("posts.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def recent_posts(hours: float) -> list[dict]:
    cutoff = now_utc() - timedelta(hours=hours)
    return [p for p in posts() if datetime.fromisoformat(p["posted_at"]) >= cutoff]


def record_post(entry: dict) -> None:
    append_jsonl("posts.jsonl", entry)


def record_decision(entry: dict) -> None:
    """承認・却下の記録（編集長AIの採点基準の改善に使う）。"""
    append_jsonl("decisions.jsonl", entry)


def recent_texts(limit: int = 60) -> Iterable[str]:
    return [p["text"] for p in posts()[-limit:]]


def read_text(rel: str, default: str = "") -> str:
    p = _path(rel)
    return p.read_text(encoding="utf-8") if p.exists() else default


def write_text(rel: str, text: str) -> None:
    _path(rel).write_text(text, encoding="utf-8")


def write_bytes(rel: str, data: bytes) -> None:
    _path(rel).write_bytes(data)
