"""Finnhub無料API（要APIキー）から米国株の株価・ニュース・決算予定を収集する。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from . import fmt
from .common import JST, NY, WEEKDAYS_JA, env, fmt_jst, log, now_utc, settings

API = "https://finnhub.io/api/v1"
SOURCE_NAME = "Finnhub"


def _get(path: str, **params) -> object:
    params["token"] = env("FINNHUB_API_KEY")
    r = requests.get(f"{API}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def quote(symbol: str) -> dict | None:
    q = _get("/quote", symbol=symbol)
    # 取得できない銘柄は c=0 が返る
    if not q or not q.get("c") or not q.get("pc"):
        return None
    return q


def _session_label(ts: int) -> str:
    """株価の時点を日本の読者向けに表示: 「米国10/23終値」/「日本時間10/24(金) 00:15時点」"""
    t = datetime.fromtimestamp(ts, tz=timezone.utc)
    ny = t.astimezone(NY)
    now_ny = now_utc().astimezone(NY)
    in_session = (
        now_ny.weekday() < 5
        and ny.date() == now_ny.date()
        and (9, 30) <= (now_ny.hour, now_ny.minute) < (16, 0)
    )
    if in_session:
        return f"日本時間{fmt_jst(t)}時点"
    return f"米国{ny.month}/{ny.day}終値"


def _date_ja(d: str) -> str:
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt.month}/{dt.day}({WEEKDAYS_JA[dt.weekday()]})"


def collect() -> list[dict]:
    cfg = settings()["stocks"]
    facts: list[dict] = []
    asof = now_utc()

    for sym in cfg["watchlist"]:
        try:
            q = quote(sym)
        except requests.RequestException as e:
            log.warning("%s の株価取得に失敗: %s", sym, e)
            continue
        if not q:
            continue
        dp = float(q["dp"])
        big = abs(dp) >= cfg["big_move_pct"]
        category = "big_move" if big else "quote"
        facts.append({
            "key": f"stock:{sym}:{category}",
            "kind": "stock",
            "category": category,
            "summary": (f"{'【大きな値動き】' if big else ''}{sym} 価格{q['c']}, 前日比{dp:+.2f}%, 前日終値{q['pc']}, "
                        f"高値{q['h']}, 安値{q['l']}"),
            "fields": {
                "symbol": sym,
                "price": fmt.usd_price(float(q["c"])),
                "change_pct": fmt.pct(dp),
                "prev_close": fmt.usd_price(float(q["pc"])),
                "high": fmt.usd_price(float(q["h"])),
                "low": fmt.usd_price(float(q["l"])),
                "asof": _session_label(int(q["t"])),
                "basis": "前日終値比",
            },
            "raw": {"price": q["c"], "change_pct": round(dp, 3), "prev_close": q["pc"]},
            "refresh": {"type": "stock", "symbol": sym},
            "source": {"name": SOURCE_NAME, "url": f"https://finance.yahoo.com/quote/{sym}"},
            "asof": asof.isoformat(),
            "confidence": "confirmed",
        })

    # 決算予定（監視銘柄のみ）
    try:
        start = asof.astimezone(NY).date()
        end = start + timedelta(days=cfg["earnings_lookahead_days"])
        cal = _get("/calendar/earnings", **{"from": start.isoformat(), "to": end.isoformat()})
        watch = set(cfg["watchlist"])
        for e in cal.get("earningsCalendar", []):
            if e.get("symbol") not in watch:
                continue
            timing = {"bmo": "米国市場の取引開始前", "amc": "米国市場の取引終了後"}.get(e.get("hour"), "時間未定")
            fields = {
                "symbol": e["symbol"],
                "date": _date_ja(e["date"]),
                "timing": timing,
            }
            summary = f"決算予定: {e['symbol']} 米国時間{e['date']} {timing}"
            if e.get("epsEstimate") is not None:
                fields["eps_estimate"] = f"${float(e['epsEstimate']):.2f}"
                summary += f", 予想EPS {e['epsEstimate']}"
            if e.get("revenueEstimate"):
                fields["revenue_estimate"] = fmt.usd_amount_ja(float(e["revenueEstimate"]))
                summary += f", 予想売上高 {e['revenueEstimate']}USD"
            facts.append({
                "key": f"stock:{e['symbol']}:earnings:{e['date']}",
                "kind": "stock",
                "category": "earnings_upcoming",
                "summary": summary,
                "fields": fields,
                "raw": {"date": e["date"], "eps_estimate": e.get("epsEstimate")},
                "refresh": None,
                "source": {"name": f"{SOURCE_NAME}決算カレンダー", "url": f"https://finance.yahoo.com/quote/{e['symbol']}"},
                "asof": asof.isoformat(),
                "confidence": "estimate",
            })
    except requests.RequestException as e:
        log.warning("決算カレンダーの取得に失敗: %s", e)

    # 市場ニュース（見出しは話題発見の補助。本文に数字は使わせない）
    try:
        news = _get("/news", category="general")[: cfg["news_max"]]
        for n in news:
            t = datetime.fromtimestamp(n["datetime"], tz=timezone.utc)
            if asof - t > timedelta(hours=18):
                continue
            facts.append({
                "key": f"news:{n['id']}",
                "kind": "stock",
                "category": "news_headline",
                "summary": f"ニュース見出し（未検証・{n.get('source')}・{t.astimezone(JST):%m/%d %H:%M}）: {n['headline']}",
                "fields": {"source": n.get("source", ""), "time": fmt_jst(t)},
                "raw": {},
                "refresh": None,
                "source": {"name": n.get("source", "news"), "url": n.get("url", "")},
                "asof": t.isoformat(),
                "confidence": "headline",
            })
    except requests.RequestException as e:
        log.warning("ニュースの取得に失敗: %s", e)

    return facts


def refresh_change_pct(ref: dict) -> float | None:
    q = quote(ref["symbol"])
    return float(q["dp"]) if q else None
