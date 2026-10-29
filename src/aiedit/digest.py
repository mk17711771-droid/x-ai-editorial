"""夜のまとめ（1日1通）: 今日の投稿の一覧と、昨日の投稿の24時間後の反応。LINEの無料枠を節約するため、投稿ごとの通知の代わりに送る。"""
from __future__ import annotations

from datetime import timedelta

from . import line, store
from .common import JST, now_utc


def run() -> str:
    today = now_utc().astimezone(JST)
    posts = store.posts()
    todays = [p for p in posts if p["posted_at_jst"].startswith(today.strftime("%Y-%m-%d"))]
    y = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    metrics = store.read_json("metrics.json", {})
    yest = [(p, metrics.get(p["tweet_id"], {}).get("24h")) for p in posts if p["posted_at_jst"].startswith(y)]
    lines = [f"🌙 今日のまとめ（{today:%m/%d}）", f"\n■ 今日の投稿 {len(todays)}件"]
    for p in todays:
        lines.append(f"・{p['posted_at_jst'][11:]} {p['text'].splitlines()[0][:34]}…\n  https://x.com/i/status/{p['tweet_id']}")
    measured = [(p, m) for p, m in yest if m]
    if measured:
        lines.append(f"\n■ 昨日の投稿の反応（24時間後）")
        for p, m in sorted(measured, key=lambda t: t[1]["impressions"], reverse=True):
            lines.append(f"・表示{m['impressions']} いいね{m['likes']} 保存{m['bookmarks']}｜{p['text'].splitlines()[0][:26]}…")
    f = store.read_jsonl("followers.jsonl")[-2:]
    if len(f) == 2 and f[-1].get("followers") is not None and f[0].get("followers") is not None:
        lines.append(f"\nフォロワー {f[-1]['followers']}人（前日比{f[-1]['followers'] - f[0]['followers']:+d}）")
    text = "\n".join(lines)
    line.notify(text)
    return text
