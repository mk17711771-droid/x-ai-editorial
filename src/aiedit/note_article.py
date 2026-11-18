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
    2: "週央レポート: 今週ここまででいちばん話題になった出来事（速報・決算・急変）の裏側で、Hyperliquidの上位トレーダー・資金調達率・建玉にどんな歪みが出ているか",
    5: "週末考察: 今週いちばん話題になった出来事を、資金の流れ（上位トレーダー・HIP-3株式先物・米国株）の数字で振り返り、来週見ておきたいポイントにつなげる",
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


def _week_news_facts() -> list[dict]:
    """直近1週間の速報（裏取り済み）。noteはその週いちばん話題になった出来事を主役にする（人気記事はその週の出来事が題材）。"""
    out = []
    for f in store.read_json("news_facts.json", []) or []:
        if f.get("status") in ("skipped", "deleted") or now_utc() - datetime.fromisoformat(f["asof"]) > timedelta(days=7):
            continue
        out.append({**f, "category": "news", "fields": {k: v for k, v in f["fields"].items() if isinstance(v, str)}})
    return out[-6:]


def collect_facts() -> list[dict]:
    raw = _week_news_facts() + collect_hyperliquid.collect() + traders.facts(max_age_hours=6) + _week_change_facts()
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
        # 記事は公開前に人が読むので、項目名の誤りだけで全体を捨てず、その文を外して使う
        body_t, dropped = checks.repair_fields(body_t, by_id)
        if dropped:
            log.info("存在しない項目を使った文を外しました: %s", dropped)
        body_t, unsafe = checks.drop_unsafe_sentences(body_t, by_id)
        if unsafe:
            log.info("データ以外の数字などを含む文を外しました（%d文）: %s", len(unsafe), unsafe[:5])
        if attempt < 2 and len(dropped) + len(unsafe) >= 4:
            # 外した文が多いと、前後の文が宙に浮く（11/18「これらはロングの…」の「これら」が消えた文を指していた）
            errors = [f"差し込み記号の誤り・データ以外の数字で外した文が多い（{len(dropped) + len(unsafe)}文）。"
                      "数字は材料の差し込み記号だけで書き、材料にない項目を使わない"]
            continue
        if len(unsafe) > 15:
            errors = [f"データ以外の数字を書いた文が多すぎる（{len(unsafe)}文）。数字は必ず差し込み記号で書く"]
            continue
        errors = checks.check_template(title_t + "\n" + body_t, by_id)
        if errors:
            continue
        title = checks.render(title_t, by_id)
        body = checks.render(body_t, by_id)
        dup = checks.REPEAT_PHRASE.search(title) or checks.NEAR_DUP.search(title)
        if dup:  # 11/4「バイナンスでバイナンスで」（差し込む値と地の文で語が重なった）
            errors = [f"タイトルで同じ語句「{dup.group(1)}」が重なっている。差し込む値の中身を見て、重ならないように書く"]
            continue
        if len(body) < 1500:
            errors = [f"本文が短すぎる（{len(body)}字）。各大見出しの下に400〜700字書き、全体で{min_chars}字以上にする"]
            continue
        refs = {ref for _, ref in CHART.findall(body)}
        if len(refs) < 2:
            # 図は違うものを2枚以上（11/11 同じ「上位トレーダーの保有」を2回指定して画像が1枚だけになった）
            errors = [f"図が{len(refs)}種類しかない。chart: の指定は違う種類の図を2〜3枚（同じ図を2回使わない）"]
            continue
        bad = [p for p in checks.read_list("banned_phrases.txt") if p in title + body]
        if bad or checks.URL.search(body) or checks.HASHTAG.search(body):
            errors = [f"禁止表現・URL・ハッシュタグを含む: {bad}"]
            continue
        images = _render_charts(body, facts)
        missing = [r for r in refs if r not in images]
        if len(images) < 2 and attempt < 2:
            # 指定はあっても図が作れないことがある（11/18 chart:hl の材料が見つからず画像1枚、本文に「上のチャートが示す通り」が残った）
            errors = [f"図 {missing} が作れなかった。chart:stocks・chart:smart_money・材料にある銘柄の chart:hl:<coin> から2〜3枚選ぶ"]
            continue
        break
    else:
        store.append_jsonl("rejected_variants.jsonl", {"at": now_utc().isoformat(), "slot": "note", "format": "note",
                                                      "style": "note", "errors": errors, "length": 0, "text": ""})
        log.warning("note記事を作れませんでした: %s", errors)
        return None

    article_id = now_utc().astimezone(JST).strftime("%Y%m%d-%H%M")
    # 画像はnoteに手動で入れるので、本文には「ここに画像」の目印を残す
    n = 0

    seen: set[str] = set()

    def marker(m: re.Match) -> str:
        nonlocal n
        if m.group(2) not in images or m.group(2) in seen:
            return ""  # 同じ図の2回目は目印を出さない（11/11 画像1枚なのに「画像2」の目印が残った）
        seen.add(m.group(2))
        n += 1
        return f"\n**▼ここに画像{n}（{m.group(1)}）を挿入**\n"
    body_marked = drop_dangling_chart_refs(CHART.sub(marker, body)) + FOOTER
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
    # 文を外したあとに句点が重なる（11/13 公開後の確認で「。。」を指摘された）
    body_marked = re.sub(r"。{2,}", "。", body_marked)
    store.write_text(f"note_articles/{article_id}/article.md", f"# {title}\n\n{body_marked}")
    store.write_text(f"note_articles/{article_id}/article.html",
                     md.markdown(body_marked, extensions=["sane_lists"]))
    store.write_json(f"note_articles/{article_id}/meta.json", meta)
    notify(meta)
    return meta


def drop_dangling_chart_refs(text: str) -> str:
    """直前に画像の目印がないのに「上のチャートが示す通り」と書いた部分を外す（11/18 図が作れず、存在しない図を指していた）。"""
    out, last = [], -10**9
    for para in text.split("\n"):
        if "▼ここに画像" in para:
            last = len(out)
        elif len(out) - last > 6:
            para = re.sub(r"(上|下)の(チャート|図|グラフ)(が示す(通り|とおり)|の(通り|とおり)|を見ると|から(わかる|分かる)ように)、?", "", para)
        out.append(para)
    return "\n".join(out)


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
        f"↓ここからタイトルと本文をコピーして、noteに貼り付けて公開してください\n{viewer_url(meta['id'])}",
        essential=True,
    )
