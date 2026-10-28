"""改善AI: 投稿の成果を分析し、編集・執筆AIが毎回読む「運用ノート（playbook）」を更新する。

一度のバズを恒久的な成功パターンと決めつけないよう、件数が少ないうちは仮説として扱う。
"""
from __future__ import annotations

import json
from statistics import median

from . import line, llm, store
from .common import JST, log, now_utc

MIN_POSTS = 5  # これ未満の計測件数では、傾向を断定しない


def _engagement(m: dict) -> float:
    imp = m.get("impressions") or 0
    if not imp:
        return 0.0
    return (m["likes"] + m["reposts"] + m["replies"] + m["bookmarks"]) / imp


def dataset() -> list[dict]:
    metrics = store.read_json("metrics.json", {})
    rows = []
    for p in store.posts():
        m = metrics.get(p["tweet_id"], {}).get("24h")
        if not m:
            continue
        rows.append({
            "tweet_id": p["tweet_id"], "text": p["text"], "kind": p.get("kind"), "format": p.get("format", "short"),
            "style": p.get("style"), "has_image": p.get("has_image", False), "score": p.get("total"),
            "hour": int(p["posted_at_jst"][11:13]), "length": p.get("length"),
            "impressions": m.get("impressions", 0), "engagement_rate": round(_engagement(m), 4),
            "bookmarks": m.get("bookmarks", 0), "profile_clicks": m.get("profile_clicks"),
        })
    return rows


def group_stats(rows: list[dict], key: str) -> dict:
    groups: dict = {}
    for r in rows:
        groups.setdefault(str(r[key]), []).append(r)
    return {k: {"n": len(v), "median_impressions": median(x["impressions"] for x in v),
                "median_engagement_rate": round(median(x["engagement_rate"] for x in v), 4)}
            for k, v in groups.items()}


def run(send_weekly_report: bool = False) -> dict:
    rows = dataset()
    followers = store.read_jsonl("followers.jsonl")[-14:]
    deleted = [d for d in store.read_jsonl("decisions.jsonl") if d["action"] in ("delete", "reject_quality", "reject_inaccurate")][-10:]
    rejected = store.read_jsonl("rejected_variants.jsonl")[-15:]
    ranked = sorted(rows, key=lambda r: r["impressions"], reverse=True)
    stats = {key: group_stats(rows, key) for key in ("format", "kind", "style", "has_image", "hour")} if rows else {}

    report = {
        "at": now_utc().isoformat(),
        "measured_posts": len(rows),
        "stats": stats,
        "top5": [{k: r[k] for k in ("text", "impressions", "engagement_rate", "style", "format", "hour")} for r in ranked[:5]],
        "bottom5": [{k: r[k] for k in ("text", "impressions", "engagement_rate", "style", "format", "hour")} for r in ranked[-5:]] if len(ranked) > 5 else [],
        "followers": followers,
        "rejected_by_admin": [{"action": d["action"], "text": d["text"][:200]} for d in deleted],
        "rejected_by_checks": [{"errors": r["errors"], "style": r["style"]} for r in rejected],
    }

    system = llm.prompt("learn")
    user = json.dumps(report, ensure_ascii=False)
    out = llm.generate_json(system, user)
    playbook = out.get("playbook", [])
    if playbook:
        stamp = now_utc().astimezone(JST).strftime("%Y-%m-%d")
        body = "\n".join(f"- {line_}" for line_ in playbook[:12])
        store.write_text("playbook.md", f"（{stamp}更新・計測済み投稿{len(rows)}件にもとづく）\n{body}\n")
    report["ai"] = out
    store.write_json(f"learn/{now_utc().astimezone(JST):%Y%m%d}.json", report)

    if send_weekly_report:
        line.notify(_weekly_text(report, out))
    log.info("運用ノートを更新しました（計測済み%d件）", len(rows))
    return report


def _weekly_text(report: dict, out: dict) -> str:
    f = report["followers"]
    delta = (f[-1]["followers"] - f[-8]["followers"]) if len(f) >= 8 and f[-1].get("followers") is not None else None
    lines = ["📊 週間運用レポート"]
    if f and f[-1].get("followers") is not None:
        lines.append(f"フォロワー: {f[-1]['followers']}人" + (f"（1週間で{delta:+d}）" if delta is not None else ""))
    lines.append(f"計測済みの投稿: {report['measured_posts']}件")
    if report["top5"]:
        lines.append("\n🏆 よく読まれた投稿")
        for r in report["top5"][:3]:
            lines.append(f"・{r['impressions']}表示｜{r['text'][:40]}…")
    return "\n".join(lines)


def playbook_text() -> str:
    """毎日の運用ノートと、改善会議で決めた今週の方針。"""
    parts = [store.read_text("playbook.md", ""), store.read_text("strategy.md", ""),
             store.read_text("experiments.md", "")]
    return "\n".join(p for p in parts if p)
