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


def stock_change_chart(facts: list[dict], highlight: set[str] | None = None, weekly: bool = False) -> bytes:
    """監視銘柄の前日比（weekly=True なら1週間の値動き）を並べた横棒グラフ。投稿で取り上げた銘柄を強調する。"""
    cats, key = (("week_change",), "week_change_pct") if weekly else (("quote", "big_move"), "change_pct")
    quotes = sorted((f for f in facts if f["kind"] == "stock" and f["category"] in cats and key in f.get("raw", {})),
                    key=lambda f: f["raw"][key])
    highlight = highlight or set()
    names = [f["fields"]["symbol"] for f in quotes]
    vals = [f["raw"][key] for f in quotes]

    fig = plt.figure(figsize=(16, 9), facecolor=BG)
    label = quotes[-1]["fields"]["asof"] if quotes else ""
    session = label.replace("米国", "米国時間 ") if "終値" in label else "取引時間中"
    if weekly:
        period = quotes[-1]["fields"].get("period", "") if quotes else ""
        fig.text(0.04, 0.9, "米国株ウォッチリスト  ・  1週間の値動き", color=TEXT, fontsize=30, fontweight="bold")
        fig.text(0.04, 0.852, f"5取引日前の終値からの騰落率  ・  {period}", color=MUTED, fontsize=16)
    else:
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
    coins = [c for c in snap["coins"] if c["long_n"] + c["short_n"] + c.get("hedge_n", 0) >= 2][:10][::-1]
    names = [c["coin"].split(":")[-1] for c in coins]
    longs = [c["long_n"] for c in coins]
    shorts = [c["short_n"] for c in coins]
    hedges = [c.get("hedge_n", 0) for c in coins]

    fig = plt.figure(figsize=(16, 9), facecolor=BG)
    fig.text(0.04, 0.9, "Hyperliquid  ・  安定上位トレーダーの保有", color=TEXT, fontsize=30, fontweight="bold")
    fig.text(0.04, 0.852, f"60日以上プラス・売買が多すぎない・大勝ち1回に頼らない・直近1週間に売買あり の{snap['traders']}人のポジション（人数）",
             color=MUTED, fontsize=15)
    ax = fig.add_axes([0.1, 0.13, 0.84, 0.66], facecolor=BG)
    ax.barh(names, longs, color=UP, label="買い（ロング）")
    ax.barh(names, [-s for s in shorts], color=DOWN, label="売り（ショート）")
    ax.barh(names, [-h for h in hedges], left=[-s for s in shorts], color=ACCENT, alpha=0.75,
            label="ヘッジの売り（現物を保有）")
    # 左（売り）と右（買い）で必要な幅が違うので、それぞれの最大に合わせる（買い0人のとき右が空白にならない）
    left = max([s + h for s, h in zip(shorts, hedges)] + [1])
    right = max(longs + [left * 0.25])
    for i, (lo, sh, he) in enumerate(zip(longs, shorts, hedges)):
        total = lo + sh
        ax.text(lo + left * 0.015, i, f"買い{lo}人", va="center", color=TEXT, fontsize=12)
        left_label = f"売り{sh}人" + (f"＋ヘッジ{he}人" if he else "")
        ax.text(-sh - he - left * 0.015, i, left_label, va="center", ha="right", color=TEXT, fontsize=12)
        # 方向に賭けたポジションの買い比率を、右端の列にそろえて表示する（ヘッジは除く）
        ratio = f"買い比率 {lo / total * 100:.0f}%" if total else "ヘッジのみ"
        ax.text(1.0, i, ratio, va="center", ha="right", transform=ax.get_yaxis_transform(),
                color=MUTED if not total else (UP if lo >= sh else DOWN), fontsize=13, fontweight="bold")
    ax.set_xlim(-left * 1.6, right * 1.3 + left * 0.45)
    ax.axvline(0, color=GRID, linewidth=1)
    ax.set_xticks([])
    ax.tick_params(colors=MUTED, labelsize=13)
    for t in ax.get_yticklabels():
        if t.get_text() in highlight:
            t.set_color(TEXT)
            t.set_fontweight("bold")
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.legend(loc="lower center", bbox_to_anchor=(0.4, -0.12), ncol=3, frameon=False, labelcolor=TEXT, fontsize=12)
    _footer(fig, "Hyperliquid公開ランキング（個人は特定していません）", datetime.fromisoformat(snap["at"]))
    return _to_png(fig)


def funnel_chart(funnel: list[dict], asof: datetime) -> bytes:
    """上位トレーダーの絞り込み（条件を重ねるごとに残る人数）。1色の横棒で、人数を棒の右に直接書く。"""
    steps = funnel[::-1]
    names = [f["step"] for f in steps]
    ns = [f["n"] for f in steps]
    fig = plt.figure(figsize=(16, 9), facecolor=BG)
    fig.text(0.04, 0.9, "Hyperliquid ・ 上位トレーダーを「真似できるか」で絞り込むと", color=TEXT, fontsize=28, fontweight="bold")
    fig.text(0.04, 0.852, "ランキング上位の候補から、条件を1つずつ重ねたときに残る人数（過去の成績です。今後も同じとは限りません）",
             color=MUTED, fontsize=14)
    ax = fig.add_axes([0.27, 0.13, 0.66, 0.68], facecolor=BG)
    ax.barh(range(len(ns)), ns, color=ACCENT, height=0.62)
    top = max(ns + [1])
    for i, n in enumerate(ns):
        ax.text(n + top * 0.01, i, f"{n}人", va="center", color=TEXT, fontsize=14, fontweight="bold")
    ax.set_yticks(range(len(ns)), names)
    ax.set_xlim(0, top * 1.12)
    ax.set_xticks([])
    ax.tick_params(colors=MUTED, labelsize=13, length=0)
    for sp in ax.spines.values():
        sp.set_visible(False)
    _footer(fig, "Hyperliquid公開ランキング・ポジション（個人は特定していません）", asof)
    return _to_png(fig)


def build_for_draft(draft: dict, all_facts: list[dict] | None = None) -> bytes | None:
    """下書きの主役の材料に合わせてチャートを作る。作れなければ None（画像なしで投稿する）。"""
    from . import collect_hyperliquid, store

    funnel = next((f for f in draft["facts"] if f.get("category") == "trader_funnel"), None)
    if funnel and funnel.get("raw", {}).get("funnel"):
        return funnel_chart(funnel["raw"]["funnel"], datetime.fromisoformat(funnel["asof"]))
    smart = [f for f in draft["facts"] if f.get("category", "").startswith("smart_money")]
    if smart:
        snap = store.read_json("traders/latest.json")
        return smart_money_chart(snap, {f["fields"]["coin"] for f in smart}) if snap else None
    main = next((f for f in draft["facts"] if f.get("refresh")), None)
    if not main:
        # 週の振り返り（米国株の1週間の値動き）は refresh がないので図なしになっていた（11/8 12:16 ARMの長文）
        weekly = {f["fields"]["symbol"] for f in draft["facts"] if f.get("category") == "week_change" and f["kind"] == "stock"}
        if weekly and all_facts:
            return stock_change_chart(all_facts, highlight=weekly, weekly=True)
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
