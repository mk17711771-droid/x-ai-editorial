"""投稿していない時間のリサーチ: 市場の定点観測と、自分の投稿の反応の計測。"""
from __future__ import annotations

from datetime import datetime, timedelta

import requests

from . import collect_hyperliquid, collect_stocks, store, traders, xapi
from .common import JST, env, log, now_utc, settings


def watch() -> dict:
    """市場の定点観測。2時間ごとの記録を残し、次の投稿の編集で「流れ」として使う。"""
    now = now_utc()
    rec: dict = {"at": now.isoformat(), "at_jst": now.astimezone(JST).strftime("%m/%d %H:%M")}
    try:
        cfg = settings()["hyperliquid"]
        markets = [m for m in collect_hyperliquid.fetch_markets() if m["vol_usd"] >= cfg["min_day_volume_usd"]]
        name = collect_hyperliquid.display_name
        rec["hl_movers"] = [(name(m), round(m["change_pct"], 2)) for m in
                            sorted(markets, key=lambda m: abs(m["change_pct"]), reverse=True)[:5]]
        rec["hl_funding"] = [(name(m), round(m["funding_annual_pct"], 1)) for m in
                             sorted(markets, key=lambda m: abs(m["funding_annual_pct"]), reverse=True)[:5]]
        rec["hl_oi_top"] = [(name(m), round(m["oi_usd"] / 1e6)) for m in
                            sorted(markets, key=lambda m: m["oi_usd"], reverse=True)[:5]]
    except requests.RequestException as e:
        log.warning("Hyperliquidの観測に失敗: %s", e)
    if env("FINNHUB_API_KEY", required=False):
        moves = []
        for sym in settings()["stocks"]["watchlist"]:
            try:
                q = collect_stocks.quote(sym)
            except requests.RequestException:
                continue
            if q:
                moves.append((sym, round(float(q["dp"]), 2)))
        rec["stock_movers"] = sorted(moves, key=lambda t: abs(t[1]), reverse=True)[:6]
    try:
        snap = traders.snapshot()
        rec["top_traders"] = [(c["coin"], c["long_n"], c["short_n"]) for c in snap["coins"][:5]]
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning("上位トレーダーの集計に失敗: %s", e)
    store.append_jsonl(f"watch/{now.astimezone(JST):%Y%m%d}.jsonl", rec)
    return rec


def watch_summary(hours: int = 24) -> str:
    """直近の定点観測を、編集長AIに渡す短い文章にする。"""
    now = now_utc()
    lines = []
    for d in (now.astimezone(JST) - timedelta(days=1), now.astimezone(JST)):
        for r in store.read_jsonl(f"watch/{d:%Y%m%d}.jsonl"):
            if now - datetime.fromisoformat(r["at"]) > timedelta(hours=hours):
                continue
            parts = [r["at_jst"]]
            if r.get("hl_funding"):
                parts.append("HL資金調達率上位 " + ", ".join(f"{c} {v:+}%" for c, v in r["hl_funding"][:3]))
            if r.get("hl_movers"):
                parts.append("HL変動上位 " + ", ".join(f"{c} {v:+}%" for c, v in r["hl_movers"][:3]))
            if r.get("top_traders"):
                parts.append("上位トレーダー保有 " + ", ".join(f"{c} 買{lo}/売{sh}" for c, lo, sh in r["top_traders"][:3]))
            if r.get("stock_movers"):
                parts.append("米国株変動上位 " + ", ".join(f"{c} {v:+}%" for c, v in r["stock_movers"][:3]))
            lines.append(" / ".join(parts))
    return "\n".join(lines[-12:])


def _metrics_of(t: dict) -> dict:
    pub = t.get("public_metrics", {})
    npm = t.get("non_public_metrics", {})
    m = {
        "impressions": npm.get("impression_count", pub.get("impression_count", 0)),
        "likes": pub.get("like_count", 0),
        "reposts": pub.get("retweet_count", 0) + pub.get("quote_count", 0),
        "replies": pub.get("reply_count", 0),
        "bookmarks": pub.get("bookmark_count", 0),
    }
    if "user_profile_clicks" in npm:
        m["profile_clicks"] = npm["user_profile_clicks"]
    return m


def snapshot(hours: int = 48) -> list[dict]:
    """直近の投稿の、いま時点の反応を取得して data/metrics_now.json に残す（分析・報告用）。"""
    posts = [p for p in store.posts() if now_utc() - datetime.fromisoformat(p["posted_at"]) < timedelta(hours=hours)]
    got = {t["id"]: _metrics_of(t) for t in xapi.get_tweets([p["tweet_id"] for p in posts])} if posts else {}
    rows = [{"posted_at_jst": p["posted_at_jst"], "slot": p["slot_key"], "has_image": p.get("has_image", False),
             "format": p.get("format", "short"), "text": p["text"], **got.get(p["tweet_id"], {})} for p in posts]
    store.write_json("metrics_now.json", {"at": now_utc().isoformat(), "posts": rows})
    return rows


def measure() -> int:
    """投稿の24時間後・7日後の反応と、1日1回フォロワー数を記録する。"""
    now = now_utc()
    metrics = store.read_json("metrics.json", {})
    due: dict[str, str] = {}
    for p in store.posts():
        age = now - datetime.fromisoformat(p["posted_at"])
        got = metrics.get(p["tweet_id"], {})
        if age >= timedelta(days=7) and "7d" not in got and age < timedelta(days=9):
            due[p["tweet_id"]] = "7d"
        elif age >= timedelta(hours=24) and "24h" not in got and age < timedelta(days=3):
            due[p["tweet_id"]] = "24h"
    if due:
        for t in xapi.get_tweets(list(due)):
            metrics.setdefault(t["id"], {})[due[t["id"]]] = {**_metrics_of(t), "at": now.isoformat()}
        store.write_json("metrics.json", metrics)

    followers = store.read_jsonl("followers.jsonl")
    today = now.astimezone(JST).strftime("%Y-%m-%d")
    if not followers or followers[-1]["date"] != today:
        try:
            pm = xapi.me().get("public_metrics", {})
            store.append_jsonl("followers.jsonl", {"date": today, "followers": pm.get("followers_count"),
                                                    "following": pm.get("following_count"), "posts": pm.get("tweet_count")})
        except xapi.XPostError as e:
            log.warning("フォロワー数の取得に失敗: %s", e)
    return len(due)
