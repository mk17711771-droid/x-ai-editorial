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
COHORT_RULE = "60日以上プラス・売買が多すぎない・1回の大勝ちに頼らない・直近1週間に売買あり"
STRICT_NAME = "厳選トレーダー"
STRICT_RULE = "安定上位トレーダーの条件に加え、レバレッジ3倍以下・最大下落5〜15%・5日以内に売買"
INFO = "https://api.hyperliquid.xyz/info"


def _cfg() -> dict:
    return {
        "top_n": 50, "candidates": 300, "min_account_usd": 50_000, "max_account_usd": 20_000_000,
        "max_monthly_turnover": 150, "min_position_usd": 5_000,
        # 「勝ち続けていて、真似できる人」の条件（運用者のトレーダー選定ルールにもとづく）
        "min_days_positive": 60,       # 60日以上プラス
        "max_leverage": 3.0,           # 建玉÷口座の資産
        "max_fills_per_day": 15,       # 1日の約定数
        "mdd_min_pct": 5.0, "mdd_max_pct": 15.0,  # 最大下落（月間）
        "max_idle_days": 5,            # 直近の売買がない日数（厳選）
        "cohort_max_idle_days": 7,     # 同（安定上位）
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
    """ランキングから審査する候補を選ぶ。

    利益額の上位は大口や高頻度の自動売買が中心で「真似できる人」がほぼいないため、
    ランキングの数字だけで高頻度・巨大口座を先に外し、月間の利益率の高い順に候補にする。
    """
    cfg = _cfg()
    rows = requests.get(LEADERBOARD, timeout=60).json().get("leaderboardRows", [])
    picked = []
    for r in rows:
        month, all_time = _window(r, "month"), _window(r, "allTime")
        acct = float(r.get("accountValue", 0))
        if not cfg["min_account_usd"] <= acct <= cfg["max_account_usd"]:
            continue
        if month.get("pnl", 0) <= 0 or all_time.get("pnl", 0) <= 0:
            continue
        # 月間の売買代金が口座の資産の何倍か（回転が速すぎる人は高頻度の自動売買の可能性が高い）
        if month.get("vlm", 0) / acct > cfg["max_monthly_turnover"]:
            continue
        picked.append({"address": r["ethAddress"], "account_value": acct,
                       "month_pnl": month["pnl"], "month_roi": month.get("roi", 0)})
    picked.sort(key=lambda t: t["month_roi"], reverse=True)
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


def _max_drawdown_pct(pnl_history: list, account_history: list) -> float:
    """最大下落率（%）。入出金の影響を受けにくいよう損益の減り方で測り、そのときの口座の資産で割る。"""
    pnl = [float(v) for _, v in pnl_history]
    acct = [float(v) for _, v in account_history]
    if not pnl or len(acct) != len(pnl):
        return 0.0
    peak_pnl, peak_acct, mdd = pnl[0], acct[0], 0.0
    for p, a in zip(pnl, acct):
        if p >= peak_pnl:
            peak_pnl, peak_acct = p, a
        if peak_acct > 0:
            mdd = max(mdd, (peak_pnl - p) / peak_acct * 100)
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
        "mdd_pct": round(_max_drawdown_pct(month.get("pnlHistory", []), month.get("accountValueHistory", [])), 1),
        "leverage": round(float(ms.get("totalNtlPos") or 0) / acct, 2) if acct else None,
        "fills_30d": len(fills),
        "fills_per_day": round(len(recent) / 7, 1),
        "idle_days": round((now_ms - max(int(f["time"]) for f in fills)) / 86_400_000, 1) if fills else None,
        "max_win_share": round(max(wins) / sum(wins), 2) if wins else None,
        "positions": _parse_positions(state),
    }


CHECKS = [
    ("60日以上プラス", "stable", lambda m, c: m["pnl_60d"] is not None and m["pnl_60d"] > 0),
    ("売買が多すぎない", "stable", lambda m, c: m["fills_30d"] < 2000 and m["fills_per_day"] <= c["max_fills_per_day"]),
    ("1回の大勝ちに頼らない", "stable", lambda m, c: m["max_win_share"] is None or m["max_win_share"] <= c["max_single_win_share"]),
    ("直近1週間に売買あり", "stable", lambda m, c: m["idle_days"] is not None and m["idle_days"] <= c["cohort_max_idle_days"]),
    ("レバレッジ3倍以下", "strict", lambda m, c: m["leverage"] is not None and m["leverage"] <= c["max_leverage"]),
    ("最大下落5〜15%", "strict", lambda m, c: c["mdd_min_pct"] <= m["mdd_pct"] <= c["mdd_max_pct"]),
    ("5日以内に売買", "strict", lambda m, c: m["idle_days"] is not None and m["idle_days"] <= c["max_idle_days"]),
]


def judge(m: dict) -> str:
    """選定ルールで審査する。「厳選」「安定」、または最初に引っかかった条件名＋「を満たさない」。"""
    cfg = _cfg()
    for name, tier, ok in CHECKS:
        if not ok(m, cfg):
            return "安定" if tier == "strict" else f"{name}を満たさない"
    return "厳選"


def vet(t: dict) -> tuple[dict | None, str]:
    m = measure(t)
    why = judge(m)
    return (m if why in ("安定", "厳選") else None), why


def select_cohort(force: bool = False) -> dict:
    """候補を審査して「安定して勝っている上位トレーダー」を選ぶ。1日1回だけ更新する。"""
    cfg = _cfg()
    cohort = store.read_json("traders/cohort.json")
    if (cohort and cohort.get("members") and not force
            and now_utc() - datetime.fromisoformat(cohort["at"]) < timedelta(hours=cfg["cohort_refresh_hours"])):
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
            if why in ("安定", "厳選"):
                members.append({**row, "strict": why == "厳選"})
    # 利益の大きさは最後に見る
    members.sort(key=lambda m: m["pnl_60d"], reverse=True)
    members = members[: cfg["top_n"]]
    # 条件ごとに何人が残るか（「ランキング上位のうち、真似できる条件を満たすのは何人か」）
    cfgv = _cfg()
    funnel, remaining = [], [x for x in measured]
    funnel.append({"step": "審査した候補", "n": len(remaining)})
    for name, _, ok in CHECKS:
        remaining = [x for x in remaining if ok(x, cfgv)]
        funnel.append({"step": name, "n": len(remaining)})
    cohort = {"at": now_utc().isoformat(), "members": members, "reasons": reasons, "funnel": funnel,
              "strict_n": sum(1 for x in members if x["strict"])}
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
        out.append({"coin": p["coin"], "side": "long" if size > 0 else "short", "value": value, "size": abs(size),
                    "upnl": float(p.get("unrealizedPnl") or 0), "leverage": float((p.get("leverage") or {}).get("value") or 0)})
    return out


def spot_balances(address: str) -> dict[str, float]:
    """現物の保有量。HyperliquidではBTCはUBTC、ETHはUETHのような名前になっている。"""
    data = _info({"type": "spotClearinghouseState", "user": address})
    out: dict[str, float] = {}
    for b in data.get("balances", []):
        coin, total = b.get("coin", ""), float(b.get("total") or 0)
        for name in {coin, coin[1:] if coin.startswith(("U", "W")) and len(coin) > 2 else coin}:
            out[name] = out.get(name, 0) + total
    return out


def mark_hedges(pos: list[dict], spot: dict[str, float]) -> list[dict]:
    """現物を持ったうえでの売り（資金調達率を受け取るための裁定・ヘッジ）を見分ける。

    売りの数量の半分以上の現物を持っていれば、値下がりに賭けた売りではなく、ヘッジとみなす。
    """
    for p in pos:
        p["hedge"] = p["side"] == "short" and spot.get(p["coin"], 0) >= 0.5 * p["size"]
    return pos


def aggregate(traders: list[dict], pos_by_addr: dict[str, list[dict]]) -> dict:
    """銘柄ごとに、上位トレーダーの買い・売りの人数と金額を集計する。"""
    min_pos = _cfg()["min_position_usd"]
    coins: dict[str, dict] = {}
    for t in traders:
        for p in pos_by_addr.get(t["address"], []):
            if p["value"] < min_pos:
                continue
            c = coins.setdefault(p["coin"], {"coin": p["coin"], "long_n": 0, "short_n": 0, "hedge_n": 0,
                                             "long_usd": 0.0, "short_usd": 0.0, "hedge_usd": 0.0, "lev_sum": 0.0, "pos_n": 0})
            if p.get("hedge"):
                # 現物つきの売り（ヘッジ・裁定）は、方向の偏りには数えない
                c["hedge_n"] += 1
                c["hedge_usd"] += p["value"]
                continue
            c[f"{p['side']}_n"] += 1
            c[f"{p['side']}_usd"] += p["value"]
            c["lev_sum"] += p["leverage"]
            c["pos_n"] += 1
    for c in coins.values():
        c["avg_leverage"] = round(c.pop("lev_sum") / c["pos_n"], 1) if c["pos_n"] else 0
        c["net_usd"] = c["long_usd"] - c["short_usd"]
    ranked = sorted(coins.values(), key=lambda c: (c["long_n"] + c["short_n"] + c["hedge_n"], c["long_usd"] + c["short_usd"]),
                    reverse=True)
    return {"traders": len(traders), "with_positions": sum(1 for t in traders if pos_by_addr.get(t["address"])),
            "coins": ranked}


def snapshot() -> dict:
    """上位トレーダーのポジションを集計して保存する（リサーチの定期実行から呼ぶ）。"""
    traders = top_traders()
    pos_by_addr = {}
    for t in traders:
        try:
            pos = positions(t["address"])
            if any(p["side"] == "short" for p in pos):
                pos = mark_hedges(pos, spot_balances(t["address"]))
            pos_by_addr[t["address"]] = pos
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
        hedge = c.get("hedge_n", 0)
        if total < 3 and hedge < 3:
            continue
        if total == 0:
            out.append({**base,
                        "key": f"traders:{c['coin']}:hedge",
                        "category": "smart_money_hedge",
                        "summary": (f"{COHORT_NAME}{n}人のうち{hedge}人が、{c['coin']}を現物で持ったまま先物で売る「ヘッジ（資金調達率を受け取る裁定）」をしている。"
                                    f"方向に賭けた売買ではなく、値動きに左右されにくい形で稼いでいるとみられる（売りの合計{c.get('hedge_usd', 0):.0f}USD）"),
                        "fields": {"coin": c["coin"].split(":")[-1], "top_n": f"{n}人", "cohort": COHORT_NAME,
                                   "cohort_rule": COHORT_RULE, "hedge_n": f"{hedge}人",
                                   "hedge_usd": fmt.usd_amount_ja(c.get("hedge_usd", 0)),
                                   "asof": f"日本時間{fmt_jst(asof)}時点", "venue": "Hyperliquid"},
                        "raw": {"hedge_n": hedge}})
            continue
        long_ratio = c["long_n"] / total * 100
        out.append({**base,
                    "key": f"traders:{c['coin']}:bias",
                    "category": "smart_money",
                    "summary": (f"安定上位トレーダー{n}人（{COHORT_RULE}）のうち、{c['coin']}を持っている{total}人の内訳: 買い{c['long_n']}人・売り{c['short_n']}人"
                                f"（買い比率{long_ratio:.0f}%）、買い合計{c['long_usd']:.0f}USD・売り合計{c['short_usd']:.0f}USD、平均レバレッジ{c['avg_leverage']}倍"
                                f"（このほか、現物つきの売り＝ヘッジ・裁定が{hedge}人。これは方向の偏りに数えていない）"),
                    "fields": {"coin": c["coin"].split(":")[-1], "top_n": f"{n}人", "cohort": COHORT_NAME,
                               "cohort_rule": COHORT_RULE, "holders": f"{total}人",
                               "long_n": f"{c['long_n']}人", "short_n": f"{c['short_n']}人",
                               "long_ratio": f"{long_ratio:.0f}%", "long_usd": fmt.usd_amount_ja(c["long_usd"]),
                               "short_usd": fmt.usd_amount_ja(c["short_usd"]), "hedge_n": f"{hedge}人",
                               "avg_leverage": fmt.ratio_x(c["avg_leverage"]),
                               "asof": f"日本時間{fmt_jst(asof)}時点", "venue": "Hyperliquid"},
                    "raw": {"long_n": c["long_n"], "short_n": c["short_n"], "long_usd": round(c["long_usd"]),
                            "short_usd": round(c["short_usd"])}})
    cohort = store.read_json("traders/cohort.json")
    if cohort and cohort.get("funnel"):
        f0, steps = cohort["funnel"][0]["n"], {f["step"]: f["n"] for f in cohort["funnel"]}
        out.append({**base,
                    "key": f"traders:funnel:{cohort['at'][:10]}",
                    "category": "trader_funnel",
                    "summary": ("Hyperliquidのランキングから選んだ候補" + f"{f0}人を、真似できるかの条件で絞り込んだ結果: "
                                + "、".join(f"{f['step']}→残り{f['n']}人" for f in cohort["funnel"][1:])
                                + "。すべての条件を満たした人はごくわずかで、ランキングの上位＝真似する価値がある人ではないことがわかる"),
                    "fields": {"candidates": f"{f0}人", "positive_60d": f"{steps.get('60日以上プラス', 0)}人",
                               "stable_n": f"{steps.get('直近1週間に売買あり', 0)}人",
                               "low_leverage_n": f"{steps.get('レバレッジ3倍以下', 0)}人",
                               "strict_n": f"{steps.get('5日以内に売買', 0)}人",
                               "venue": "Hyperliquid"},
                    "raw": {"funnel": cohort["funnel"]}})
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
