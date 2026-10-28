"""noteの解説記事を自動で作る（公開は手動）。

記事と図解は data/note_articles/<ID>/ に保存し、LINEに「記事ができました」と知らせる。
LINEのリンク先（Cloudflare Worker）で、タイトルと本文をワンタップでコピーできる。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import markdown as md

from . import charts, checks, improve, collect_hyperliquid, collect_stocks, learn, line, llm, research, store, traders
from .common import JST, env, log, now_utc, settings

CHART = re.compile(r"!\[([^\]]*)\]\((chart:[^)\s]+)\)")
FOOTER = (
    "\n\n---\n\n"
    "速報はXで毎日発信しています。フォローしていただけると、新しい分析をお届けできます。\n"
    "https://x.com/satoshikun_666\n\n"
    "※上位トレーダーの数字は、公開データから算出した過去の成績です。今後も同じ結果になるとは限りません。\n"
    "※この記事は情報共有を目的としたもので、特定の銘柄の売買を勧めるものではありません。"
    "数字は公開データにもとづいていますが、正確性を保証するものではありません。投資は必ずご自身の判断で行ってください。\n"
)

THEMES = {
    2: "週央レポート: Hyperliquidの上位トレーダーがいま何を持っているか、資金調達率・建玉の偏りとあわせて読み解く",
    5: "週末考察: 今週の資金の流れ（上位トレーダー・HIP-3株式先物・米国株）と、来週見ておきたいポイント",
}


def _week_change_facts() -> list[dict]:
    """上位トレーダーの保有の、約1週間前からの変化。"""
    now = now_utc()
    cur = store.read_json("traders/latest.json")
    old = None
    for h in range(7 * 24, 7 * 24 + 24):
        old = store.read_json(f"traders/{(now - timedelta(hours=h)).astimezone(JST):%Y%m%d-%H}.json")
        if old:
            break
    if not cur or not old:
        return []
    before = {c["coin"]: c for c in old["coins"]}
    out = []
    for c in cur["coins"][:6]:
        b = before.get(c["coin"])
        if not b:
            continue
        out.append({
            "key": f"traders:{c['coin']}:week", "kind": "hl", "category": "smart_money_week", "refresh": None,
            "confidence": "confirmed", "asof": cur["at"],
            "summary": f"上位トレーダーの{c['coin']}保有の1週間の変化: 買い{b['long_n']}→{c['long_n']}人、売り{b['short_n']}→{c['short_n']}人",
            "fields": {"coin": c["coin"].split(":")[-1], "long_before": f"{b['long_n']}人", "long_now": f"{c['long_n']}人",
                       "short_before": f"{b['short_n']}人", "short_now": f"{c['short_n']}人"},
            "raw": {}, "source": {"name": "Hyperliquid公開ランキング・ポジション", "url": ""},
        })
    return out


def collect_facts() -> list[dict]:
    raw = collect_hyperliquid.collect() + traders.facts(max_age_hours=6) + _week_change_facts()
    if env("FINNHUB_API_KEY", required=False):
        raw += [f for f in collect_stocks.collect() if f["category"] in ("quote", "big_move", "earnings_upcoming")]
    for i, f in enumerate(raw, 1):
        f["id"] = f"f{i}"
    return raw


def _render_charts(markdown_text: str, facts: list[dict]) -> dict[str, bytes]:
    images: dict[str, bytes] = {}
    for _, ref in CHART.findall(markdown_text):
        if ref in images:
            continue
        try:
            if ref == "chart:smart_money":
                snap = store.read_json("traders/latest.json")
                if snap:
                    images[ref] = charts.smart_money_chart(snap)
            elif ref == "chart:stocks":
                images[ref] = charts.stock_change_chart(facts)
            elif ref.startswith("chart:hl:"):
                coin = ref.removeprefix("chart:hl:")
                main = next((f for f in facts if (f.get("refresh") or {}).get("coin") == coin), None)
                if main:
                    end = datetime.now(JST)
                    images[ref] = charts.hl_price_chart(main, collect_hyperliquid.candles(coin, end - timedelta(days=7), end))
        except Exception as e:  # noqa: BLE001 図が作れなくても記事は出す
            log.warning("記事の図を作れませんでした %s: %s", ref, e)
    return images


def write(theme: str | None = None) -> dict | None:
    today = now_utc().astimezone(JST)
    if theme is None and list(store.DATA_DIR.glob(f"note_articles/{today:%Y%m%d}-*/meta.json")):
        log.info("今日の記事は作成済みです（予備の起動）")
        return None
    weekday = today.weekday()
    theme = theme or THEMES.get(weekday) or THEMES[5]
    facts = collect_facts()
    by_id = {f["id"]: f for f in facts}
    min_chars, max_chars = 2000, 4500
    system = pipeline_playbook(llm.prompt("note_article")
                               .replace("{min_chars}", str(min_chars)).replace("{max_chars}", str(max_chars)))
    rows = [{"id": f["id"], "category": f["category"], "summary": f["summary"], "fields": f["fields"],
             "refresh": f.get("refresh")} for f in facts]
    user = (f"# 今回のテーマ\n{theme}\n\n# 直近の定点観測（流れの把握用。数字は使わない）\n{research.watch_summary(72)}\n\n"
            f"# 材料（この差し込み記号だけ使える）\n{json.dumps(rows, ensure_ascii=False)}")

    errors: list[str] = []
    for attempt in range(3):
        prompt = user if not errors else user + "\n\n# 前回の原稿で直すべき点\n- " + "\n- ".join(errors)
        out = llm.generate_json(system, prompt)
        title_t, body_t = checks.relink(out.get("title", ""), by_id), checks.relink(out.get("markdown", ""), by_id)
        errors = checks.check_template(title_t + "\n" + body_t, by_id)
        if errors:
            continue
        title = checks.render(title_t, by_id)
        body = checks.render(body_t, by_id)
        if len(body) < 1500:
            errors = [f"本文が短すぎる（{len(body)}字）。各大見出しの下に400〜700字書き、全体で{min_chars}字以上にする"]
            continue
        bad = [p for p in checks.read_list("banned_phrases.txt") if p in title + body]
        if bad or checks.URL.search(body) or checks.HASHTAG.search(body):
            errors = [f"禁止表現・URL・ハッシュタグを含む: {bad}"]
            continue
        break
    else:
        store.append_jsonl("rejected_variants.jsonl", {"at": now_utc().isoformat(), "slot": "note", "format": "note",
                                                      "style": "note", "errors": errors, "length": 0, "text": ""})
        log.warning("note記事を作れませんでした: %s", errors)
        return None

    images = _render_charts(body, facts)
    article_id = now_utc().astimezone(JST).strftime("%Y%m%d-%H%M")
    # 画像はnoteに手動で入れるので、本文には「ここに画像」の目印を残す
    n = 0

    def marker(m: re.Match) -> str:
        nonlocal n
        if m.group(2) not in images:
            return ""
        n += 1
        return f"\n**▼ここに画像{n}（{m.group(1)}）を挿入**\n"
    body_marked = CHART.sub(marker, body) + FOOTER
    order = [ref for _, ref in CHART.findall(body) if ref in images]
    order = list(dict.fromkeys(order))
    for i, ref in enumerate(order, 1):
        store.write_bytes(f"note_articles/{article_id}/image{i}.png", images[ref])
    meta = {
        "id": article_id, "created_at": now_utc().isoformat(), "theme": theme, "title": title,
        "hashtags": [h.lstrip("#") for h in out.get("hashtags", [])][:5],
        "images": [{"file": f"image{i}.png", "label": next(lbl for lbl, r in CHART.findall(body) if r == ref)}
                   for i, ref in enumerate(order, 1)],
        "chars": len(body), "status": "ready",
    }
    store.write_text(f"note_articles/{article_id}/article.md", f"# {title}\n\n{body_marked}")
    store.write_text(f"note_articles/{article_id}/article.html",
                     md.markdown(body_marked, extensions=["sane_lists"]))
    store.write_json(f"note_articles/{article_id}/meta.json", meta)
    notify(meta)
    return meta


def pipeline_playbook(prompt: str) -> str:
    notes = learn.playbook_text() + "\n" + improve.note_strategy_text()
    notes = notes.strip()
    return prompt + ("\n\n# 運用ノート（Xの反応から学んだこと。記事にも応用する）\n" + notes if notes else "")


def viewer_url(article_id: str) -> str:
    base = settings().get("worker_url", "").rstrip("/")
    return f"{base}/note/{article_id}?k={env('LINE_ADMIN_USER_ID', required=False)}"


def notify(meta: dict) -> None:
    line.notify(
        f"📝 noteの記事ができました（{meta['chars']}字・図{len(meta['images'])}枚）\n\n"
        f"「{meta['title']}」\n\n"
        f"↓ここからタイトルと本文をコピーして、noteに貼り付けて公開してください\n{viewer_url(meta['id'])}"
    )
