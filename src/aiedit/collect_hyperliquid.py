"""Hyperliquid公式API（キー不要・無料）から市場データを収集し、投稿の材料（fact）を作る。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import requests

from . import fmt, store
from .common import fmt_jst, log, now_utc, settings

API = "https://api.hyperliquid.xyz/info"
SOURCE_NAME = "Hyperliquid公式API"


def _info(body: dict) -> object:
    r = requests.post(API, json=body, timeout=20)
    r.raise_for_status()
    return r.json()


def _to_market(asset: dict, ctx: dict, dex: str) -> dict | None:
    if asset.get("isDelisted"):
        return None
    try:
        mark = float(ctx["markPx"])
        prev = float(ctx["prevDayPx"])
        oracle = float(ctx.get("oraclePx") or 0)
        oi = float(ctx["openInterest"]) * mark
        vol = float(ctx["dayNtlVlm"])
        funding_h = float(ctx["funding"])
    except (KeyError, TypeError, ValueError):
        return None
    if mark <= 0 or prev <= 0:
        return None
    # 取引価格が参照価格（オラクル）から大きく乖離している市場は、データ異常の可能性があるため除外
    if oracle > 0 and abs(mark / oracle - 1) > 0.03:
        log.info("価格乖離のため除外: %s mark=%s oracle=%s", asset["name"], mark, oracle)
        return None
    return {
        "coin": asset["name"],
        "dex": dex,
        "mark": mark,
        "change_pct": (mark / prev - 1) * 100,
        "oi_usd": oi,
        "vol_usd": vol,
        # 資金調達率は1時間ごと。年率換算 = 1時間レート × 24 × 365
        "funding_annual_pct": funding_h * 24 * 365 * 100,
    }


def fetch_markets() -> list[dict]:
    """全無期限先物（標準 + HIP-3）の市場データを取得して数値に変換する。"""
    dexes = [""]
    if settings()["hyperliquid"].get("include_hip3"):
        try:
            for d in _info({"type": "perpDexs"}):
                if d and d.get("name"):
                    dexes.append(d["name"])
        except requests.RequestException as e:
            log.warning("HIP-3市場一覧の取得に失敗: %s", e)

    markets = []
    for dex in dexes:
        body = {"type": "metaAndAssetCtxs"}
        if dex:
            body["dex"] = dex
        try:
            meta, ctxs = _info(body)
        except requests.RequestException as e:
            if not dex:
                raise
            log.warning("HIP-3市場 %s の取得に失敗: %s", dex, e)
            continue
        for asset, ctx in zip(meta["universe"], ctxs):
            m = _to_market(asset, ctx, dex)
            if m:
                markets.append(m)
    return markets


def display_name(m: dict) -> str:
    # HIP-3の銘柄は "xyz:NVDA" の形式
    return m["coin"].split(":", 1)[1] if ":" in m["coin"] else m["coin"]


def _trade_url(m: dict) -> str:
    return f"https://app.hyperliquid.xyz/trade/{m['coin']}"


def _snapshot_oi(markets: list[dict]) -> dict[str, float]:
    """建玉の24時間変化を計算するため、実行ごとにスナップショットを保存する。"""
    snaps = store.read_json("hl_snapshots.json", [])
    now = now_utc()
    snaps.append({"ts": now.isoformat(), "oi": {m["coin"]: round(m["oi_usd"]) for m in markets}})
    cutoff = now - timedelta(hours=72)
    snaps = [s for s in snaps if datetime.fromisoformat(s["ts"]) >= cutoff]
    store.write_json("hl_snapshots.json", snaps)
    # 20〜28時間前のスナップショットのうち、24時間前に最も近いもの
    best, best_gap = None, None
    for s in snaps:
        age = (now - datetime.fromisoformat(s["ts"])).total_seconds() / 3600
        if 20 <= age <= 28:
            gap = abs(age - 24)
            if best_gap is None or gap < best_gap:
                best, best_gap = s, gap
    return best["oi"] if best else {}


def _fact(m: dict, category: str, summary: str, extra_fields: dict | None = None, extra_raw: dict | None = None) -> dict:
    asof = now_utc()
    fields = {
        "coin": display_name(m),
        "market": "株式先物（HIP-3）" if m["dex"] else "無期限先物",
        "price": fmt.usd_price(m["mark"]),
        "change_pct": fmt.pct(m["change_pct"]),
        "oi": fmt.usd_amount_ja(m["oi_usd"]),
        "volume": fmt.usd_amount_ja(m["vol_usd"]),
        "funding_annual": fmt.pct(m["funding_annual_pct"], 1),
        "asof": f"日本時間{fmt_jst(asof)}時点",
    }
    fields.update(extra_fields or {})
    raw = {
        "mark": m["mark"],
        "change_pct": round(m["change_pct"], 3),
        "oi_usd": round(m["oi_usd"]),
        "vol_usd": round(m["vol_usd"]),
        "funding_annual_pct": round(m["funding_annual_pct"], 2),
    }
    if "us_change_pct" in m:
        # 照合に使った米国株の前日比（検証の記録）
        raw["us_change_pct"] = round(m["us_change_pct"], 3)
        fields["us_change_pct"] = fmt.pct(m["us_change_pct"])
        summary += f"（照合: 米国株の前日比{m['us_change_pct']:+.2f}%）"
    raw.update(extra_raw or {})
    return {
        "key": f"hl:{m['coin']}:{category}",
        "kind": "hl",
        "category": category,
        "summary": summary,
        "fields": fields,
        "raw": raw,
        "refresh": {"type": "hl_coin", "coin": m["coin"], "dex": m["dex"]},
        "source": {"name": SOURCE_NAME, "url": _trade_url(m)},
        "asof": asof.isoformat(),
        "confidence": "confirmed",
    }


def _verify_hip3_moves(markets: list[dict]) -> list[dict]:
    """HIP-3の株式先物で大きく動いた銘柄は、実際の米国株の値動きと照合する。

    24時間の騰落率と米国株の前日比は計算期間が違うため完全には一致しないが、
    大きく食い違う場合はデータ異常の可能性が高いので材料から外す。
    """
    if not os.environ.get("FINNHUB_API_KEY"):
        return markets
    from . import collect_stocks

    big = settings()["hyperliquid"]["big_move_pct"]
    kept = []
    for m in markets:
        if m["dex"] and abs(m["change_pct"]) >= big:
            try:
                q = collect_stocks.quote(display_name(m))
            except requests.RequestException:
                q = None
            if q is None:
                log.info("HIP-3 %s は米国株と照合できないため除外", m["coin"])
                continue
            if abs(float(q["dp"]) - m["change_pct"]) > big:
                log.info("HIP-3 %s の騰落率%+.1f%%が米国株%+.1f%%と食い違うため除外", m["coin"], m["change_pct"], float(q["dp"]))
                continue
            m["us_change_pct"] = float(q["dp"])
        kept.append(m)
    return kept


def collect() -> list[dict]:
    cfg = settings()["hyperliquid"]
    markets = _verify_hip3_moves(fetch_markets())
    prev_oi = _snapshot_oi(markets)
    liquid = [m for m in markets if m["vol_usd"] >= cfg["min_day_volume_usd"]]
    n = cfg["top_n"]
    facts: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(m: dict, category: str, summary: str, **kw) -> None:
        if (m["coin"], category) in seen:
            return
        seen.add((m["coin"], category))
        facts.append(_fact(m, category, summary, **kw))

    def desc(m: dict) -> str:
        kind = "HIP-3株式等先物" if m["dex"] else "無期限先物"
        return (f"{display_name(m)}（{kind}） 24時間騰落率{m['change_pct']:+.2f}%, 価格{m['mark']}, "
                f"建玉{m['oi_usd']:.0f}USD, 24時間出来高{m['vol_usd']:.0f}USD, 資金調達率(年率){m['funding_annual_pct']:+.1f}%")

    for m in sorted(liquid, key=lambda m: m["vol_usd"], reverse=True)[:n]:
        add(m, "volume_top", "出来高上位: " + desc(m))
    for m in sorted(liquid, key=lambda m: abs(m["change_pct"]), reverse=True)[:n]:
        if abs(m["change_pct"]) >= cfg["big_move_pct"]:
            add(m, "big_move", "24時間の急変動: " + desc(m))
    for m in sorted(liquid, key=lambda m: abs(m["funding_annual_pct"]), reverse=True)[:n]:
        if abs(m["funding_annual_pct"]) >= cfg["funding_extreme_annual_pct"]:
            side = "ロング過熱（ロングがショートに支払い）" if m["funding_annual_pct"] > 0 else "ショート過熱（ショートがロングに支払い）"
            add(m, "funding_extreme", f"資金調達率が異常値・{side}: " + desc(m))

    oi_changes = []
    for m in liquid:
        before = prev_oi.get(m["coin"])
        if before and before > 1e6:
            oi_changes.append((m, (m["oi_usd"] / before - 1) * 100))
    for m, ch in sorted(oi_changes, key=lambda t: abs(t[1]), reverse=True)[:n]:
        if abs(ch) >= 15:
            add(m, "oi_change", f"建玉の24時間変化{ch:+.1f}%: " + desc(m),
                extra_fields={"oi_change_pct": fmt.pct(ch, 1)}, extra_raw={"oi_change_pct": round(ch, 2)})

    hip3 = [m for m in liquid if m["dex"]]
    for m in sorted(hip3, key=lambda m: m["vol_usd"], reverse=True)[:n]:
        add(m, "hip3_top", "HIP-3市場（株式先物など）の出来高上位: " + desc(m))

    # 市場全体
    total_oi = sum(m["oi_usd"] for m in markets)
    total_vol = sum(m["vol_usd"] for m in markets)
    hip3_vol = sum(m["vol_usd"] for m in markets if m["dex"])
    asof = now_utc()
    facts.append({
        "key": "hl:market:overview",
        "kind": "hl",
        "category": "overview",
        "summary": f"Hyperliquid全体: 建玉合計{total_oi:.0f}USD, 24時間出来高合計{total_vol:.0f}USD, うちHIP-3市場{hip3_vol:.0f}USD, 上場銘柄数{len(markets)}",
        "fields": {
            "total_oi": fmt.usd_amount_ja(total_oi),
            "total_volume": fmt.usd_amount_ja(total_vol),
            "hip3_volume": fmt.usd_amount_ja(hip3_vol),
            "hip3_share": fmt.pct(hip3_vol / total_vol * 100, 1).lstrip("+") if total_vol else "0.0%",
            "asof": f"日本時間{fmt_jst(asof)}時点",
        },
        "raw": {"total_oi": round(total_oi), "total_vol": round(total_vol), "hip3_vol": round(hip3_vol)},
        "refresh": None,
        "source": {"name": SOURCE_NAME, "url": "https://app.hyperliquid.xyz/trade"},
        "asof": asof.isoformat(),
        "confidence": "confirmed",
    })
    return facts


def refresh_market(ref: dict) -> dict | None:
    """投稿直前の再確認用: 指定銘柄の最新の市場データを返す。"""
    body = {"type": "metaAndAssetCtxs"}
    if ref.get("dex"):
        body["dex"] = ref["dex"]
    meta, ctxs = _info(body)
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset["name"] == ref["coin"]:
            return _to_market(asset, ctx, ref.get("dex", ""))
    return None


def refresh_change_pct(ref: dict) -> float | None:
    m = refresh_market(ref)
    return m["change_pct"] if m else None


def refresh_fact(fact: dict) -> dict | None:
    """材料を最新のデータで作り直す。作り直せない材料（建玉の24時間変化など）は None。"""
    if fact["category"] in ("oi_change", "overview"):
        return None
    m = refresh_market(fact["refresh"])
    if not m:
        return None
    if m["dex"] and "us_change_pct" in fact["raw"]:
        if not _verify_hip3_moves([m]) or "us_change_pct" not in m:
            return None
    new = _fact(m, fact["category"], fact["summary"])
    new["id"] = fact["id"]
    return new
