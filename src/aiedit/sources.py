"""外部リサーチ: ニュース・noteの人気記事・Xの同ジャンルの伸びた投稿を集める。

ここで集めたものは「流れ・型・テーマ」の参考にだけ使い、投稿の数字には使わない（数字は必ず材料から）。
他者の文章は転載しない。改善会議では「なぜ伸びたか（書き出し・構成・画像の使い方）」だけを抽出する。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import timedelta
from urllib.parse import quote

import requests

from . import store
from .common import JST, log, now_utc, settings

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; x-ai-editorial/1.0)"}


def _cfg() -> dict:
    return settings().get("research", {})


def _error(kind: str, query: str, e: object) -> None:
    """取得に失敗した理由を残す（Actionsのログを見なくても原因がわかるように）。"""
    store.append_jsonl("sources/errors.jsonl", {"at": now_utc().isoformat(), "kind": kind, "query": query,
                                                "error": str(e)[:300]})


# ---------- ニュース（Google ニュースのRSS。無料） ----------

def news() -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for q in _cfg().get("news_queries", []):
        url = f"https://news.google.com/rss/search?q={quote(q)}+when:1d&hl=ja&gl=JP&ceid=JP:ja"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except (requests.RequestException, ET.ParseError) as e:
            log.warning("ニュースの取得に失敗 %s: %s", q, e)
            continue
        for it in list(root.iter("item"))[:10]:
            title = (it.findtext("title") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            src = it.find("source")
            items.append({"query": q, "title": title, "source": src.text if src is not None else "",
                          "published": it.findtext("pubDate") or ""})
    if items:
        store.write_json(f"sources/news/{now_utc().astimezone(JST):%Y%m%d}.json", items)
    return items


def news_digest(n: int = 8) -> str:
    """編集長AIに渡す、直近のニュース見出し。"""
    today = now_utc().astimezone(JST)
    for d in (today, today - timedelta(days=1)):
        items = store.read_json(f"sources/news/{d:%Y%m%d}.json")
        if items:
            return "\n".join(f"- {i['title']}" for i in items[:n])
    return ""


# ---------- noteの人気記事（公開の検索。無料） ----------

def note_trends() -> list[dict]:
    out: list[dict] = []
    for q in _cfg().get("note_queries", []):
        try:
            r = requests.get("https://note.com/api/v3/searches",
                             params={"context": "note", "q": q, "size": 20, "start": 0, "sort": "popular"},
                             headers=HEADERS, timeout=30)
            r.raise_for_status()
            notes = (r.json().get("data", {}).get("notes") or {}).get("contents", [])
        except (requests.RequestException, ValueError) as e:
            log.warning("noteの検索に失敗 %s: %s", q, e)
            _error("note", q, e)
            continue
        if not notes:
            _error("note", q, f"0件 keys={list(r.json().get('data', {}).keys())[:10]}")
        for n in notes:
            out.append({
                "query": q, "title": n.get("name"),
                "likes": n.get("like_count", n.get("likeCount")),
                "price": n.get("price"),
                "creator": (n.get("user") or {}).get("urlname"),
                "published": n.get("publish_at", n.get("publishAt")),
            })
    if out:
        store.write_json(f"sources/note/{now_utc().astimezone(JST):%Y%m%d}.json", out)
    return out


# ---------- Xの同ジャンルの投稿（有料API。月の上限つき） ----------

def _reads_this_month() -> int:
    month = now_utc().astimezone(JST).strftime("%Y-%m")
    return sum(r["n"] for r in store.read_jsonl("sources/x_reads.jsonl") if r["month"] == month)


def x_search() -> list[dict]:
    """同ジャンルで伸びた投稿を検索する。読んだ件数ぶん課金されるので、月の上限を守る。"""
    from . import xapi

    cfg = _cfg().get("x", {})
    if not cfg.get("enabled"):
        return []
    budget = cfg.get("monthly_read_limit", 400) - _reads_this_month()
    queries = list(cfg.get("queries", []))
    accounts = cfg.get("accounts", [])
    if accounts:
        queries.append("(" + " OR ".join(f"from:{a.lstrip('@')}" for a in accounts) + ") -is:retweet -is:reply")
    out: list[dict] = []
    for q in queries:
        size = min(cfg.get("per_query", 30), budget)
        if size < 10:
            log.info("Xリサーチは今月の上限に達しました")
            break
        try:
            tweets, users = xapi.search_recent(q, size)
        except xapi.XPostError as e:
            log.warning("Xの検索に失敗 %s: %s", q, e)
            _error("x", q, e)
            continue
        budget -= len(tweets)
        store.append_jsonl("sources/x_reads.jsonl", {"month": now_utc().astimezone(JST).strftime("%Y-%m"),
                                                     "at": now_utc().isoformat(), "n": len(tweets)})
        for t in tweets:
            m = t.get("public_metrics", {})
            u = users.get(t.get("author_id"), {})
            followers = (u.get("public_metrics") or {}).get("followers_count") or 0
            out.append({
                "query": q, "author": u.get("username"), "author_followers": followers,
                "text": t.get("text", "")[:400], "created_at": t.get("created_at"),
                "has_media": bool((t.get("attachments") or {}).get("media_keys")),
                "likes": m.get("like_count", 0), "reposts": m.get("retweet_count", 0) + m.get("quote_count", 0),
                "bookmarks": m.get("bookmark_count", 0), "impressions": m.get("impression_count", 0),
            })
    if out:
        out.sort(key=lambda x: x["likes"] + 2 * x["reposts"] + 2 * x["bookmarks"], reverse=True)
        store.write_json(f"sources/x/{now_utc().astimezone(JST):%Y%m%d}.json", out)
    return out


def latest(kind: str, days: int = 8) -> list:
    """sources/<kind>/ の直近のファイルを新しい順にまとめて返す。"""
    today = now_utc().astimezone(JST)
    rows: list = []
    for i in range(days):
        rows += store.read_json(f"sources/{kind}/{today - timedelta(days=i):%Y%m%d}.json", []) or []
    return rows


def run(daily: bool, weekly: bool) -> dict:
    res = {}
    if daily:
        res["news"] = len(news())
        res["note"] = len(note_trends())
    if weekly:
        res["x"] = len(x_search())
    return res
