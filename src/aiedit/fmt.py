"""投稿に差し込む数値の表示形式。数値は必ずここを通して文字列化する。"""
from __future__ import annotations


def pct(x: float, digits: int = 2) -> str:
    """+3.21% / -0.50%"""
    return f"{x:+.{digits}f}%"


def usd_price(x: float) -> str:
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    if x >= 0.01:
        return f"${x:.4f}"
    return f"${x:.6f}"


def usd_amount_ja(x: float) -> str:
    """金額を日本語の単位で表示: 約12.3億ドル / 約4,500万ドル"""
    sign = "-" if x < 0 else ""
    a = abs(x)
    if a >= 1e12:
        return f"{sign}約{a / 1e12:.2f}兆ドル"
    if a >= 1e8:
        return f"{sign}約{a / 1e8:.1f}億ドル"
    if a >= 1e4:
        return f"{sign}約{a / 1e4:,.0f}万ドル"
    return f"{sign}約{a:,.0f}ドル"


def ratio_x(x: float) -> str:
    """倍率: 2.3倍"""
    return f"{x:.1f}倍"
