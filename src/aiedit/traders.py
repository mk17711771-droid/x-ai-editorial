"""Hyperliquidの上位トレーダー分析（公開データのみ）。

「一番勝った人」ではなく「勝ち続けていて、真似できる人」を選ぶ。
公開ランキングの候補を、60日以上の成績・最大下落・レバレッジ・売買頻度・稼働状況・大勝ちへの依存で審査し、
合格した人（安定上位トレーダー）の今のポジションを集計する。
投稿では個人（アドレス）を特定せず、「上位N人のうち何人が買い越しているか」のような集計だけを使う。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import requests

from . import fmt, store
from .common import JST, fmt_jst, log, now_utc, settings

LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
COHORT_NAME = "安定上位トレーダー"
COHORT_RULE = "60日以上プラス・最大下落5〜15%・レバレッジ3倍以下・売買が多すぎず最近も稼働"
INFO = "https://api.hyperliquid.xyz/info"


def _cfg() -> dict:
    return {
        "top_n": 50, "candidates": 120, "min_account_usd": 100_000, "min_position_usd": 10_000,
        # 「勝ち続けていて、真似できる人」の条件（運用者のトレーダー選定ルールにもとづく）
        "min_days_positive": 60,       # 60日以上プラス
        "max_leverage": 3.0,           # 建玉÷口座の資産
        "max_fills_per_day": 15,       # 1日の約定数
        "mdd_min_pct": 5.0, "mdd_max_pct": 15.0,  # 最大下落（月間）
        "max_idle_days": 5,            # 直近の売買がない日数
        "max_single_win_share": 0.5,   # 1回の大勝ちが利益に占める割合
        "cohort_refresh_hours": 24,
        **settings().get("traders", {}),
    }


def _window(row: dict, name: str) -> dict:
    for w, perf in row.get("windowPerformances", []):
        if w == name:
            return {k: float(v) for k, v in perf.items()}
    return {}


def candidates() -> list[dict]:
    """ランキングから候補を選ぶ: 月間の利益上位で、規模が一定以上、月間・全期間ともにプラス。"""
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
    return picked[: cfg["candidates"]]


def _info(body: dict) -> object:
    """公開APIの呼び出し。混雑（429）のときは少し待ってやり直す。"""
    for attempt in range(4):
        r = requests.post(INFO, json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(0.3)  # 公開APIに負担をかけない
        return r.json()
    raise requests.HTTPError("429 Too Many Requests")


def _pnl_change(history: list, days: int) -> float | None:
    """損益の推移から、直近N日の損益の増減を求める。"""
    if not history:
        return None
    pts = [(int(t), float(v)) for t, v in history]
    target = pts[-1][0] - days * 86_400_000
    if pts[0][0] > target:
        return None  # N日分の履歴がない
    past = min(pts, key=lambda p: abs(p[0] - target))
    return pts[-1][1] - past[1]


def _max_drawdown_pct(pnl_history: list, account_value: float) -> float:
    """損益の推移から最大下落率（%）を求める。入出金の影響を受けにくいよう、口座の資産ではなく損益で計算する。"""
    pts = [float(v) for _, v in pnl_history]
    if not pts:
        return 0.0
    base = max(account_value - pts[-1], 1.0)  # 期間の初めの資産の目安
    peak, mdd = pts[0], 0.0
    for v in pts:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / (base + peak) * 100)
    return mdd


def measure(t: dict) -> dict:
    """1人のトレーダーの指標を、公開データから計算する。"""
    now_ms = int(now_utc().timestamp() * 1000)
    portfolio = dict(_info({"type": "portfolio", "user": t["address"]}))
    all_time, month = portfolio.get("allTime", {}), portfolio.get("month", {})
    state = _info({"type": "clearinghouseState", "user": t["address"]})
    ms = state.get("marginSummary", {})
    acct = float(ms.get("accountValue") or 0)
    fills = _info({"type": "userFillsByTime", "user": t["address"], "startTime": now_ms - 30 * 86_400_000})
    recent = [f for f in fills if int(f["time"]) >= now_ms - 7 * 86_400_000]
    wins = [float(f.get("closedPnl") or 0) for f in fills if float(f.get("closedPnl") or 0) > 0]
    hist = all_time.get("pnlHistory", [])
    return {
        **t,
        "pnl_60d": _pnl_change(hist, 60),
        "history_days": round((int(hist[-1][0]) - int(hist[0][0])) / 86_400_000) if len(hist) > 1 else 0,
        "mdd_pct": round(_max_drawdown_pct(month.get("pnlHistory", []), t["account_value"]), 1),
        "leverage": round(float(ms.get("totalNtlPos") or 0) / acct, 2) if acct else None,
        "fills_30d": len(fills),
        "fills_per_day": round(len(recent) / 7, 1),
        "idle_days": round((now_ms - max(int(f["time"]) for f in fills)) / 86_400_000, 1) if fills else None,
        "max_win_share": round(max(wins) / sum(wins), 2) if wins else None,
        "positions": _parse_positions(state),
    }


def judge(m: dict) -> str:
    """選定ルールで審査する。合格なら「合格」、不合格なら最初に引っかかった理由。"""
    cfg = _cfg()
    if m["pnl_60d"] is None or m["pnl_60d"] <= 0:
        return "60日以上プラスではない"
    if not cfg["mdd_min_pct"] <= m["mdd_pct"] <= cfg["mdd_max_pct"]:
        return "最大下落が目安の範囲外"
    if m["leverage"] is None or m["leverage"] > cfg["max_leverage"]:
        return "レバレッジが高い"
    if m["fills_30d"] >= 2000 or m["fills_per_day"] > cfg["max_fills_per_day"]:
        return "売買の回数が多すぎる"
    if m["idle_days"] is None or m["idle_days"] > cfg["max_idle_days"]:
        return "最近売買していない"
    if m["max_win_share"] is not None and m["max_win_share"] > cfg["max_single_win_share"]:
        return "1回の大勝ちに頼っている"
    return "合格"


def vet(t: dict) -> tuple[dict | None, str]:
    m = measure(t)
    why = judge(m)
    return (m if why == "合格" else None), why


def select_cohort(force: bool = False) -> dict:
    """候補を審査して「安定して勝っている上位トレーダー」を選ぶ。1日1回だけ更新する。"""
    cfg = _cfg()
    cohort = store.read_json("traders/cohort.json")
    if cohort and not force and now_utc() - datetime.fromisoformat(cohort["at"]) < timedelta(hours=cfg["cohort_refresh_hours"]):
        return cohort
    members, reasons, measured = [], {}, []
    for t in candidates():
        try:
            m = measure(t)
            why = judge(m)
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            m, why = None, f"取得失敗: {type(e).__name__}"
        reasons[why] = reasons.get(why, 0) + 1
        if m:
            row = {k: v for k, v in m.items() if k != "positions"}
            measured.append({**row, "result": why})
            if why == "合格":
                members.append(row)
        if len(members) >= cfg["top_n"]:
            break
    # 利益の大きさは最後に見る
    members.sort(key=lambda m: m["pnl_60d"], reverse=True)
    cohort = {"at": now_utc().isoformat(), "members": members, "reasons": reasons}
    # 条件の調整用に、候補全員の指標を残す（アドレスは非公開リポジトリにだけ保存）
    store.write_json("traders/candidates_measured.json", measured)
    store.write_json("traders/cohort.json", cohort)
    log.info("安定上位トレーダー %d人を選定（審査結果: %s）", len(members), reasons)
    return cohort


def top_traders() -> list[dict]:
    return select_cohort()["members"]


def positions(address: str) -> list[dict]:
    return _parse_positions(_info({"type": "clearinghouseState", "user": address}))


def _parse_positions(state: dict) -> list[dict]:
    out = []
    for ap in state.get("assetPositions", []):
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
                    "summary": (f"安定上位トレーダー{n}人（{COHORT_RULE}）のうち、{c['coin']}を持っている{total}人の内訳: 買い{c['long_n']}人・売り{c['short_n']}人"
                                f"（買い比率{long_ratio:.0f}%）、買い合計{c['long_usd']:.0f}USD・売り合計{c['short_usd']:.0f}USD、平均レバレッジ{c['avg_leverage']}倍"
                                "。注意: 上位口座には現物保有のヘッジや裁定目的で売りを持つ口座も含まれうるため、売り越し＝弱気とは限らない"),
                    "fields": {"coin": c["coin"].split(":")[-1], "top_n": f"{n}人", "cohort": COHORT_NAME,
                               "cohort_rule": COHORT_RULE, "holders": f"{total}人",
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
                        "summary": f"安定上位トレーダー{n}人のうち{o['n']}人が、約1日前に含み益で持っていた{coin}のポジションを閉じた（利確とみられる。前回時点の含み益合計{o['upnl']:.0f}USD）",
                        "fields": {"coin": coin.split(":")[-1], "top_n": f"{n}人", "closed_n": f"{o['n']}人",
                                   "upnl": fmt.usd_amount_ja(o["upnl"]), "asof": f"日本時間{fmt_jst(asof)}時点",
                                   "venue": "Hyperliquid"},
                        "raw": {"closed_n": o["n"], "upnl": round(o["upnl"])}})
    return out
