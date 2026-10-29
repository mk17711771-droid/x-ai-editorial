"""記録（過去最高値）の基準。Cloudflare Workerが10分おきに現在値と比べ、更新した瞬間に速報にする。"""
from __future__ import annotations

from datetime import datetime, timezone

from . import collect_hyperliquid, store
from .common import log, now_utc

COINS = ["HYPE", "BTC", "ETH", "SOL"]


def rebuild() -> dict:
    """日足の全期間から過去最高値（高値）を求めて data/records.json に保存する。"""
    rec = store.read_json("records.json", {}) or {}
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    for coin in COINS:
        try:
            cs = collect_hyperliquid.candles(coin, start, now_utc(), interval="1d")
        except Exception as e:  # noqa: BLE001 取れない銘柄は前回の値を使う
            log.warning("過去最高値を取得できません %s: %s", coin, e)
            continue
        if not cs:
            continue
        top = max(cs, key=lambda c: float(c["h"]))
        ath = float(top["h"])
        if ath > rec.get(coin, {}).get("ath", 0):
            rec[coin] = {"ath": ath, "at": datetime.fromtimestamp(top["t"] / 1000, timezone.utc).isoformat()}
    store.write_json("records.json", rec)
    return rec


def update(coin: str, price: float) -> None:
    """記録を更新したら基準を引き上げる（同じ記録で何度も速報しない）。"""
    rec = store.read_json("records.json", {}) or {}
    if price > rec.get(coin, {}).get("ath", 0):
        rec[coin] = {"ath": price, "at": now_utc().isoformat()}
        store.write_json("records.json", rec)
