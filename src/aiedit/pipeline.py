"""編集部の処理の流れ: 収集 → 編集長（選定・採点）→ 制作（原稿）→ チェック → 承認依頼 / 投稿。"""
from __future__ import annotations

import json
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
    weekend = now.weekday() >= 5
    if slot_id != "auto":
        for s in cfg["slots"]:
            if s["id"] == slot_id:
                return s
        raise ValueError(f"枠 {slot_id} が設定にありません")
    best = None
    for s in cfg["slots"]:
        if s["days"] == "weekday" and weekend or s["days"] == "weekend" and not weekend:
            continue
        h, m = map(int, s["time"].split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start - timedelta(minutes=10) <= now <= start + timedelta(minutes=cfg["slot_grace_minutes"]):
            if best is None or start > best[0]:
                best = (start, s)
    return best[1] if best else None


def slot_format(slot: dict) -> str:
    """長文は X Premium のときだけ。それ以外は短文にする。"""
    return "long" if slot.get("format") == "long" and settings()["account"]["premium"] else "short"


def slot_key(slot: dict) -> str:
    return f"{now_jst():%Y%m%d}-{slot['id']}"


# ---------------------------------------------------------------- 生成

def collect(kind: str) -> list[dict]:
    raw = collect_stocks.collect() if kind == "stock" else collect_hyperliquid.collect() + traders.facts()
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


def run_editor(slot: dict, facts: list[dict]) -> list[dict]:
    recent = [p.get("topic_key") for p in store.recent_posts(72)]
    form = ("長文の考察投稿。複数の材料（目安3〜8個）を組み合わせて、背景と「歪み」を深く解説できる切り口を選ぶ。"
            if slot_format(slot) == "long" else "短文の投稿。1つの異変を一言で伝えられる切り口を選ぶ。")
    user = (f"# 今回の投稿枠\n{slot['time']} {slot['theme']}\n\n# 投稿の形式\n{form}\n\n"
            f"# 直近72時間の投稿トピック (recent_topics)\n{json.dumps(recent, ensure_ascii=False)}\n\n"
            f"# 材料 (facts)\n{_facts_for_llm(facts, with_fields=False)}")
    watch = research.watch_summary()
    if watch:
        user += f"\n\n# 直近24時間の定点観測（流れの把握用。投稿の数字には使わない）\n{watch}"
    news = sources.news_digest()
    if news:
        user += f"\n\n# いま話題のニュース見出し（切り口の参考。見出しの数字・事実は投稿に使わない）\n{news}"
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


def run_writer(slot: dict, cand: dict, facts_by_id: dict[str, dict]) -> dict | None:
    """原稿を3案作らせ、チェックを通過した中で最良の1案を返す。"""
    fmt = slot_format(slot)
    min_chars, max_chars, _ = checks.length_limits(fmt)
    system = with_playbook(llm.prompt("writer_long" if fmt == "long" else "writer")
                           .replace("{min_chars}", str(min_chars)).replace("{max_chars}", str(max_chars)))
    used = [facts_by_id[i] for i in cand["fact_ids"]]
    user = (f"# 投稿枠\n{slot['theme']}\n\n# 編集長の切り口\n{cand.get('angle', '')}\n\n"
            f"# 材料（この差し込み記号だけ使える）\n{_facts_for_llm(used, with_fields=True)}\n\n"
            f"# 直近の投稿 (recent_posts)\n{json.dumps(list(store.recent_texts(10)), ensure_ascii=False)}")
    out = llm.generate_json(system, user)
    variants = out.get("variants", [])
    best = out.get("best", 0)
    order = ([best] if isinstance(best, int) and 0 <= best < len(variants) else []) + list(range(len(variants)))
    topic_key = cand.get("topic_key") or used[0]["key"]
    usable = {f["id"]: f for f in used}
    tried = set()
    for idx in order:
        if idx in tried:
            continue
        tried.add(idx)
        v = variants[idx]
        tmpl = v.get("template", "")
        errors = checks.check_template(tmpl, usable)
        text = checks.render(tmpl, usable) if not errors else tmpl
        errors = errors or checks.check_text(text, topic_key, fmt)
        if errors:
            log.info("案%d（%s）を破棄: %s", idx, v.get("style"), errors)
            # 破棄理由を残し、プロンプト改善に使う
            store.append_jsonl("rejected_variants.jsonl", {
                "at": now_utc().isoformat(), "slot": slot["id"], "format": fmt, "style": v.get("style"),
                "errors": errors, "length": len(text), "text": text[:1500]})
            continue
        return {"template": tmpl, "text": text, "style": v.get("style", ""), "topic_key": topic_key, "facts": used,
                "format": fmt}
    return None


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

    facts = collect(slot["kind"])
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

    drafts = []
    for cand in cands:
        if len(drafts) >= cfg["limits"]["max_candidates_per_slot"]:
            break
        if any(d["topic_key"] == cand.get("topic_key") for d in drafts):
            continue
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
        return []

    # 全自動モードでは最高点の案を投稿する。未検証ニュースを含む案だけは人の承認に回す
    auto_pick = next((d for d in drafts if not d["needs_human"]), None) if cfg["mode"] == "auto" else None
    if auto_pick:
        publish(auto_pick["id"], "approve", decided_by="auto")
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
    if len(store.recent_posts(24)) >= cfg["max_posts_per_day"]:
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
        line.notify("投稿直前の確認で問題が見つかったため投稿を中止しました:\n- " + "\n- ".join(problems)
                    + "\n\n値動きで投稿の切り口が成り立たなくなったため、作り直しも見送りました。")
        return "stale"

    media_ids = _attach_chart(draft)
    try:
        tweet_id = xapi.post(draft["text"], media_ids=media_ids)
    except xapi.XAuthError as e:
        store.halt(f"X API認証・権限エラー: {e}")
        _close(draft, "failed", error=str(e))
        line.notify(f"【緊急停止】X APIで認証・権限エラーが発生したため自動運用を停止しました。\n{e}\n\n確認後、LINEで「再開」と送ってください。")
        raise
    except (xapi.XRateLimitError, xapi.XPostError, requests.RequestException) as e:
        _close(draft, "failed", error=str(e))
        line.notify(f"投稿に失敗しました（再試行はしません）: {e}")
        raise

    posted_at = now_utc()
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
    if decided_by == "auto":
        line.send_posted_notice(draft, tweet_id)
    return "posted"


def delete_post(draft: dict) -> str:
    """投稿済みの下書きをXから削除する（LINEの「削除する」ボタン）。"""
    xapi.delete(draft["tweet_id"])
    _close(draft, "deleted", deleted_at=now_utc().isoformat())
    store.record_decision({"id": draft["id"], "action": "delete", "by": "admin", "at": now_utc().isoformat(),
                           "total": draft["total"], "scores": draft["scores"], "style": draft["style"],
                           "topic_key": draft["topic_key"], "text": draft["text"]})
    line.notify("🗑 投稿を削除しました。理由は今後の改善に使います。")
    return "deleted"
