"""返信の候補（半自動）: 同じジャンルで伸びている投稿を探し、返信の文案を付けてLINEで管理者に送る。

投稿は管理者が自分で選んで手で行う（自動返信はしない）。
返信の数字は、元の投稿か、このアカウントの実データ（材料）にあるものだけを使う（それ以外の数字を含む文案は捨てる）。
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime

from . import line, llm, research, store, xapi
from .common import JST, log, now_utc, read_list, settings

NUM = re.compile(r"[+\-−]?\d[\d,]*(?:\.\d+)?")


def _cfg() -> dict:
    return settings().get("reply_ideas", {})


def candidates() -> list[dict]:
    """返信先の候補: 直近の同ジャンルの投稿のうち、反応があり、自分より大きい（伸びている）アカウントのもの。"""
    cfg = _cfg()
    me = settings()["account"].get("handle", "satoshikun_666")
    out, seen = [], set()
    for q in cfg.get("queries", []):
        try:
            tweets, users = xapi.search_recent(q, cfg.get("per_query", 10))
        except xapi.XPostError as e:
            log.warning("返信候補の検索に失敗: %s", e)
            continue
        store.append_jsonl("sources/x_reads.jsonl", {"month": now_utc().astimezone(JST).strftime("%Y-%m"),
                                                     "at": now_utc().isoformat(), "n": len(tweets), "for": "reply"})
        for t in tweets:
            u = users.get(t.get("author_id"), {})
            name = u.get("username", "")
            followers = (u.get("public_metrics") or {}).get("followers_count") or 0
            m = t.get("public_metrics", {})
            age_h = (now_utc() - datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))).total_seconds() / 3600
            if name == me or t["id"] in seen or age_h > cfg.get("max_age_hours", 12):
                continue
            if not cfg.get("min_followers", 2000) <= followers <= cfg.get("max_followers", 500000):
                continue
            seen.add(t["id"])
            out.append({"id": t["id"], "author": name, "followers": followers, "age_h": round(age_h, 1),
                        "text": html.unescape(t.get("text", ""))[:500], "likes": m.get("like_count", 0),
                        "replies": m.get("reply_count", 0), "reposts": m.get("retweet_count", 0),
                        "has_media": bool((t.get("attachments") or {}).get("media_keys"))})
    # 同ジャンルのリサーチ（伸びた投稿の型の分析）にも使う
    if out:
        path = f"sources/x/{now_utc().astimezone(JST):%Y%m%d}.json"
        old = store.read_json(path, []) or []
        store.write_json(path, old + [{"query": "reply", "author": c["author"], "author_followers": c["followers"],
                                       "text": c["text"], "has_media": c["has_media"], "likes": c["likes"],
                                       "reposts": c["reposts"], "bookmarks": 0, "impressions": 0} for c in out])
    return sorted(out, key=lambda c: c["likes"] + 3 * c["replies"], reverse=True)[:25]


def _numbers_ok(reply: str, allowed_text: str) -> bool:
    for term in sorted(read_list("allowed_numeric_terms.txt"), key=len, reverse=True):
        reply = reply.replace(term, "")  # HIP-3・S&P500・24時間 などの決まった語は数字として数えない
    allowed = {n.replace(",", "").replace("−", "-").lstrip("+") for n in NUM.findall(allowed_text)}
    return all(n.replace(",", "").replace("−", "-").lstrip("+") in allowed for n in NUM.findall(reply))


def build() -> list[dict]:
    cands = candidates()
    if not cands:
        return []
    facts_text = research.watch_summary(12)
    material = {"candidates": cands, "my_data_last_12h": facts_text,
                "my_recent_posts": [p["text"][:200] for p in store.recent_posts(48)][-6:]}
    out = llm.generate_json(llm.prompt("reply_ideas"), json.dumps(material, ensure_ascii=False))
    by_id = {c["id"]: c for c in cands}
    picks = []
    banned = read_list("banned_phrases.txt")
    for i in out.get("ideas", []):
        c = by_id.get(str(i.get("id")))
        reply = (i.get("reply") or "").strip()
        if not c or not reply or len(reply) > 140:
            continue
        if not _numbers_ok(reply, c["text"] + "\n" + facts_text):
            log.info("根拠のない数字を含む返信案を捨てました: %s", reply)
            continue
        if any(b in reply for b in banned) or "http" in reply or "#" in reply:
            continue
        picks.append({**c, "reply": reply, "why": i.get("why", "")})
    return picks[: _cfg().get("max_ideas", 8)]


def run() -> list[dict]:
    picks = build()
    day = now_utc().astimezone(JST).strftime("%Y%m%d")
    store.write_json(f"reply_ideas/{day}.json", picks)
    if not picks:
        log.info("返信の候補が見つかりませんでした")
        return []
    lines = ["💬 今日の返信候補（手で返信してください）", "伸びている同ジャンルの投稿に、データを添えた返信をすると、相手のフォロワーに見つけてもらえます。"]
    marks = "①②③④⑤⑥⑦⑧⑨⑩"
    for n, p in enumerate(picks):
        fl = f"{p['followers'] / 10000:.1f}万" if p["followers"] >= 10000 else f"{p['followers']:,}"
        lines.append(f"\n{marks[n]} @{p['author']}（フォロワー{fl}・{p['age_h']:.0f}時間前・いいね{p['likes']}）\n"
                     f"元: {p['text'][:70].replace(chr(10), ' ')}…\n"
                     f"https://x.com/{p['author']}/status/{p['id']}\n"
                     f"返信案: {p['reply']}")
    lines.append("\n※返信案はそのままでも、少し自分の言葉に変えてもOK。数字は元の投稿か当アカウントのデータにあるものだけです。")
    line.notify("\n".join(lines)[:4900])
    return picks

