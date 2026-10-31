"""改善会議: 週次（改善担当が各担当と連携）と月次（過去の改善策を検証する大型会議）。

結果はLINEで報告しない。会議で決めた方針は次の投稿・記事から自動で反映される:
  data/strategy.md       … 編集長・執筆AIが毎回読む「今週の方針」
  data/note_strategy.md  … note記事の執筆AIが読む方針
  data/image_prefs.json  … 画像を付ける投稿の形式（短文/長文）
  data/improvements.json … 改善策の台帳（担当・仮説・測る指標・開始日・評価）
  data/improve/backlog.md … コードの変更が必要な改善（画像のデザインなど。開発のときに対応する）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from statistics import median

from . import learn, llm, sources, store
from .common import JST, log, now_utc

ROLES = ["情報収集", "編集長", "投稿制作", "画像", "分析", "note", "成長戦略"]


def _window(rows: list[dict], start: datetime, end: datetime) -> dict:
    sel = [r for r in rows if start <= datetime.fromisoformat(r["posted_at"]) < end]
    if not sel:
        return {"n": 0}
    return {"n": len(sel), "median_impressions": median(r["impressions"] for r in sel),
            "median_engagement_rate": round(median(r["engagement_rate"] for r in sel), 4)}


def _rows() -> list[dict]:
    posted = {p["tweet_id"]: p["posted_at"] for p in store.posts()}
    rows = learn.dataset()
    for r in rows:
        r["posted_at"] = posted[r["tweet_id"]]
    return rows


def _evaluate(actions: list[dict], rows: list[dict]) -> list[dict]:
    """改善策ごとに、開始前2週間と開始後の投稿の反応を比べる。"""
    out = []
    for a in actions:
        start = datetime.fromisoformat(a["started_at"])
        out.append({**a, "before": _window(rows, start - timedelta(days=14), start),
                    "after": _window(rows, start, now_utc())})
    return out


def _note_summary() -> list[dict]:
    stats = store.read_json("note/stats.json", {})
    out = []
    for key, s in stats.items():
        likes = [v for _, v in sorted(s["likes"].items()) if v is not None]
        out.append({"title": s["title"], "published": s["published"], "likes": likes[-1] if likes else None})
    checks = [store.read_json(f"note/checks/{k}.json") for k in store.read_json("note/checked.json", {})]
    return sorted(out, key=lambda x: x["published"] or "", reverse=True)[:15] + [
        {"check": c["title"], "issues": c["issues"] + c["proofread"]} for c in checks if c][-5:]


def materials(monthly: bool) -> dict:
    now = now_utc()
    rows = _rows()
    days = 35 if monthly else 7
    recent = [r for r in rows if now - datetime.fromisoformat(r["posted_at"]) < timedelta(days=days)]
    ranked = sorted(recent, key=lambda r: r["impressions"], reverse=True)
    pick = ("text", "impressions", "engagement_rate", "bookmarks", "style", "format", "kind", "has_image", "hour")
    ledger = store.read_json("improvements.json", [])
    active = [a for a in ledger if a.get("status") in ("active", "測定中")]
    mat = {
        "period_days": days,
        "this_period": _window(rows, now - timedelta(days=days), now),
        "previous_period": _window(rows, now - timedelta(days=2 * days), now - timedelta(days=days)),
        "stats": {k: learn.group_stats(recent, k) for k in ("format", "kind", "style", "has_image", "hour")} if recent else {},
        "top": [{k: r[k] for k in pick} for r in ranked[:6]],
        "bottom": [{k: r[k] for k in pick} for r in ranked[-4:]] if len(ranked) > 6 else [],
        "followers": store.read_jsonl("followers.jsonl")[-(40 if monthly else 10):],
        "rejected_by_checks": [r["errors"] for r in store.read_jsonl("rejected_variants.jsonl")[-20:]],
        "improvements_in_progress": _evaluate(active, rows),
        # 削除・障害の本当の理由（投稿の削除を「話題がだめだった」と誤解しないため。11/1 会議でバイナンスの件を「ノイズ」と誤解した）
        "incidents_and_deletion_reasons": store.read_text("improve/incidents.md", "").strip().splitlines()[-15:],
        # アカウントの範囲は管理者が決めたもの。会議で狭めない
        "account_scope_set_by_admin": _scope_text(),
        "current_strategy": store.read_text("strategy.md", ""),
        "current_note_strategy": store.read_text("note_strategy.md", ""),
        "image_prefs": image_prefs(),
        "daily_playbook": store.read_text("playbook.md", ""),
        "note": _note_summary(),
        "research_x": [{k: x[k] for k in ("author_followers", "text", "has_media", "likes", "reposts", "bookmarks")}
                       for x in sources.latest("x", days)[:20]],
        "research_note": sorted(sources.latest("note", days), key=lambda n: n.get("likes") or 0, reverse=True)[:20],
        "research_news": [n["title"] for n in sources.latest("news", 3)][:20],
    }
    if monthly:
        mat["weekly_meetings"] = [
            {k: w.get(k) for k in ("at", "decisions", "actions", "evaluations")}
            for w in (store.read_json(f"improve/{(now - timedelta(days=d)).astimezone(JST):%Y%m%d}-weekly.json")
                      for d in range(1, 36)) if w
        ]
        mat["all_improvements"] = _evaluate(ledger[-30:], rows)
    return mat


def _scope_text() -> str:
    from .common import CONFIG_DIR
    text = (CONFIG_DIR / "buzz_patterns.md").read_text(encoding="utf-8")
    start = text.find("## 前提")
    end = text.find("## ", start + 3)
    return text[start:end].strip() if start >= 0 else ""


def image_prefs() -> dict:
    return store.read_json("image_prefs.json", {"short": True, "long": True})


def run(monthly: bool = False) -> dict:
    kind = "monthly" if monthly else "weekly"
    mat = materials(monthly)
    out = llm.generate_json(llm.prompt(f"improve_{kind}"), json.dumps(mat, ensure_ascii=False, default=str))
    now = now_utc()
    stamp = now.astimezone(JST).strftime("%Y-%m-%d")

    # 既存の改善策の評価を台帳に反映する
    ledger = store.read_json("improvements.json", [])
    verdicts = {e.get("id"): e for e in out.get("evaluations", [])}
    for a in ledger:
        v = verdicts.get(a["id"])
        if v and v.get("verdict") in ("継続", "中止", "定着", "測定中"):
            a["status"] = {"継続": "active", "測定中": "active"}.get(v["verdict"], v["verdict"])
            a.setdefault("history", []).append({"at": stamp, "verdict": v["verdict"], "reason": v.get("reason", "")})
    base = len(ledger)
    for i, a in enumerate(out.get("actions", [])[:5], 1):
        if a.get("owner") not in ROLES or not a.get("change"):
            continue
        ledger.append({"id": f"I{base + i:03d}", "owner": a["owner"], "change": a["change"],
                       "hypothesis": a.get("hypothesis", ""), "metric": a.get("metric", ""),
                       "started_at": now.isoformat(), "status": "active", "meeting": kind})
    store.write_json("improvements.json", ledger)

    if out.get("strategy"):
        store.write_text("strategy.md", f"（{stamp}の{'月次' if monthly else '週次'}改善会議で決定）\n"
                         + "\n".join(f"- {s}" for s in out["strategy"][:10]) + "\n")
    if out.get("note_strategy"):
        store.write_text("note_strategy.md", f"（{stamp}決定）\n" + "\n".join(f"- {s}" for s in out["note_strategy"][:8]) + "\n")
    prefs = out.get("image_prefs") or {}
    if isinstance(prefs, dict) and prefs:
        cur = image_prefs()
        cur.update({k: bool(v) for k, v in prefs.items() if k in ("short", "long")})
        store.write_json("image_prefs.json", cur)
    if out.get("dev_backlog"):
        old = store.read_text("improve/backlog.md", "# 開発が必要な改善（会議で出たもの。対応したら消す）\n")
        new = [b for b in out["dev_backlog"][:5] if b not in old]
        if new:
            store.write_text("improve/backlog.md", old + "".join(f"- [ ] {stamp} {b}\n" for b in new))

    record = {"at": now.isoformat(), "kind": kind, **out}
    store.write_json(f"improve/{now.astimezone(JST):%Y%m%d}-{kind}.json", record)
    log.info("%s改善会議を行いました（新しい改善策 %d件）", "月次" if monthly else "週次", len(ledger) - base)
    return record


def strategy_text() -> str:
    return store.read_text("strategy.md", "")


def note_strategy_text() -> str:
    return store.read_text("note_strategy.md", "")
