"""Hyperliquidの上位トレーダー分析（公開データのみ）。

公開ランキングから月間成績の良いトレーダーを選び、今のポジションを集計する。
投稿では個人（アドレス）を特定せず、「上位N人のうち何人が買い越しているか」のような集計だけを使う。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import requests

from . import fmt, store
from .common import JST, fmt_jst, log, now_utc, settings

LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO = "https://api.hyperliquid.xyz/info"


def _cfg() -> dict:
    return {"top_n": 50, "min_account_usd": 100_000, "min_position_usd": 10_000, **settings().get("traders", {})}


def _window(row: dict, name: str) -> dict:
    for w, perf in row.get("windowPerformances", []):
        if w == name:
            return {k: float(v) for k, v in perf.items()}
    return {}


def top_traders() -> list[dict]:
    """月間の利益上位から、規模が一定以上で、月間・全期間ともにプラスのトレーダーを選ぶ。"""
    cfg = _cfg()
    rows = requests.get(LEADERBOARD, timeout=60).json().get("leaderboardRows", [])
    picked = []
    for r in rows:
        month, all_time = _window(r, "month"), _window(r, "allTime")
        if float(r.get("accountValue", 0)) < cfg["min_account_usd"]:
            continue
        if month.get("pnl", 0) <= 0 or all_time.get("pnl", 0) <= 0:
            continue
        picked.append({"address": r["ethAddress"], "account_value": float(r["accountValue"]),
                       "month_pnl": month["pnl"], "month_roi": month.get("roi", 0)})
    picked.sort(key=lambda t: t["month_pnl"], reverse=True)
    return picked[: cfg["top_n"]]


def positions(address: str) -> list[dict]:
    r = requests.post(INFO, json={"type": "clearinghouseState", "user": address}, timeout=30)
    r.raise_for_status()
    out = []
    for ap in r.json().get("assetPositions", []):
        p = ap.get("position", {})
        try:
            size = float(p["szi"])
            value = abs(float(p["positionValue"]))
        except (KeyError, TypeError, ValueError):
            continue
        if size == 0:
            continue
        out.append({"coin": p["coin"], "side": "long" if size > 0 else "short", "value": value,
                    "upnl": float(p.get("unrealizedPnl") or 0), "leverage": float((p.get("leverage") or {}).get("value") or 0)})
    return out


def aggregate(traders: list[dict], pos_by_addr: dict[str, list[dict]]) -> dict:
    """銘柄ごとに、上位トレーダーの買い・売りの人数と金額を集計する。"""
    min_pos = _cfg()["min_position_usd"]
    coins: dict[str, dict] = {}
    for t in traders:
        for p in pos_by_addr.get(t["address"], []):
            if p["value"] < min_pos:
                continue
            c = coins.setdefault(p["coin"], {"coin": p["coin"], "long_n": 0, "short_n": 0,
                                             "long_usd": 0.0, "short_usd": 0.0, "lev_sum": 0.0, "pos_n": 0})
            c[f"{p['side']}_n"] += 1
            c[f"{p['side']}_usd"] += p["value"]
            c["lev_sum"] += p["leverage"]
            c["pos_n"] += 1
    for c in coins.values():
        c["avg_leverage"] = round(c.pop("lev_sum") / c["pos_n"], 1) if c["pos_n"] else 0
        c["net_usd"] = c["long_usd"] - c["short_usd"]
    ranked = sorted(coins.values(), key=lambda c: (c["long_n"] + c["short_n"], c["long_usd"] + c["short_usd"]), reverse=True)
    return {"traders": len(traders), "with_positions": sum(1 for t in traders if pos_by_addr.get(t["address"])),
            "coins": ranked}


def snapshot() -> dict:
    """上位トレーダーのポジションを集計して保存する（リサーチの定期実行から呼ぶ）。"""
    traders = top_traders()
    pos_by_addr = {}
    for t in traders:
        try:
            pos_by_addr[t["address"]] = positions(t["address"])
        except requests.RequestException as e:
            log.warning("ポジション取得に失敗: %s", e)
    agg = aggregate(traders, pos_by_addr)
    now = now_utc()
    snap = {"at": now.isoformat(), **agg,
            # 利確の推定用に、アドレスは非公開リポジトリにだけ保存する（投稿には使わない）
            "positions": {t["address"]: [(p["coin"], p["side"], round(p["value"]), round(p["upnl"]))
                                               for p in pos_by_addr.get(t["address"], [])] for t in traders}}
    store.write_json(f"traders/{now.astimezone(JST):%Y%m%d-%H}.json", snap)
    store.write_json("traders/latest.json", snap)
    _prune(days=10)
    return snap


def _prune(days: int) -> None:
    """古いスナップショットを消して、リポジトリが大きくなりすぎないようにする。"""
    from .common import DATA_DIR

    cutoff = (now_utc() - timedelta(days=days)).astimezone(JST).strftime("%Y%m%d")
    for f in (DATA_DIR / "traders").glob("2*.json"):
        if f.name[:8] < cutoff:
            f.unlink()


def _previous(hours: float = 20) -> dict | None:
    """約1日前のスナップショット（変化の計算用）。"""
    now = now_utc()
    for h in range(int(hours), int(hours) + 10):
        s = store.read_json(f"traders/{(now - timedelta(hours=h)).astimezone(JST):%Y%m%d-%H}.json")
        if s:
            return s
    return None


def closed_in_profit(prev: dict, cur: dict) -> dict[str, dict]:
    """前回は含み益で持っていたのに、今回はなくなったポジション（利確とみられるもの）を銘柄ごとに数える。"""
    out: dict[str, dict] = {}
    for addr, plist in prev.get("positions", {}).items():
        now_keys = {(c, s) for c, s, *_ in cur.get("positions", {}).get(addr, [])}
        for coin, side, value, upnl in plist:
            if (coin, side) not in now_keys and upnl > 0 and value >= _cfg()["min_position_usd"]:
                o = out.setdefault(coin, {"n": 0, "upnl": 0})
                o["n"] += 1
                o["upnl"] += upnl
    return out


def facts(max_age_hours: float = 4) -> list[dict]:
    """最新の集計から、編集長AIに渡す材料を作る。古い集計は使わない。"""
    snap = store.read_json("traders/latest.json")
    if not snap or now_utc() - datetime.fromisoformat(snap["at"]) > timedelta(hours=max_age_hours):
        return []
    n = snap["traders"]
    asof = datetime.fromisoformat(snap["at"])
    base = {"kind": "hl", "refresh": None, "confidence": "confirmed", "asof": snap["at"],
            "source": {"name": "Hyperliquid公開ランキング・ポジション", "url": "https://app.hyperliquid.xyz/leaderboard"}}
    out = []
    for c in snap["coins"][:6]:
        total = c["long_n"] + c["short_n"]
        if total < 3:
            continue
        long_ratio = c["long_n"] / total * 100
        out.append({**base,
                    "key": f"traders:{c['coin']}:bias",
                    "category": "smart_money",
                    "summary": (f"月間利益上位{n}人のうち、{c['coin']}を持っている{total}人の内訳: 買い{c['long_n']}人・売り{c['short_n']}人"
                                f"（買い比率{long_ratio:.0f}%）、買い合計{c['long_usd']:.0f}USD・売り合計{c['short_usd']:.0f}USD、平均レバレッジ{c['avg_leverage']}倍"
                                "。注意: 上位口座には現物保有のヘッジや裁定目的で売りを持つ口座も含まれうるため、売り越し＝弱気とは限らない"),
                    "fields": {"coin": c["coin"].split(":")[-1], "top_n": f"{n}人", "holders": f"{total}人",
                               "long_n": f"{c['long_n']}人", "short_n": f"{c['short_n']}人",
                               "long_ratio": f"{long_ratio:.0f}%", "long_usd": fmt.usd_amount_ja(c["long_usd"]),
                               "short_usd": fmt.usd_amount_ja(c["short_usd"]),
                               "avg_leverage": fmt.ratio_x(c["avg_leverage"]),
                               "asof": f"日本時間{fmt_jst(asof)}時点", "venue": "Hyperliquid"},
                    "raw": {"long_n": c["long_n"], "short_n": c["short_n"], "long_usd": round(c["long_usd"]),
                            "short_usd": round(c["short_usd"])}})
    prev = _previous()
    if prev:
        for coin, o in sorted(closed_in_profit(prev, snap).items(), key=lambda kv: kv[1]["upnl"], reverse=True)[:3]:
            if o["n"] < 2:
                continue
            out.append({**base,
                        "key": f"traders:{coin}:takeprofit",
                        "category": "smart_money_exit",
                        "summary": f"月間利益上位{n}人のうち{o['n']}人が、約1日前に含み益で持っていた{coin}のポジションを閉じた（利確とみられる。前回時点の含み益合計{o['upnl']:.0f}USD）",
                        "fields": {"coin": coin.split(":")[-1], "top_n": f"{n}人", "closed_n": f"{o['n']}人",
                                   "upnl": fmt.usd_amount_ja(o["upnl"]), "asof": f"日本時間{fmt_jst(asof)}時点",
                                   "venue": "Hyperliquid"},
                        "raw": {"closed_n": o["n"], "upnl": round(o["upnl"])}})
    return out
