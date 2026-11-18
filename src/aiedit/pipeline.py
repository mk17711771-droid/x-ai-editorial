"""編集部の処理の流れ: 収集 → 編集長（選定・採点）→ 制作（原稿）→ チェック → 承認依頼 / 投稿。"""
from __future__ import annotations

import json
import re
import os
from datetime import datetime, timedelta

import requests

from . import checks, collect_hyperliquid, collect_stocks, learn, line, llm, research, sources, store, traders, xapi
from .common import JST, log, now_jst, now_utc, settings


class Halted(Exception):
    pass


def ensure_running() -> None:
    if os.environ.get("BOT_ENABLED", "true").lower() == "false":
        raise Halted("停止スイッチ（BOT_ENABLED=false）が有効です")
    s = store.state()
    if s.get("halted"):
        raise Halted(f"異常検知により停止中: {s.get('halt_reason')}")


# ---------------------------------------------------------------- 投稿枠

def pick_slot(slot_id: str = "auto") -> dict | None:
    cfg = settings()
    now = now_jst()
    if slot_id != "auto":
        for s in cfg["slots"]:
            if s["id"] == slot_id:
                return s
        raise ValueError(f"枠 {slot_id} が設定にありません")
    best = None
    for s in cfg["slots"]:
        if s["days"] == "manual":
            continue  # 速報用の枠は手動で起動したときだけ
        start = slot_base(s, now)
        weekend = start.weekday() >= 5  # 日付をまたいだ深夜の枠は、前日の曜日で判定する
        if s["days"] == "weekday" and weekend or s["days"] == "weekend" and not weekend:
            continue
        if start - timedelta(minutes=cfg.get("slot_early_minutes", 10)) <= now <= start + timedelta(minutes=cfg["slot_grace_minutes"]):
            if best is None or start > best[0]:
                best = (start, s)
    return best[1] if best else None


def slot_format(slot: dict) -> str:
    """長文・まとめは X Premium のときだけ。それ以外は短文にする。"""
    fmt = slot.get("format", "short")
    return fmt if fmt in ("long", "summary") and settings()["account"]["premium"] else "short"


def slot_base(slot: dict, now: datetime | None = None) -> datetime:
    """枠の時刻（日本時間）。0時を過ぎてから深夜の枠を処理するときは、前日の枠として扱う。"""
    now = now or now_jst()
    h, m = map(int, slot["time"].split(":"))
    base = now.replace(hour=h, minute=m, second=0, microsecond=0)
    cfg = settings()
    if now < base - timedelta(minutes=cfg.get("slot_early_minutes", 10)) and \
            now <= base - timedelta(days=1) + timedelta(minutes=cfg["slot_grace_minutes"]):
        return base - timedelta(days=1)
    return base


def slot_key(slot: dict) -> str:
    if slot["id"] in ("news", "newslong"):
        return f"{now_jst():%Y%m%d}-{slot['id']}{now_jst():%H%M}"  # 速報は1日に複数あり得る
    return f"{slot_base(slot):%Y%m%d}-{slot['id']}"


# ---------------------------------------------------------------- 生成

def recent_news_post(minutes: int) -> bool:
    return any(str(p.get("slot_key", "")).split("-")[-1].startswith("news")
               for p in store.recent_posts(minutes / 60))


NEWS_FRESH_HOURS = 3


def _story_first_seen(key: str) -> datetime:
    """同じ話題（news:xxx, news:xxx-2 …）のうち、最初に材料にした時刻。"""
    base = re.sub(r"-\d+$", "", key)
    times = []
    for f in store.read_json("news_facts.json", []) or []:
        if re.sub(r"-\d+$", "", f["key"]) != base:
            continue
        for k in ("asof", "first_reported_at"):  # ネットに最初に出た時刻があればそれを使う（速報＝いち早くキャッチした情報）
            try:
                t = datetime.fromisoformat(str(f.get(k, "")).replace("Z", "+00:00"))
                if t.tzinfo:
                    times.append(t)
            except ValueError:
                pass
    stories = store.read_json("news/stories.json", {}) or {}
    st = stories.get(base.removeprefix("news:"))
    if st and st.get("at"):
        times.append(datetime.fromisoformat(st["at"]))
    return min(times) if times else now_utc()


# 投稿済みの速報を通常の枠の材料に使うのは、最初の報道から12時間まで（11/7 21:07 の週末長文が、
# 11/6朝に速報で出した「優先手数料の過去最高」の説明文をそのまま「いま起きてること」に書いた反省）。週の振り返りは除く
POSTED_NEWS_REUSE_HOURS = 12


def news_facts(pending_only: bool = False, weekly: bool = False) -> list[dict]:
    """確認済みの速報（公式発表・複数の報道で裏付けたもの）。data/news_facts.json に期限つきで置く。"""
    now = now_utc()
    out = []
    for f in store.read_json("news_facts.json", []) or []:
        if pending_only and f.get("status", "pending") != "pending":  # 投稿済み・見送り（skipped）は使わない（10/30 見送りにしたICEの材料で投稿した反省）
            continue  # 速報枠では、まだ投稿していない速報だけを主役にする
        if pending_only and now - _story_first_seen(f["key"]) > timedelta(hours=NEWS_FRESH_HOURS):
            continue  # 最初の報道から時間がたった話題は「速報」として出さない（10/31 1:11 前日のバイナンスの件を【速報】で出した反省）
        if f.get("status") in ("deleted", "skipped"):
            continue  # 削除・見送りにした速報は、通常の枠でも材料にしない（11/10 23:24 削除したWintermuteの件が通常の短文で再登場）
        if (not pending_only and not weekly and f.get("status") == "posted"
                and now - _story_first_seen(f["key"]) > timedelta(hours=POSTED_NEWS_REUSE_HOURS)):
            continue
        if datetime.fromisoformat(f["expires_at"]) > now:
            out.append({**f, "kind": f.get("kind", "news"), "category": "breaking_news", "refresh": None,
                        "confidence": "confirmed"})
    return out


def collect(kind: str, weekly: bool = False, pending_news: bool = False) -> list[dict]:
    raw = collect_stocks.collect() if kind == "stock" else collect_hyperliquid.collect() + traders.facts()
    raw = news_facts(pending_only=pending_news, weekly=weekly) + raw  # 確認済みの速報（あれば最優先の材料）
    if weekly:
        # 週の振り返り: 1週間の値動き（Hyperliquidの主要銘柄・HIP-3と、記録した米国株の終値）
        try:
            raw = collect_hyperliquid.week_changes() + collect_stocks.week_change_facts() + raw
        except requests.RequestException as e:
            log.warning("1週間の値動きを取得できません: %s", e)
    # 株の枠でもHIP-3の株式先物は材料にする（株とHyperliquidをつなぐ視点）
    if kind == "stock" and settings()["hyperliquid"].get("include_hip3"):
        try:
            raw += [f for f in collect_hyperliquid.collect() if f["category"] == "hip3_top"]
        except requests.RequestException as e:
            log.warning("HIP-3データの取得に失敗: %s", e)
    facts = []
    for i, f in enumerate(raw, 1):
        f["id"] = f"f{i}"
        facts.append(f)
    return facts


def _facts_for_llm(facts: list[dict], with_fields: bool) -> str:
    rows = []
    for f in facts:
        row = {"id": f["id"], "key": f["key"], "category": f["category"], "confidence": f["confidence"], "summary": f["summary"]}
        if with_fields:
            row["fields"] = f["fields"]
        rows.append(row)
    return json.dumps(rows, ensure_ascii=False)


def with_playbook(prompt: str) -> str:
    """改善AIが成果データから作った運用ノートを、毎回プロンプトに添える。"""
    notes = learn.playbook_text()
    if not notes:
        return prompt
    return (prompt + "\n\n# 運用ノート（実際の投稿の反応から学んだこと。上のルールと矛盾する場合は上のルールを優先）\n"
            + notes)


ANGLE_NAMES = {"hl_flow": "Hyperliquid/HIP-3の資金調達率・建玉の偏り", "move": "急な値動き",
               "traders": "上位トレーダーの集計", "stock": "米国株の値動き・決算", "summary": "まとめ・振り返り"}


def angle_family(topic_key: str) -> str:
    """投稿の切り口の系統。同じ系統が続くと単調になるので、続けて出さないために使う。"""
    t = str(topic_key)
    if t.startswith("traders"):
        return "traders"
    if t.startswith("stock:"):
        return "stock"
    last = t.rsplit(":", 1)[-1]
    if last in ("funding_extreme", "hip3_top", "oi_change", "volume_top", "overview"):
        return "hl_flow"
    if last in ("big_move",):
        return "move"
    if last in ("week_change",):
        return "summary"
    return "other"


def run_editor(slot: dict, facts: list[dict], exclude: set[str] | None = None, must: str = "") -> list[dict]:
    recent = [p.get("topic_key") for p in store.recent_posts(72)]
    form = {
        "long": "長文の考察投稿。複数の材料（目安3〜8個）を組み合わせて、背景と「歪み」を深く解説できる切り口を選ぶ。",
        "summary": ("朝のまとめ投稿。米国株の指数（SPY・QQQ・SOXX）→ 目立って上げた銘柄・下げた銘柄（明暗）→ 今日の決算予定を、"
                    "材料4〜8個で一覧にする。最後にHyperliquidの材料を1つだけ添えてよい。fact_ids に使う材料をすべて入れる。"),
    }.get(slot_format(slot), "短文の投稿。1つの異変を一言で伝えられる切り口を選ぶ。")
    user = (f"# 今回の投稿枠\n{slot['time']} {slot['theme']}\n\n# 投稿の形式\n{form}\n\n"
            f"# 直近72時間の投稿トピック (recent_topics)\n{json.dumps(recent, ensure_ascii=False)}\n\n"
            f"# 材料 (facts)\n{_facts_for_llm(facts, with_fields=False)}")
    recent_tags = sorted({t for p in store.recent_posts(settings()["limits"].get("same_symbol_cooldown_hours", 18))
                          for t in checks.CASHTAG.findall(p["text"])})
    if recent_tags:
        user += ("\n\n# 直近に主役にした銘柄（続けて同じ銘柄を主役にしない。別の銘柄・別の切り口を選ぶ）\n"
                 + json.dumps(recent_tags, ensure_ascii=False))
    if must:
        user += f"\n\n# 必ず守ること\n{must}"
    if any(f.get("category") == "breaking_news" for f in facts):
        user += ("\n\n# 確認済みの速報あり（category: breaking_news）。このアカウントのテーマ（Hyperliquid・xyz）に直結するなら最優先で検討し、"
                 "速報の事実（何が起きたか）＋このアカウントの実データ（価格・建玉・資金調達率）を組み合わせる。未確定の部分（規制当局の承認待ちなど）は断定しない")
    recent_angles = [angle_family(p.get("topic_key", "")) for p in store.recent_posts(36)][-2:]
    if len(recent_angles) == 2 and recent_angles[0] == recent_angles[1]:
        user += (f"\n\n# 直近2件の切り口が同じ（{ANGLE_NAMES.get(recent_angles[0], recent_angles[0])}）。"
                 "今回は別の切り口の候補を上位にする")
    top = [n for n in checks.read_list("top_names.txt")]
    user += ("\n\n# 主役に最優先する名前（日本の個人投資家にいちばん通じる。24時間表示の中央値は約300で、ほかの銘柄の約3倍）。"
             "材料にあれば、この中の1つを主役にした候補を必ず1件以上出す\n" + "・".join(top))
    big = [n for n in checks.read_list("big_names.txt")]
    user += ("\n\n# 主役に優先する「大きな名前」（読者に通じる銘柄・指数。材料にあればこの中から主役を選ぶ。"
             "馴染みの薄い銘柄は、変化が極端なときだけ主役にする）\n" + "・".join(big))
    if slot.get("kind") == "stock":
        user += "\n\n# この枠の主役は米国株（材料の1つ目に米国株の材料を置く）。Hyperliquid/HIP-3の材料は添える程度"
    if exclude:
        user += ("\n\n# 選んではいけない話題（24時間以内に投稿済み。別の材料を主役にした候補を出す）\n"
                 + json.dumps(sorted(t for t in exclude if t), ensure_ascii=False))
    watch = research.watch_summary()
    if watch:
        user += f"\n\n# 直近24時間の定点観測（流れの把握用。投稿の数字には使わない）\n{watch}"
    news = sources.news_digest()
    if news:
        user += f"\n\n# いま話題のニュース見出し（切り口の参考。見出しの数字・事実は投稿に使わない）\n{news}"
    hot = sources.hot_digest()
    if hot:
        user += ("\n\n# 日本でいま検索が急増している言葉（Googleトレンド）。アカウントの範囲内の話題（暗号資産・米国株・半導体・取引所の事件など）が"
                 "あれば旬として切り口に使う。範囲外は無視する。数字・事実は投稿に使わない\n" + hot)
    out = llm.generate_json(with_playbook(llm.prompt("editor")), user)
    ids = {f["id"] for f in facts}
    cands = []
    for c in out.get("candidates", []):
        c["fact_ids"] = [i for i in c.get("fact_ids", []) if i in ids]
        if not c["fact_ids"]:
            continue
        # 合計点はAIの申告ではなく内訳から再計算する
        sc = c.get("scores", {})
        for k in ("novelty", "interest", "evidence", "utility", "insight"):
            sc[k] = max(0, min(20, int(sc.get(k, 0))))
        c["scores"] = sc
        c["total"] = sum(sc.values())
        cands.append(c)
    return sorted(cands, key=lambda c: c["total"], reverse=True)


def rank_candidates(cands: list[dict], facts_by_id: dict[str, dict], slot: dict, repeat: str | None = None) -> list[dict]:
    """書く順番: 直近と同じ切り口は後回し → 米国株の枠は米国株が主役 → 主役の通じやすさ（top_names > big_names > その他）→ 点数。
    11/18: 大きな名前の並べ替えを、このあと点数順で並べ直して消していた（$CXMT・$KIOXIA が主役になった）。
    10/27〜11/17 の24時間表示の中央値: $QQQ 301・$SOXL 300・ナスダック100 180、ほかの大きな名前 約100、馴染みの薄い銘柄 約80。"""
    top = {n.upper() for n in checks.read_list("top_names.txt")}
    big = {n.upper() for n in checks.read_list("big_names.txt")}

    def priority(c: dict) -> tuple:
        first = facts_by_id.get((c.get("fact_ids") or [""])[0], {})
        stock_led = slot["kind"] != "stock" or first.get("kind") == "stock"
        sym = _cand_symbol(c, facts_by_id).upper()
        tier = 2 if sym in top else 1 if sym in big else 0
        return (angle_family(c.get("topic_key", "")) == repeat, not stock_led, -tier, -c["total"])
    return sorted(cands, key=priority)


def _cand_symbol(cand: dict, facts_by_id: dict[str, dict]) -> str:
    """候補の主役の銘柄（1つ目の材料の銘柄）。"""
    for fid in cand.get("fact_ids", [])[:1]:
        f = facts_by_id.get(fid) or {}
        sym = (f.get("fields") or {}).get("symbol") or (f.get("fields") or {}).get("coin") or ""
        return str(sym).split(":")[-1].lstrip("$")
    return ""


def notify_credits_depleted() -> None:
    st = store.state()
    today = now_jst().strftime("%Y-%m-%d")
    if st.get("credits_notified") == today:
        return
    line.notify("【要対応】X APIの利用クレジットが尽きたため、Xへの投稿・削除ができなくなっています（402 credits depleted）。\n"
                "X Developer Portal（developer.x.com）→ 該当アプリの Billing / Credits からクレジットを追加してください。"
                "追加後は自動で投稿が再開します（操作は不要です）。", essential=True)
    st["credits_notified"] = today
    store.save_state(st)


def us_close_stale(now: datetime | None = None) -> bool:
    """記録した米国株の最新の終値が、米国の日付で2日以上前か（週明けの月曜の取引開始前・祝日明け）。"""
    from zoneinfo import ZoneInfo
    closes = store.read_json("stock_closes.json", {}) or {}
    days = [d for by_day in closes.values() for d in by_day]
    if not days:
        return False
    last = datetime.strptime(max(days), "%Y-%m-%d").date()
    today_ny = (now or now_utc()).astimezone(ZoneInfo("America/New_York")).date()
    return (today_ny - last).days >= 2


def run_writer(slot: dict, cand: dict, facts_by_id: dict[str, dict]) -> dict | None:
    """原稿を3案作らせ、チェックを通過した中で最良の1案を返す。"""
    fmt = slot_format(slot)
    min_chars, max_chars, _ = checks.length_limits(fmt)
    system = with_playbook(llm.prompt("writer_long" if fmt == "long" else "writer")
                           .replace("{min_chars}", str(min_chars)).replace("{max_chars}", str(max_chars)))
    used = [facts_by_id[i] for i in cand["fact_ids"]]
    shape = ("\n\n# 形式（朝のまとめ）\n1行目に「今朝の米国株」の一言要約。続けて「・」の箇条書きで指数→上げた銘柄→下げた銘柄（明暗）→今日の予定を並べ、"
             "最後に1〜2行で自分の見立て。箇条書きの行は短く（銘柄名と数字だけでよい）。" if fmt == "summary" else "")
    user = (f"# 投稿枠\n{slot['theme']}{shape}\n\n# 編集長の切り口\n{cand.get('angle', '')}\n\n"
            f"# 材料（この差し込み記号だけ使える）\n{_facts_for_llm(used, with_fields=True)}\n\n"
            f"# 直近の投稿 (recent_posts)\n{json.dumps(list(store.recent_texts(10)), ensure_ascii=False)}")
    # 週明けの朝など、米国株の数字が2日以上前の終値のときは先に伝える（11/9 月曜の朝の長文が全案破棄・1本は誤投稿）
    stale = checks._stale_close_facts("".join(f"{{{{{f['id']}.x}}}}" for f in used), {f["id"]: f for f in used})
    if stale:
        user += ("\n\n# 注意（米国株の時点）\n" + "・".join(sorted(stale)) + " の数字は2日以上前（前営業日）の終値。"
                 "必ず {{fN.asof}} で時点（例: 米国11/6(金)終値）を書き、「今日」「今」の動きとして書かない。"
                 "Hyperliquidの24時間騰落率とは並べて対比しない（資金調達率・建玉の偏りとの比較はよい）。")
    topic_key = cand.get("topic_key") or used[0]["key"]
    feedback: list[str] = []
    for attempt in range(2):
        prompt = user if not feedback else (user + "\n\n# 前回の案が破棄された理由（直して書き直す）\n- "
                                            + "\n- ".join(sorted(set(feedback))[:6]))
        w = _write_once(slot, system, prompt, used, facts_by_id, topic_key, fmt, feedback)
        if w:
            return w
    return None


def _write_once(slot: dict, system: str, user: str, used: list[dict], facts_by_id: dict[str, dict],
                topic_key: str, fmt: str, feedback: list[str]) -> dict | None:
    out = llm.generate_json(system, user)
    variants = out.get("variants", [])
    best = out.get("best", 0)
    order = ([best] if isinstance(best, int) and 0 <= best < len(variants) else []) + list(range(len(variants)))
    tried = set()
    for idx in order:
        if idx in tried:
            continue
        tried.add(idx)
        v = variants[idx]
        # 切り口の材料以外でも、同じ枠で集めた実データの材料なら使ってよい（数字の根拠は変わらない）
        usable = {f["id"]: f for f in used}
        for fid, _ in checks.PLACEHOLDER.findall(v.get("template", "")):
            if fid in facts_by_id:
                usable[fid] = facts_by_id[fid]
        tmpl = checks.relink(v.get("template", ""), usable)
        v["template"] = tmpl
        errors = checks.check_template(tmpl, usable)
        text = checks.render(tmpl, usable) if not errors else tmpl
        if not errors:
            before = text
            text = checks.fit_length(text, fmt)  # 少しだけ長い短文は、最後の段落・文を落として収める（11/3 文字数超過で枠を落とした）
            if len(text) < len(before) * 0.6:
                # 半分近く削ると、時点など大事な部分が落ちる（11/9 12:18 長文の形の原稿が1行目だけになり、金曜の終値の時点が消えた）
                errors = [f"短文の上限に対して長すぎる（{len(before)}字）。短文の形（2〜3段落）で書き直す"]
            for sym, fid in checks._stale_close_facts(tmpl, usable).items():
                asof = str(usable[fid]["fields"].get("asof", ""))
                if asof and asof not in text:
                    errors.append(f"{sym}は2日以上前の終値なのに、投稿文に時点（{asof}）が残っていない")
        errors = errors or checks.check_text(text, topic_key, fmt)
        if errors:
            log.info("案%d（%s）を破棄: %s", idx, v.get("style"), errors)
            # 破棄理由を残し、プロンプト改善に使う
            store.append_jsonl("rejected_variants.jsonl", {
                "at": now_utc().isoformat(), "slot": slot["id"], "format": fmt, "style": v.get("style"),
                "errors": errors, "length": len(text), "text": text[:1500]})
            feedback += errors
            continue
        return {"template": tmpl, "text": text, "style": v.get("style", ""), "topic_key": topic_key,
                "facts": list(usable.values()), "format": fmt}
    return None


def _write_drafts(slot: dict, cands: list[dict], key: str, seq: int, facts_by_id: dict, recent_topics: set,
                  recent_syms: set, tried_syms: set, cfg: dict) -> list[dict]:
    """候補を順に書かせ、チェックを通った下書きを返す。"""
    drafts: list[dict] = []
    for cand in cands:
        if len(drafts) >= cfg["limits"]["max_candidates_per_slot"]:
            break
        if any(d["topic_key"] == cand.get("topic_key") for d in drafts):
            continue
        if cand.get("topic_key") in recent_topics:
            log.info("候補 %s は直近に投稿済みの話題なので飛ばします", cand.get("topic_key"))
            continue
        if slot["id"] != "news" and _cand_symbol(cand, facts_by_id) in recent_syms:
            # 主役の銘柄を直近に出している候補は、書かせる前に飛ばす（11/3 23時 $SKHXの案を何度も書かせて枠を落とした）
            log.info("候補 %s は主役の銘柄を直近に投稿済みなので飛ばします", cand.get("topic_key"))
            continue
        tried_syms.add(_cand_symbol(cand, facts_by_id))
        w = run_writer(slot, cand, facts_by_id)
        if not w:
            continue
        draft = {
            "id": f"{key}-{seq + len(drafts)}",
            "slot_key": key,
            "slot_theme": f"{slot['time']} {slot['theme']}",
            "status": "pending",
            "created_at": now_utc().isoformat(),
            "text": w["text"],
            "template": w["template"],
            "style": w["style"],
            "format": w["format"],
            "topic_key": w["topic_key"],
            "angle": cand.get("angle", ""),
            "scores": cand["scores"],
            "total": cand["total"],
            "fact_ids": cand["fact_ids"],
            "facts": w["facts"],
            "sources": [f["source"] for f in w["facts"]],
            "needs_human": any(f["confidence"] == "headline" for f in w["facts"]),
        }
        store.save_draft(draft)
        drafts.append(draft)
    return drafts


def generate(slot_id: str = "auto", force: bool = False) -> list[dict]:
    ensure_running()
    cfg = settings()
    slot = pick_slot(slot_id)
    if not slot:
        log.info("現在時刻に該当する投稿枠がありません")
        return []
    key = slot_key(slot)
    st = store.state()
    if key in st.get("generated_slots", []) and not force:
        log.info("枠 %s は生成済みです", key)
        return []

    if slot["id"] == "news" and recent_news_post(minutes=120):
        # 速報を短時間に連発しない（同じ出来事の言い換えが続いて単調になる）。速報は未投稿のまま残し、間隔をあけて出す
        log.info("直近2時間以内に速報を出したため、今回は見送ります")
        return []
    if slot["kind"] == "stock" and us_close_stale():
        # 週明けの月曜（米国の取引開始前）・祝日明けは、米国株の材料が2日以上前の終値しかない。
        # 24時間動いているHIP-3の株式先物を主役にする（11/9 月曜は朝・昼・夜の米国株の枠が期間の違う対比で全滅・削除）
        slot = {**slot, "kind": "hl", "theme": slot["theme"] + "。※米国株は前営業日の終値しかないため、24時間動いているHIP-3の株式先物（大きな名前の銘柄）の今の数字を主役にする"}
        log.info("米国株の終値が2日以上前のため、HIP-3の株式先物を主役にします")
    facts = collect(slot["kind"], weekly=bool(slot.get("weekly")), pending_news=slot["id"] == "news")
    if us_close_stale():
        # 週末・祝日明けは米国株の日々の終値が2日以上前。今の24時間の数字と並べると期間の違う対比になるので材料から外す
        # （1週間の値動きは期間を書く前提なので残す。11/15 日曜の週末長文が「11/13の終値と今の24時間の対比」で全案破棄）
        before = len(facts)
        facts = [f for f in facts if not (f.get("kind") == "stock" and f.get("category") != "week_change")]
        if len(facts) != before:
            log.info("米国株の終値が古いため、日々の米国株の材料 %d 件を外しました", before - len(facts))
    store.write_json(f"facts/{key}.json", facts)
    log.info("材料 %d 件を収集", len(facts))
    facts_by_id = {f["id"]: f for f in facts}

    all_cands = run_editor(slot, facts)
    cands = [c for c in all_cands if c["total"] >= cfg["scoring"]["auto_post_min"]]
    log.info("80点以上の候補 %d 件", len(cands))
    store.write_json(f"editor/{key}.json", all_cands)

    # 再生成時は、同じ枠の未承認の下書きを無効にし、IDが重ならないよう続き番号にする
    seq = 1
    while store.load_draft(f"{key}-{seq}"):
        old_draft = store.load_draft(f"{key}-{seq}")
        if old_draft["status"] == "pending":
            _close(old_draft, "superseded")
        seq += 1

    longest = max([cfg["limits"]["same_topic_cooldown_hours"], *cfg["limits"].get("topic_cooldown_overrides", {}).values()])
    recent_topics = set()
    for p in store.recent_posts(longest):
        t = p.get("topic_key") or ""
        if now_utc() - datetime.fromisoformat(p["posted_at"]) < timedelta(hours=checks.topic_cooldown_hours(t)):
            # 旧形式（traders:funnel:日付）も、定番企画の名前で除外する
            recent_topics |= {t, t.rsplit(":", 1)[0] if t.startswith("traders:funnel:") else t}
    recent_syms = {s.lstrip("$") for p in store.recent_posts(cfg["limits"].get("same_symbol_cooldown_hours", 18))
                   for s in checks.CASHTAG.findall(p["text"])}
    stale = lambda c: c.get("topic_key") in recent_topics or (slot["id"] != "news" and _cand_symbol(c, facts_by_id) in recent_syms)  # noqa: E731
    if cands and all(stale(c) for c in cands):
        log.info("候補がすべて直近に投稿済みの話題・銘柄なので、編集をやり直します")
        all_cands = run_editor(slot, facts, exclude=recent_topics,
                               must=f"次の銘柄は主役にしない（直近に投稿済み）: {'・'.join(sorted(recent_syms))}。主役の違う候補を3件以上出す")
        cands = [c for c in all_cands if c["total"] >= cfg["scoring"]["auto_post_min"]]
        store.write_json(f"editor/{key}.json", all_cands)
    # 主役が「大きな名前」（config/big_names.txt）の候補を先に書く（11/8 夜の長文 $GEV は65表示、
    # 11/7 $SOXL 820・11/8 $MU 106。馴染みの薄い銘柄は点数が高くても伸びない）
    big = {n.upper() for n in checks.read_list("big_names.txt")}
    cands.sort(key=lambda c: (_cand_symbol(c, facts_by_id).upper() in big, c["total"]), reverse=True)
    # 切り口が3回続かないように、直近2件と同じ系統の候補は後回しにする
    recent_angles = [angle_family(p.get("topic_key", "")) for p in store.recent_posts(36)][-2:]
    repeat = recent_angles[0] if len(recent_angles) == 2 and recent_angles[0] == recent_angles[1] else None
    # 米国株の枠では、米国株の材料が主役の候補を先にする（Hyperliquidばかりにならないように）
    cands = rank_candidates(cands, facts_by_id, slot, repeat)
    if slot["id"] in ("news", "newslong"):
        # 速報枠は、まだ出していない速報を主役にした候補だけ。編集長が別の話題を選んだら、速報を主役にするよう編集をやり直す
        def news_cands(cs: list[dict]) -> list[dict]:
            out = []
            for c in cs:
                news = [facts_by_id[i] for i in c.get("fact_ids", []) if facts_by_id.get(i, {}).get("category") == "breaking_news"]
                if news:
                    # 解説の長文は速報とは別の話題として扱う（速報を出した直後でも「投稿済みの話題」で止めない）
                    c["topic_key"] = news[0]["key"] + (":long" if slot["id"] == "newslong" else "")
                    out.append(c)
            return out
        picked = news_cands(cands)
        if not picked:
            target = next((f for f in facts if f.get("category") == "breaking_news"), None)
            if target:
                log.info("速報を主役にした候補がないため、編集をやり直します")
                again = run_editor(slot, facts, must=(f"速報「{target['fields'].get('headline', target['summary'][:60])}」"
                                                     f"（{target['id']}）を主役にした候補だけを出す。fact_ids の最初に {target['id']} を入れる"))
                picked = news_cands([c for c in again if c["total"] >= cfg["scoring"]["auto_post_min"] - 10])
        cands = picked
    drafts = []
    tried_syms: set[str] = set()
    for _round in range(2):
        if _round == 1:
            if drafts or slot["id"] in ("news", "newslong"):
                break
            # 1巡目の候補が全部だめだったら、主役を変えて編集をやり直す（11/14 10時 候補3件のうち2件が直近の銘柄、
            # 残り1件はSOXLとSOXXの比較で2回とも破棄され、週末の長文の枠を落とした）
            avoid = sorted((recent_syms | tried_syms) - {""})
            log.info("1巡目で原稿ができなかったため、主役を変えて編集をやり直します（除外: %s）", "・".join(avoid))
            again = run_editor(slot, facts, exclude=recent_topics,
                               must=f"次の銘柄は主役にしない: {'・'.join(avoid)}。主役の違う候補を3件以上出す")
            cands = [c for c in again if c["total"] >= cfg["scoring"]["auto_post_min"]]
        skip = recent_syms | (set(tried_syms) if _round else set())  # 2巡目は1巡目で失敗した銘柄も避ける
        drafts = _write_drafts(slot, cands, key, seq, facts_by_id, recent_topics, skip, tried_syms, cfg)

    if os.environ.get("DRY_RUN"):
        # 試し書き: 下書きを保存するだけで、投稿もLINE通知もしない
        for d in drafts:
            _close(d, "dry_run")
        log.info("試し書きのため投稿しません（%d件）", len(drafts))
        return drafts

    st["generated_slots"] = st.get("generated_slots", []) + [key]
    store.save_state(st)

    if not drafts:
        log.info("基準を満たす投稿がないため、この枠は見送ります")
        if slot["id"] in ("news", "newslong"):
            # 速報を「投稿します」と知らせたあとに出せなかったときは、正直に知らせる
            line.notify("⚠️ 速報の投稿を見送りました（基準を満たす原稿ができませんでした）。原因を調べて出し直します。")
        return []

    # 全自動モードでは最高点の案を投稿する。未検証ニュースを含む案だけは人の承認に回す
    auto_picks = [d for d in drafts if not d["needs_human"]] if cfg["mode"] == "auto" else []
    if auto_picks:
        # 1案目が投稿直前の確認で使えなくなったら、次の案を出す（11/6 12時 1案目が「データが古い」で止まり、2案目が残ったまま枠を落とした）
        for d in auto_picks:
            if publish(d["id"], "approve", decided_by="auto") not in ("stale", "rejected"):
                break
    else:
        line.send_approval_request(drafts, cfg["limits"]["draft_expiry_hours"])
    return drafts


# ---------------------------------------------------------------- 投稿

def _recheck(draft: dict) -> list[str]:
    """投稿直前に元データを取り直し、内容が古くなっていないか確認する。"""
    tol = settings()["limits"]["recheck_change_pct_tolerance"]
    problems = []
    for f in draft["facts"]:
        ref = f.get("refresh")
        if not ref or "change_pct" not in f.get("raw", {}):
            continue
        try:
            if ref["type"] == "stock":
                latest = collect_stocks.refresh_change_pct(ref)
            else:
                latest = collect_hyperliquid.refresh_change_pct(ref)
        except requests.RequestException as e:
            problems.append(f"再確認のデータ取得に失敗: {e}")
            continue
        if latest is None:
            problems.append(f"{f['key']} の最新データが取得できない")
        elif abs(latest - f["raw"]["change_pct"]) > tol:
            problems.append(f"{f['fields'].get('symbol') or f['fields'].get('coin')} の騰落率が変化（{f['raw']['change_pct']:+.2f}% → {latest:+.2f}%）")
    return problems


def _still_valid(old: dict, new: dict) -> bool:
    """最新データでも、元の投稿の切り口（上昇/下落、異変かどうか）が成り立つか。"""
    o, n = old["raw"].get("change_pct"), new["raw"].get("change_pct")
    if o is not None and n is not None and (o > 0) != (n > 0):
        return False
    cfg = settings()
    if old["category"] == "big_move":
        threshold = cfg["stocks" if old["kind"] == "stock" else "hyperliquid"]["big_move_pct"]
        return n is not None and abs(n) >= threshold
    if old["category"] == "funding_extreme":
        of, nf = old["raw"]["funding_annual_pct"], new["raw"]["funding_annual_pct"]
        return (of > 0) == (nf > 0) and abs(nf) >= cfg["hyperliquid"]["funding_extreme_annual_pct"]
    return True


MAX_REFRESH = 2


def refresh_draft(draft: dict) -> dict | None:
    """数字が古くなった下書きを、同じ文章の型のまま最新データで作り直す。作り直せなければ None。"""
    if draft.get("refresh_count", 0) >= MAX_REFRESH:
        return None
    facts = []
    for f in draft["facts"]:
        if not f.get("refresh"):
            facts.append(f)
            continue
        try:
            nf = (collect_stocks if f["refresh"]["type"] == "stock" else collect_hyperliquid).refresh_fact(f)
        except requests.RequestException as e:
            # 取得元の回数制限（Finnhub 429）などで作り直せなくても、集めて30分以内の数字ならそのまま使う
            # （11/10 08:05 朝の長文が429で作り直せず、枠ごと見送りになった）
            try:
                age = now_utc() - datetime.fromisoformat(f["asof"])
            except (KeyError, ValueError):
                age = timedelta(hours=99)
            if age <= timedelta(minutes=30):
                log.warning("最新データでの作り直しに失敗（%s）。%d分前の数字のまま使います", e, age.total_seconds() // 60)
                facts.append(f)
                continue
            log.warning("最新データでの作り直しに失敗: %s", e)
            return None
        if not nf or not _still_valid(f, nf):
            return None
        facts.append(nf)
    by_id = {f["id"]: f for f in facts}
    if checks.check_template(draft["template"], by_id):
        return None
    text = checks.render(draft["template"], by_id)
    if checks.check_text(text, draft["topic_key"], draft.get("format", "short")):
        return None
    seq = 1
    while store.load_draft(f"{draft['slot_key']}-{seq}"):
        seq += 1
    new = dict(draft)
    new.update({
        "id": f"{draft['slot_key']}-{seq}",
        "status": "pending",
        "created_at": now_utc().isoformat(),
        "text": text,
        "facts": facts,
        "sources": [f["source"] for f in facts],
        "refreshed_from": draft["id"],
        "refresh_count": draft.get("refresh_count", 0) + 1,
    })
    store.save_draft(new)
    return new


def _attach_chart(draft: dict) -> list[str] | None:
    """チャート画像を作ってアップロードする。失敗しても投稿は止めず、画像なしで投稿する。"""
    if not settings().get("images", {}).get("enabled", True):
        return None
    from .improve import image_prefs
    if not image_prefs().get(draft.get("format", "short"), True):
        return None  # 改善会議で「この形式は画像なし」と決めたとき
    try:
        from . import charts
        png = charts.build_for_draft(draft, store.read_json(f"facts/{draft['slot_key']}.json", []))
        if not png:
            return None
        media_id = xapi.upload_image(png)
        draft["has_image"] = True
        return [media_id]
    except Exception as e:  # noqa: BLE001 画像は付加価値なので、どんな失敗でも本文の投稿を優先する
        log.warning("チャート画像を付けられませんでした: %s", e)
        draft["image_error"] = str(e)[:300]
        return None


OFFICIAL_ACCOUNTS = ("HyperliquidX", "tradexyz")


def _quote_target(draft: dict) -> str | None:
    """速報の元が公式アカウントの投稿なら、その投稿を引用して出す（公式の投稿を見ている人に届く。10/30の分析）。"""
    import re
    for f in draft["facts"]:
        if f.get("category") != "breaking_news":
            continue
        m = re.search(r"(?:x|twitter)\.com/(\w+)/status/(\d+)", (f.get("source") or {}).get("url", ""))
        if m and m.group(1) in OFFICIAL_ACCOUNTS:
            return m.group(2)
    return None


def _mark_news_posted(draft: dict) -> None:
    """速報を投稿したら、同じ速報を二度使わないよう記録する。"""
    keys = {f["key"] for f in draft["facts"] if f.get("category") == "breaking_news"}
    if not keys or draft.get("format") == "long":  # 解説の長文は速報の後に出すので、速報の材料を使い切らない
        return
        return
    facts = store.read_json("news_facts.json", []) or []
    for f in facts:
        if f["key"] in keys:
            f["status"] = "posted"
    store.write_json("news_facts.json", facts)


def _close(draft: dict, status: str, **extra) -> None:
    draft["status"] = status
    draft.update(extra)
    store.save_draft(draft)


STATUS_JA = {
    "posted": "すでに投稿済みの",
    "rejected": "却下済みの",
    "superseded": "新しい投稿案に置き換えられた古い案の",
    "expired": "承認期限が切れた",
    "stale": "データが古くなった",
    "failed": "投稿に失敗した",
    "skipped": "投稿上限で見送られた",
    "deleted": "すでに削除済みの",
}


def publish(draft_id: str, action: str, decided_by: str = "admin") -> str:
    draft = store.load_draft(draft_id)
    if not draft:
        raise ValueError(f"下書き {draft_id} が見つかりません")
    if action == "delete" and draft["status"] == "posted":
        return delete_post(draft)
    if draft["status"] != "pending":
        log.info("下書き %s は処理済みです（%s）", draft_id, draft["status"])
        if decided_by == "admin":
            reason = STATUS_JA.get(draft["status"], draft["status"])
            line.notify(f"この投稿案は{reason}ため、投稿しませんでした。最新の投稿案のボタンを押してください。")
        return draft["status"]

    store.record_decision({"id": draft_id, "action": action, "by": decided_by, "at": now_utc().isoformat(),
                           "total": draft["total"], "scores": draft["scores"], "style": draft["style"],
                           "topic_key": draft["topic_key"], "text": draft["text"]})
    if action != "approve":
        _close(draft, "rejected", rejected_reason=action)
        return "rejected"

    ensure_running()
    cfg = settings()["limits"]

    created = datetime.fromisoformat(draft["created_at"])
    if now_utc() - created > timedelta(hours=cfg["draft_expiry_hours"]):
        _close(draft, "expired")
        line.notify(f"承認期限（{cfg['draft_expiry_hours']}時間）を過ぎたため投稿しませんでした。")
        return "expired"
    # 速報は別枠（news_watch.max_posts_per_day で管理）。通常の上限で止めると熱い話題を逃す（10/30 バイナンスの件）
    is_news = str(draft.get("slot_key", "")).split("-")[-1].startswith("news")
    recent = [p for p in store.recent_posts(24) if not str(p.get("slot_key", "")).split("-")[-1].startswith("news")]
    if not is_news and len(recent) >= cfg["max_posts_per_day"]:
        _close(draft, "skipped", skip_reason="1日の投稿上限")
        line.notify("1日の投稿上限に達したため投稿しませんでした。")
        return "skipped"
    if any(p.get("slot_key") == draft["slot_key"] for p in store.posts()):
        _close(draft, "superseded")
        line.notify("同じ枠ですでに投稿済みのため、この下書きは投稿しませんでした。")
        return "superseded"

    text_problems = checks.check_text(draft["text"], draft["topic_key"], draft.get("format", "short"))
    problems = text_problems + _recheck(draft)
    if problems:
        fresh = None if text_problems else refresh_draft(draft)
        if fresh:
            _close(draft, "stale", problems=problems, refreshed_to=fresh["id"])
            if decided_by == "auto":
                return publish(fresh["id"], "approve", decided_by="auto")
            line.send_approval_request(
                [fresh], cfg["draft_expiry_hours"],
                note="数字が動いていたため、最新のデータで作り直しました。内容を確認して、もう一度承認してください:\n- "
                     + "\n- ".join(problems),
            )
            return "refreshed"
        _close(draft, "stale", problems=problems)
        if decided_by != "auto":  # 自動投稿では次の案に回すので、LINEは使わない（見回りが枠の投稿漏れを見る）
            line.notify("投稿直前の確認で問題が見つかったため投稿を中止しました:\n- " + "\n- ".join(problems)
                        + "\n\n値動きで投稿の切り口が成り立たなくなったため、作り直しも見送りました。")
        return "stale"

    media_ids = _attach_chart(draft)
    try:
        # 公式の投稿の引用は、このAPIでは許可されていない（11/2 403で自動運用が止まった）ので使わない
        tweet_id = xapi.post(draft["text"], media_ids=media_ids)
    except xapi.XAuthError as e:
        store.halt(f"X API認証・権限エラー: {e}")
        _close(draft, "failed", error=str(e))
        line.notify(f"【緊急停止】X APIで認証・権限エラーが発生したため自動運用を停止しました。\n{e}\n\n確認後、LINEで「再開」と送ってください。", essential=True)
        raise
    except xapi.XCreditsError as e:
        # X APIのクレジット切れ（11/11 06:08〜）。管理者の支払いが必要なので、1日1回だけ知らせる（LINEの無料枠を使いすぎない）
        _close(draft, "failed", error=f"402 credits depleted: {e}")
        notify_credits_depleted()
        raise
    except (xapi.XRateLimitError, xapi.XPostError, requests.RequestException) as e:
        _close(draft, "failed", error=str(e))
        line.notify(f"投稿に失敗しました（再試行はしません）: {e}")
        raise

    posted_at = now_utc()
    _mark_news_posted(draft)
    store.record_post({
        "tweet_id": tweet_id,
        "draft_id": draft_id,
        "slot_key": draft["slot_key"],
        "posted_at": posted_at.isoformat(),
        "posted_at_jst": posted_at.astimezone(JST).strftime("%Y-%m-%d %H:%M"),
        "text": draft["text"],
        "style": draft["style"],
        "topic_key": draft["topic_key"],
        "total": draft["total"],
        "kind": draft["facts"][0]["kind"],
        "length": len(draft["text"]),
        "format": draft.get("format", "short"),
        "has_image": bool(draft.get("has_image")),
        "decided_by": decided_by,
    })
    _close(draft, "posted", tweet_id=tweet_id, posted_at=posted_at.isoformat())
    # 同じ枠の他の候補は投稿しない
    i = 1
    while sib := store.load_draft(f"{draft['slot_key']}-{i}"):
        if sib["id"] != draft_id and sib["status"] == "pending":
            _close(sib, "superseded")
        i += 1
    log.info("投稿しました: https://x.com/i/status/%s", tweet_id)
    if decided_by == "auto" and str(draft.get("slot_key", "")).split("-")[1].startswith("news"):
        # 通常の投稿の通知は月200通の無料枠を圧迫するので、夜のまとめ（1日1通）に回す。速報だけはすぐ知らせる
        line.send_posted_notice(draft, tweet_id)
    return "posted"


def delete_post(draft: dict) -> str:
    """投稿済みの下書きをXから削除する（LINEの「削除する」ボタン）。"""
    xapi.delete(draft["tweet_id"])
    store.mark_post_deleted(draft["tweet_id"])
    if str(draft.get("topic_key", "")).startswith("news:"):
        # 削除した速報の材料は、noteなど他の場所でも使わない（11/4 Xで削除した未確認の話題がnote記事に入った）
        facts = store.read_json("news_facts.json", []) or []
        still_live = any(p.get("topic_key") == draft["topic_key"] for p in store.posts())  # 同じ材料の投稿が他に残っていれば消さない
        for f in facts:
            if f["key"] == draft["topic_key"] and not still_live:
                f["status"] = "deleted"
        store.write_json("news_facts.json", facts)
    _close(draft, "deleted", deleted_at=now_utc().isoformat())
    store.record_decision({"id": draft["id"], "action": "delete", "by": "admin", "at": now_utc().isoformat(),
                           "total": draft["total"], "scores": draft["scores"], "style": draft["style"],
                           "topic_key": draft["topic_key"], "text": draft["text"]})
    if not os.environ.get("QUIET_DELETE"):
        # 点検からの削除（tools の delete-post）はLINEで知らせない。管理者がLINEのボタンで消したときだけ確認を返す
        # （11/13 点検の削除・再試行でLINEを1日26通使い、無料枠の残りが49通になった）
        line.notify("🗑 投稿を削除しました。理由は今後の改善に使います。")
    return "deleted"
