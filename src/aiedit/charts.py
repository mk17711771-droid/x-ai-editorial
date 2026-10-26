"""投稿に添付するチャート画像（実データのみから作る。AI生成画像は使わない）。

日本の読者向けに日本語表記。フォントは Noto Sans CJK JP（GitHub Actionsで導入）。
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from . import fmt  # noqa: E402
from .common import JST, WEEKDAYS_JA, settings  # noqa: E402


def _register_japanese_font() -> None:
    """Noto Sans CJK（apt の fonts-noto-cjk）を明示的に登録する。見つからなければ英字フォントのまま。"""
    import glob
    import logging

    from matplotlib import font_manager

    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    for path in sorted(glob.glob("/usr/share/fonts/**/NotoSansCJK-*.tt[cf]", recursive=True)):
        font_manager.fontManager.addfont(path)
    plt.rcParams["font.family"] = ["Noto Sans CJK JP", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


_register_japanese_font()

BG = "#0d1117"
PANEL = "#161b22"
TEXT = "#e6edf3"
MUTED = "#8b949e"
GRID = "#30363d"
UP = "#3fb950"
DOWN = "#f85149"
ACCENT = "#58a6ff"


def _usd(x: float) -> str:
    return fmt.usd_amount_ja(x).replace("約", "")


def _price(x: float) -> str:
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    return f"${x:.6g}"


def _footer(fig, source: str, asof: datetime) -> None:
    handle = settings()["account"]["handle"]
    fig.text(0.04, 0.035, f"@{handle}", color=TEXT, fontsize=15, fontweight="bold")
    d = asof.astimezone(JST)
    fig.text(0.96, 0.035, f"データ: {source}  ・  {d:%Y/%m/%d}({WEEKDAYS_JA[d.weekday()]}) {d:%H:%M} 日本時間",
             color=MUTED, fontsize=12, ha="right")


def _to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def hl_price_chart(fact: dict, candles: list[dict]) -> bytes:
    """Hyperliquid銘柄の価格（7日・1時間足）と出来高、主要指標のカード。"""
    raw = fact["raw"]
    name = fact["fields"]["coin"]
    hip3 = fact["refresh"].get("dex")
    change = raw["change_pct"]
    color = UP if change >= 0 else DOWN

    times = [datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).astimezone(JST) for c in candles]
    closes = [float(c["c"]) for c in candles]
    vols = [float(c["v"]) * float(c["c"]) for c in candles]

    fig = plt.figure(figsize=(16, 9), facecolor=BG)
    fig.text(0.04, 0.9, f"Hyperliquid  ・  {name}", color=TEXT, fontsize=30, fontweight="bold")
    fig.text(0.04, 0.852, "HIP-3 株式先物（無期限）" if hip3 else "無期限先物", color=MUTED, fontsize=16)
    fig.text(0.96, 0.9, _price(raw["mark"]), color=TEXT, fontsize=30, fontweight="bold", ha="right")
    fig.text(0.96, 0.852, f"24時間  {change:+.2f}%", color=color, fontsize=18, fontweight="bold", ha="right")

    ax = fig.add_axes([0.06, 0.34, 0.9, 0.47], facecolor=BG)
    ax.plot(times, closes, color=color, linewidth=2.4)
    ax.fill_between(times, closes, min(closes), color=color, alpha=0.12)
    ax.set_xlim(times[0], times[-1])
    lo, hi = min(closes), max(closes)
    pad = (hi - lo) * 0.08 or hi * 0.01
    ax.set_ylim(lo - pad, hi + pad)
    vx = fig.add_axes([0.06, 0.24, 0.9, 0.09], facecolor=BG, sharex=ax)
    vx.bar(times, vols, width=1 / 24 * 0.8, color=MUTED, alpha=0.6)
    for a in (ax, vx):
        a.grid(color=GRID, linewidth=0.6)
        a.tick_params(colors=MUTED, labelsize=12)
        for s in a.spines.values():
            s.set_visible(False)
    ax.tick_params(labelbottom=False)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: _price(v)))
    vx.set_yticks([])
    vx.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d", tz=JST))
    fig.text(0.06, 0.815, "直近7日間（1時間足）", color=MUTED, fontsize=12)

    stats = [("建玉（OI）", _usd(raw["oi_usd"])),
             ("24時間出来高", _usd(raw["vol_usd"])),
             ("資金調達率（年率）", f"{raw['funding_annual_pct']:+.1f}%")]
    if "us_change_pct" in raw:
        stats.append(("米国株（前日比）", f"{raw['us_change_pct']:+.2f}%"))
    w = 0.9 / len(stats)
    for i, (label, value) in enumerate(stats):
        x = 0.06 + i * w
        fig.patches.append(matplotlib.patches.FancyBboxPatch(
            (x, 0.1), w - 0.015, 0.1, boxstyle="round,pad=0.004,rounding_size=0.01",
            transform=fig.transFigure, facecolor=PANEL, edgecolor=GRID))
        fig.text(x + 0.015, 0.165, label, color=MUTED, fontsize=12)
        fig.text(x + 0.015, 0.118, value, color=TEXT, fontsize=20, fontweight="bold")

    _footer(fig, "Hyperliquid", datetime.fromisoformat(fact["asof"]))
    return _to_png(fig)


def stock_change_chart(facts: list[dict], highlight: set[str] | None = None) -> bytes:
    """監視銘柄の前日比を並べた横棒グラフ。投稿で取り上げた銘柄を強調する。"""
    quotes = sorted((f for f in facts if f["kind"] == "stock" and f["category"] in ("quote", "big_move")),
                    key=lambda f: f["raw"]["change_pct"])
    highlight = highlight or set()
    names = [f["fields"]["symbol"] for f in quotes]
    vals = [f["raw"]["change_pct"] for f in quotes]

    fig = plt.figure(figsize=(16, 9), facecolor=BG)
    label = quotes[-1]["fields"]["asof"] if quotes else ""
    session = label.replace("米国", "米国時間 ") if "終値" in label else "取引時間中"
    fig.text(0.04, 0.9, "米国株ウォッチリスト  ・  前日比", color=TEXT, fontsize=30, fontweight="bold")
    fig.text(0.04, 0.852, f"前日終値からの騰落率  ・  {session}", color=MUTED, fontsize=16)

    ax = fig.add_axes([0.1, 0.12, 0.84, 0.69], facecolor=BG)
    colors = [(UP if v >= 0 else DOWN) for v in vals]
    alphas = [1.0 if n in highlight or not highlight else 0.45 for n in names]
    bars = ax.barh(names, vals, color=colors)
    for b, a in zip(bars, alphas):
        b.set_alpha(a)
    for b, v, n in zip(bars, vals, names):
        ax.text(v + (0.08 if v >= 0 else -0.08), b.get_y() + b.get_height() / 2, f"{v:+.2f}%",
                va="center", ha="left" if v >= 0 else "right", color=TEXT if n in highlight else MUTED, fontsize=12)
    ax.axvline(0, color=GRID, linewidth=1)
    span = max((abs(v) for v in vals), default=1) * 1.25
    ax.set_xlim(-span, span)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.tick_params(colors=MUTED, labelsize=12)
    for t in ax.get_yticklabels():
        if t.get_text() in highlight:
            t.set_color(TEXT)
            t.set_fontweight("bold")
    for s in ax.spines.values():
        s.set_visible(False)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:+.0f}%"))

    asof = datetime.fromisoformat(quotes[-1]["asof"]) if quotes else datetime.now(timezone.utc)
    _footer(fig, "Finnhub", asof)
    return _to_png(fig)


def smart_money_chart(snap: dict, highlight: set[str] | None = None) -> bytes:
    """上位トレーダーの保有の偏り（銘柄ごとの買い・売りの人数）。"""
    highlight = highlight or set()
    coins = [c for c in snap["coins"] if c["long_n"] + c["short_n"] >= 2][:10][::-1]
    names = [c["coin"].split(":")[-1] for c in coins]
    longs = [c["long_n"] for c in coins]
    shorts = [c["short_n"] for c in coins]

    fig = plt.figure(figsize=(16, 9), facecolor=BG)
    fig.text(0.04, 0.9, "Hyperliquid  ・  安定上位トレーダーの保有", color=TEXT, fontsize=30, fontweight="bold")
    fig.text(0.04, 0.852, f"60日以上プラス・最大下落5〜15%・レバレッジ3倍以下などの条件を満たした{snap['traders']}人のポジション（人数）",
             color=MUTED, fontsize=15)
    ax = fig.add_axes([0.1, 0.13, 0.84, 0.66], facecolor=BG)
    ax.barh(names, longs, color=UP, label="買い（ロング）")
    ax.barh(names, [-s for s in shorts], color=DOWN, label="売り（ショート）")
    span = max(longs + shorts + [1]) * 1.25
    for i, (lo, sh) in enumerate(zip(longs, shorts)):
        total = lo + sh
        ax.text(lo + span * 0.01, i, f"買い{lo}人", va="center", color=TEXT, fontsize=12)
        ax.text(-sh - span * 0.01, i, f"売り{sh}人", va="center", ha="right", color=TEXT, fontsize=12)
        # 買い比率は右端の列にそろえて表示する
        ax.text(span * 1.55, i, f"買い比率 {lo / total * 100:.0f}%", va="center", ha="right",
                color=UP if lo >= sh else DOWN, fontsize=13, fontweight="bold")
    ax.set_xlim(-span, span * 1.6)
    ax.axvline(0, color=GRID, linewidth=1)
    ax.set_xticks([])
    ax.tick_params(colors=MUTED, labelsize=13)
    for t in ax.get_yticklabels():
        if t.get_text() in highlight:
            t.set_color(TEXT)
            t.set_fontweight("bold")
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.legend(loc="lower center", bbox_to_anchor=(0.35, -0.12), ncol=2, frameon=False, labelcolor=TEXT, fontsize=12)
    _footer(fig, "Hyperliquid公開ランキング（個人は特定していません）", datetime.fromisoformat(snap["at"]))
    return _to_png(fig)


def build_for_draft(draft: dict, all_facts: list[dict] | None = None) -> bytes | None:
    """下書きの主役の材料に合わせてチャートを作る。作れなければ None（画像なしで投稿する）。"""
    from . import collect_hyperliquid, store

    smart = [f for f in draft["facts"] if f.get("category", "").startswith("smart_money")]
    if smart:
        snap = store.read_json("traders/latest.json")
        return smart_money_chart(snap, {f["fields"]["coin"] for f in smart}) if snap else None
    main = next((f for f in draft["facts"] if f.get("refresh")), None)
    if not main:
        return None
    if main["kind"] == "hl":
        end = datetime.now(timezone.utc)
        candles = collect_hyperliquid.candles(main["refresh"]["coin"], end - timedelta(days=7), end)
        if len(candles) < 24:
            return None
        return hl_price_chart(main, candles)
    if all_facts:
        names = {f["fields"]["symbol"] for f in draft["facts"] if f["kind"] == "stock" and "symbol" in f["fields"]}
        return stock_change_chart(all_facts, highlight=names)
    return None
