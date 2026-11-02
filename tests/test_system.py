"""外部APIを使わずに、チェック機能と編集部の一連の流れを検証する。"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from aiedit import checks, collect_hyperliquid, line, llm, pipeline, store, xapi
from aiedit.common import settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-26T01:20:00+00:00")  # 10:20 JST（月曜）
    monkeypatch.delenv("BOT_ENABLED", raising=False)
    settings.cache_clear()
    sent = []
    monkeypatch.setattr(line, "push", lambda msgs: sent.append(msgs) or True)
    return sent


FACTS = {
    "f1": {"id": "f1", "key": "hl:BTC:funding_extreme", "kind": "hl", "confidence": "confirmed",
           "fields": {"coin": "BTC", "funding_annual": "+62.3%", "change_pct": "+4.10%", "oi": "約45.2億ドル",
                      "asof": "日本時間10/26(月) 10:05時点"},
           "raw": {"change_pct": 4.1}, "refresh": {"type": "hl_coin", "coin": "BTC", "dex": ""},
           "source": {"name": "Hyperliquid公式API", "url": "x"}},
}


# ---------------------------------------------------------------- チェック機能

def test_weighted_length_counts_japanese_as_two():
    assert checks.weighted_length("abc") == 3
    assert checks.weighted_length("株価") == 4


def test_template_rejects_invented_numbers():
    ok = "{{f1.coin}}の資金調達率は年率{{f1.funding_annual}}。24時間でロングが過熱しています。"
    assert checks.check_template(ok, FACTS) == []
    bad = "{{f1.coin}}の資金調達率は年率62%。"
    assert any("数字" in e for e in checks.check_template(bad, FACTS))
    fullwidth = "{{f1.coin}}が５％上昇。"
    assert checks.check_template(fullwidth, FACTS)
    kanji = "{{f1.coin}}の建玉は三割増えた。"
    assert checks.check_template(kanji, FACTS)
    missing = "{{f2.coin}}が{{f1.nope}}"
    errs = checks.check_template(missing, FACTS)
    assert any("f2" in e for e in errs) and any("nope" in e for e in errs)


def test_text_checks_banned_url_mention_length():
    base = "BTCの資金調達率が年率+62.3%まで上昇。ロングがショートに支払う状態が続いており、建玉も約45.2億ドルと高水準。過熱感が強まっている可能性があります。"
    assert checks.check_text(base, "t") == []
    assert any("禁止表現" in e for e in checks.check_text(base + "今すぐ買うべき", "t"))
    assert any("URL" in e for e in checks.check_text(base + " https://example.com", "t"))
    assert any("メンション" in e for e in checks.check_text(base + " @someone", "t"))
    assert any("上限" in e for e in checks.check_text(base * 3, "t"))
    assert any("短すぎ" in e for e in checks.check_text("BTC上昇", "t"))


def test_similarity_and_topic_cooldown():
    text = "BTCの資金調達率が年率+62.3%まで上昇。ロングがショートに支払う状態が続いており、建玉も約45.2億ドルと高水準。過熱感が強まっている可能性があります。"
    store.record_post({"tweet_id": "1", "posted_at": "2026-10-26T00:00:00+00:00", "text": text, "topic_key": "hl:BTC:funding_extreme"})
    # 数字だけ変えた同じ構文は類似と判定
    errs = checks.check_text(text.replace("62.3", "70.1").replace("45.2", "48.0"), "other")
    assert any("類似" in e for e in errs)
    errs = checks.check_text("ETHの建玉が24時間で急増しています。一方で価格はほぼ横ばいで、新規のショートが積み上がっている可能性があります。今夜の米国市場の動きに注目です。", "hl:BTC:funding_extreme")
    assert any("同じ話題" in e for e in errs)


# ---------------------------------------------------------------- 一連の流れ

MARKETS = [
    {"coin": "BTC", "dex": "", "mark": 111000.0, "change_pct": 4.1, "oi_usd": 4.52e9, "vol_usd": 3.1e9, "funding_annual_pct": 62.3},
    {"coin": "HYPE", "dex": "", "mark": 41.2, "change_pct": -9.5, "oi_usd": 1.2e9, "vol_usd": 8e8, "funding_annual_pct": -12.0},
    {"coin": "xyz:NVDA", "dex": "xyz", "mark": 182.3, "change_pct": 2.2, "oi_usd": 6e7, "vol_usd": 9e7, "funding_annual_pct": 8.0},
    {"coin": "TINY", "dex": "", "mark": 0.01, "change_pct": 80.0, "oi_usd": 1e5, "vol_usd": 1e5, "funding_annual_pct": 500.0},
]


def fake_llm(system: str, user: str) -> dict:
    if "編集長AIです" in system:
        facts = json.loads(user.split("# 材料 (facts)\n")[1].split("\n\n# ")[0])
        fid = next(f["id"] for f in facts if f["key"] == "hl:BTC:funding_extreme")
        return {"candidates": [
            {"fact_ids": [fid], "topic_key": "hl:BTC:funding_extreme", "angle": "ロング過熱",
             "scores": {"novelty": 18, "interest": 18, "evidence": 18, "utility": 16, "insight": 16}, "total": 99},
            {"fact_ids": [fid], "topic_key": "low", "angle": "x",
             "scores": {"novelty": 10, "interest": 10, "evidence": 10, "utility": 10, "insight": 10}, "total": 50},
        ]}
    fid = json.loads(user.split("# 材料（この差し込み記号だけ使える）\n")[1].split("\n\n# ")[0])[0]["id"]
    return {"variants": [
        {"style": "速報型", "template": f"{{{{{fid}.coin}}}}の資金調達率が年率62%に。"},  # 数字を捏造 → 破棄される
        {"style": "数値分析型", "template": (
            f"{{{{{fid}.coin}}}}の資金調達率が年率{{{{{fid}.funding_annual}}}}まで上昇（{{{{{fid}.asof}}}}）。"
            f"ロングがショートに支払う状態で、建玉は{{{{{fid}.oi}}}}。価格は24時間で{{{{{fid}.change_pct}}}}。"
            "買いの偏りが強く、急な巻き戻しが起きやすい水準の可能性があります。")},
    ], "best": 0}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(collect_hyperliquid, "fetch_markets", lambda: [dict(m) for m in MARKETS])
    monkeypatch.setattr(llm, "generate_json", fake_llm)
    posted = []
    monkeypatch.setattr(xapi, "post", lambda text, reply_to=None, media_ids=None, quote_id=None: posted.append(text) or "999")
    monkeypatch.setattr(collect_hyperliquid, "refresh_change_pct", lambda ref: 4.3)
    monkeypatch.setattr(collect_hyperliquid, "candles", lambda coin, start, end, interval="1h": FAKE_CANDLES)
    monkeypatch.setattr(xapi, "upload_image", lambda png: "m1")
    return posted


FAKE_CANDLES = [{"t": 1793000000000 + i * 3600_000, "c": str(100000 + i * 50), "v": "120"} for i in range(168)]


def test_generate_creates_safe_draft_and_asks_approval(wired, isolated):
    drafts = pipeline.generate()
    assert len(drafts) == 1  # 50点の候補は除外
    d = drafts[0]
    assert d["id"] == "20261026-1000-1"
    assert "+62.3%" in d["text"] and "約45.2億ドル" in d["text"]
    assert d["style"] == "数値分析型"  # 数字を捏造した案は破棄された
    assert len(isolated) == 1 and isolated[0][0]["type"] == "flex"
    assert wired == []  # 承認制なので投稿はまだ
    # 小型銘柄は材料にしない
    facts = store.read_json("facts/20261026-1000.json")
    assert not any("TINY" in f["key"] for f in facts)
    # 同じ枠は二重に生成しない
    assert pipeline.generate() == []


def test_approve_posts_once(wired, isolated):
    d = pipeline.generate()[0]
    assert pipeline.publish(d["id"], "approve") == "posted"
    assert len(wired) == 1
    assert store.posts()[0]["tweet_id"] == "999"
    assert pipeline.publish(d["id"], "approve") == "posted"  # 処理済みなので再投稿しない
    assert len(wired) == 1
    assert len(store.read_jsonl("decisions.jsonl")) == 1


def test_recheck_blocks_stale_draft(wired, isolated, monkeypatch):
    d = pipeline.generate()[0]
    monkeypatch.setattr(collect_hyperliquid, "refresh_change_pct", lambda ref: -1.0)
    assert pipeline.publish(d["id"], "approve") == "stale"
    assert wired == []


def test_expired_draft_not_posted(wired, isolated, monkeypatch):
    d = pipeline.generate()[0]
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-26T05:00:00+00:00")
    assert pipeline.publish(d["id"], "approve") == "expired"
    assert wired == []


def test_kill_switch_and_auth_error_halt(wired, isolated, monkeypatch):
    monkeypatch.setenv("BOT_ENABLED", "false")
    with pytest.raises(pipeline.Halted):
        pipeline.generate()
    monkeypatch.delenv("BOT_ENABLED")
    d = pipeline.generate()[0]

    def boom(text, reply_to=None):
        raise xapi.XAuthError("401 Unauthorized")
    monkeypatch.setattr(xapi, "post", lambda text, reply_to=None, media_ids=None, quote_id=None: boom(text))
    with pytest.raises(xapi.XAuthError):
        pipeline.publish(d["id"], "approve")
    assert store.state()["halted"] is True
    with pytest.raises(pipeline.Halted):
        pipeline.generate("2300", force=True)


def test_auto_mode_posts_without_approval(wired, isolated, monkeypatch):
    monkeypatch.setenv("APPROVAL_MODE", "false")
    settings.cache_clear()
    pipeline.generate()
    assert len(wired) == 1


def test_reject_records_reason(wired, isolated):
    d = pipeline.generate()[0]
    assert pipeline.publish(d["id"], "reject_inaccurate") == "rejected"
    assert store.read_jsonl("decisions.jsonl")[0]["action"] == "reject_inaccurate"
    assert wired == []


def test_render_removes_duplicated_words():
    assert checks.render("{{f1.asof}}時点、{{f1.coin}}は", FACTS) == "日本時間10/26(月) 10:05時点、BTCは"


def test_hip3_moves_checked_against_us_stock(monkeypatch):
    from aiedit import collect_stocks
    monkeypatch.setenv("FINNHUB_API_KEY", "x")
    quotes = {"EWZ": {"dp": 0.4}, "NVDA": {"dp": -11.0}}
    monkeypatch.setattr(collect_stocks, "quote", lambda sym: quotes.get(sym))
    markets = [
        {"coin": "xyz:EWZ", "dex": "xyz", "change_pct": -26.0},   # 米国株は+0.4% → データ異常として除外
        {"coin": "xyz:NVDA", "dex": "xyz", "change_pct": -10.0},  # 米国株も-11% → 残す
        {"coin": "xyz:FOO", "dex": "xyz", "change_pct": 12.0},    # 照合できない → 除外
        {"coin": "xyz:AAPL", "dex": "xyz", "change_pct": 1.0},    # 小さな値動きは照合不要
        {"coin": "BTC", "dex": "", "change_pct": -20.0},          # 暗号資産は対象外
    ]
    kept = [m["coin"] for m in collect_hyperliquid._verify_hip3_moves(markets)]
    assert kept == ["xyz:NVDA", "xyz:AAPL", "BTC"]


def test_force_regenerate_keeps_old_drafts(wired, isolated):
    first = pipeline.generate()[0]
    assert pipeline.publish(first["id"], "approve") == "posted"
    pipeline.generate(force=True)  # 同じ話題は投稿済みなので新しい下書きは作られない
    assert store.load_draft(first["id"])["status"] == "posted"  # 投稿済みの記録は上書きされない


def _moved_market(change_pct: float, funding: float = 62.3) -> dict:
    return {"coin": "BTC", "dex": "", "mark": 112000.0, "change_pct": change_pct, "oi_usd": 4.6e9,
            "vol_usd": 3.2e9, "funding_annual_pct": funding}


def test_stale_draft_is_rebuilt_with_latest_numbers(wired, isolated, monkeypatch):
    d = pipeline.generate()[0]
    monkeypatch.setattr(collect_hyperliquid, "refresh_change_pct", lambda ref: 6.0)
    monkeypatch.setattr(collect_hyperliquid, "refresh_market", lambda ref: _moved_market(6.0, 70.0))
    assert pipeline.publish(d["id"], "approve") == "refreshed"
    assert wired == []  # 作り直した案は、改めて承認されるまで投稿しない
    old = store.load_draft(d["id"])
    fresh = store.load_draft(old["refreshed_to"])
    assert old["status"] == "stale" and fresh["status"] == "pending"
    assert "+70.0%" in fresh["text"] and "+6.00%" in fresh["text"]
    assert fresh["template"] == d["template"]  # 文章の型は同じ
    assert isolated[-1][0]["type"] == "text" and isolated[-1][1]["type"] == "flex"
    # 作り直した案を承認すると投稿される
    monkeypatch.setattr(collect_hyperliquid, "refresh_change_pct", lambda ref: 6.1)
    assert pipeline.publish(fresh["id"], "approve") == "posted"
    assert len(wired) == 1 and "+70.0%" in wired[0]


def test_stale_draft_not_rebuilt_when_story_no_longer_holds(wired, isolated, monkeypatch):
    d = pipeline.generate()[0]
    # 資金調達率の過熱（ロング過熱）が解消していたら、同じ切り口では書けない
    monkeypatch.setattr(collect_hyperliquid, "refresh_change_pct", lambda ref: -1.0)
    monkeypatch.setattr(collect_hyperliquid, "refresh_market", lambda ref: _moved_market(-1.0, 10.0))
    assert pipeline.publish(d["id"], "approve") == "stale"
    assert "refreshed_to" not in store.load_draft(d["id"])
    assert wired == []


def test_hashtags_rejected_cashtags_limited():
    base = "HyperliquidのBTC無期限先物、資金調達率が年率+62.3%まで上がってる。ロングがショートに払い続けてる状態で、建玉も約45.2億ドルと高め。過熱っぽい👀"
    assert checks.check_text(base + " $BTC", "t") == []
    assert any("ハッシュタグ" in e for e in checks.check_text(base + " #BTC", "t"))
    assert any("キャッシュタグ" in e for e in checks.check_text(base + " $BTC $ETH $SOL", "t"))


def test_auto_mode_notifies_and_delete_removes_post(wired, isolated, monkeypatch):
    monkeypatch.setenv("APPROVAL_MODE", "false")
    settings.cache_clear()
    d = pipeline.generate()[0]
    assert len(wired) == 1
    assert isolated == []  # 通常の投稿はLINEに個別に知らせない（夜のまとめで1日1通。無料枠の節約）
    deleted = []
    monkeypatch.setattr(xapi, "delete", lambda tid: deleted.append(tid))
    assert pipeline.publish(d["id"], "delete") == "deleted"
    assert deleted == ["999"] and store.load_draft(d["id"])["status"] == "deleted"
    assert store.posts() == [] and len(store.posts(include_deleted=True)) == 1  # 削除した投稿は分析・まとめの対象外
    assert pipeline.publish(d["id"], "delete") == "deleted"  # 二重に押しても再削除しない
    assert deleted == ["999"]



def test_post_includes_chart_image(wired, isolated, monkeypatch):
    sent = {}
    monkeypatch.setattr(xapi, "post", lambda text, reply_to=None, media_ids=None, quote_id=None: sent.update(media=media_ids) or "999")
    uploads = []
    monkeypatch.setattr(xapi, "upload_image", lambda png: uploads.append(png) or "m1")
    d = pipeline.generate()[0]
    assert pipeline.publish(d["id"], "approve") == "posted"
    assert sent["media"] == ["m1"] and uploads[0][:8] == b"\x89PNG\r\n\x1a\n"
    assert store.posts()[-1]["has_image"] is True


def test_chart_failure_still_posts_text(wired, isolated, monkeypatch):
    def broken(png):
        raise xapi.XPostError("media 403")
    monkeypatch.setattr(xapi, "upload_image", broken)
    d = pipeline.generate()[0]
    assert pipeline.publish(d["id"], "approve") == "posted"
    assert store.state().get("halted") is not True  # 画像の失敗では止めない
    assert store.posts()[-1]["has_image"] is False


def test_stock_chart_renders():
    from aiedit import charts
    facts = [{"kind": "stock", "category": "quote", "fields": {"symbol": s, "asof": "米国10/26終値"},
              "raw": {"change_pct": c}, "asof": "2026-10-26T21:00:00+00:00"}
             for s, c in (("NVDA", 3.2), ("AMD", -1.4), ("TSM", 0.6))]
    assert charts.stock_change_chart(facts, highlight={"NVDA"})[:4] == b"\x89PNG"


def test_long_format_limits(monkeypatch):
    text = "【何が起きてるか】" + "HyperliquidのHIP-3株式先物で資金調達率が大きく偏っている。" * 12
    assert checks.check_text(text, "t", "long") == []
    assert any("上限" in e for e in checks.check_text(text, "t", "short"))
    assert any("短すぎ" in e for e in checks.check_text("短い考察", "t", "long"))


def test_cashtags_normalized_to_one():
    # X APIはキャッシュタグが2つ以上の投稿を403で拒否する
    out = checks.normalize_cashtags("$SOXXと$MU、$SKHXの話。$SOXXはまた上昇。価格は$496.27")
    assert out == "$SOXXとMU、SKHXの話。SOXXはまた上昇。価格は$496.27"
    assert any("キャッシュタグ" in e for e in checks.check_text("BTCの資金調達率が上がってる。過熱っぽい $BTC $ETH", "t"))


def test_cashtag_rejection_does_not_halt(monkeypatch):
    class R:
        status_code = 403
        text = '{"detail":"Posts are limited to a maximum of one cashtag ($SYMBOL)."}'
    monkeypatch.setattr(xapi.requests, "post", lambda *a, **k: R())
    monkeypatch.setattr(xapi, "_auth", lambda: None)
    with pytest.raises(xapi.XPostError):
        xapi.post("$BTC $ETH")


def test_render_fixes_double_dollar():
    facts = {"f1": {"fields": {"price": "$496.25", "symbol": "SOXX"}}}
    assert checks.render("${{f1.symbol}} 価格: ${{f1.price}}", facts) == "$SOXX 価格: $496.25"


def test_research_measures_posts_and_learn_updates_playbook(wired, isolated, monkeypatch):
    from aiedit import learn, research
    d = pipeline.generate()[0]
    pipeline.publish(d["id"], "approve")
    # 25時間後に反応を計測する
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-27T02:30:00+00:00")
    monkeypatch.setattr(xapi, "get_tweets", lambda ids: [{"id": i, "public_metrics": {
        "impression_count": 1500, "like_count": 30, "retweet_count": 5, "quote_count": 1,
        "reply_count": 2, "bookmark_count": 12}} for i in ids])
    monkeypatch.setattr(xapi, "me", lambda: {"public_metrics": {"followers_count": 1250, "following_count": 347, "tweet_count": 903}})
    assert research.measure() == 1
    assert research.measure() == 0  # 計測済みは二重に取らない
    assert store.read_jsonl("followers.jsonl")[0]["followers"] == 1250

    seen = {}
    def fake_learn(system, user):
        seen["report"] = json.loads(user)
        return {"playbook": ["冒頭で異変を一言で言い切る"], "hypotheses": ["a", "b", "c"], "next_week": "x"}
    monkeypatch.setattr(llm, "generate_json", fake_learn)
    learn.run(send_weekly_report=True)
    assert seen["report"]["measured_posts"] == 1
    assert "冒頭で異変を一言で言い切る" in learn.playbook_text()
    assert "週間運用レポート" in isolated[-1][0]["text"]
    # 運用ノートは次の編集・執筆のプロンプトに入る
    assert "冒頭で異変を一言で言い切る" in pipeline.with_playbook("base")


def test_watch_summary_feeds_editor(isolated):
    from aiedit import research
    store.append_jsonl("watch/20261026.jsonl", {"at": "2026-10-26T00:37:00+00:00", "at_jst": "10/26 09:37",
                                                "hl_funding": [["SKHX", 213.5]], "hl_movers": [["EWZ", -26.4]]})
    s = research.watch_summary()
    assert "SKHX +213.5%" in s and "EWZ -26.4%" in s


def test_trader_vetting_aggregation_facts_and_chart(isolated, monkeypatch):
    from aiedit import charts, traders
    monkeypatch.setattr(traders.time, "sleep", lambda s: None)
    now_ms = 1793000000000  # 2026-10-26付近
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-26T01:20:00+00:00")
    now_ms = int(datetime.fromisoformat("2026-10-26T01:20:00+00:00").timestamp() * 1000)
    day = 86_400_000
    rows = [{"ethAddress": f"0x{i:040x}", "accountValue": "500000",
             "windowPerformances": [["month", {"pnl": str(100000 - i), "roi": "0.2", "vlm": "1000000"}],
                                    ["allTime", {"pnl": "1", "roi": "1"}]]}
            for i in range(7)]
    # 高頻度の自動売買（月間の売買代金が口座の資産の1,000倍）は、ランキングの段階で外れる
    rows.append({"ethAddress": "0xhft", "accountValue": "500000",
                 "windowPerformances": [["month", {"pnl": "9e9", "roi": "9", "vlm": "5e8"}], ["allTime", {"pnl": "1"}]]})

    class R:
        status_code = 200
        def __init__(self, data): self.data = data
        def json(self): return self.data
        def raise_for_status(self): pass
    monkeypatch.setattr(traders.requests, "get", lambda url, timeout: R({"leaderboardRows": rows}))

    def steady_pnl(dip):  # 90日かけて増える損益。途中で dip だけ下がる
        pts = [[now_ms - (90 - d) * day, str(d * 1000)] for d in range(91)]
        pts[75][1] = str(75 * 1000 - dip)
        return pts
    # 0〜2: 合格 / 3: 高レバレッジ / 4: 超高頻度 / 5: 最近休んでいる / 6: 最大下落が0%に近い
    lev = {3: 5.0}
    fills_n = {4: 1500}
    idle = {5: 9}
    dip = {6: 0}
    held = {0: [("BTC", "1", "200000", "5000")], 1: [("BTC", "2", "300000", "100")], 2: [("BTC", "-1", "50000", "-10")]}

    def post(url, json, timeout):
        i = int(json["user"], 16)
        if json["type"] == "portfolio":
            pnl = steady_pnl(dip.get(i, 60000))
            acct = [[t, "500000"] for t, _ in pnl]
            return R([["allTime", {"pnlHistory": pnl, "accountValueHistory": acct}],
                      ["month", {"pnlHistory": pnl[-31:], "accountValueHistory": acct[-31:]}]])
        if json["type"] == "userFillsByTime":
            n = fills_n.get(i, 20)
            last = now_ms - idle.get(i, 1) * day
            return R([{"time": last - k * 3600_000, "closedPnl": "100"} for k in range(n)])
        return R({"marginSummary": {"accountValue": "500000", "totalNtlPos": str(500000 * lev.get(i, 1.0))},
                  "assetPositions": [{"position": {"coin": c, "szi": sz, "positionValue": v, "unrealizedPnl": u,
                                                   "leverage": {"value": 3}}} for c, sz, v, u in held.get(i, [])]})
    monkeypatch.setattr(traders.requests, "post", post)

    cohort = traders.select_cohort(force=True)
    # 0〜2: すべて満たす（厳選）/ 3: 高レバレッジ・6: 最大下落0% は安定上位どまり / 4・5 は対象外
    assert len(cohort["members"]) == 5 and cohort["strict_n"] == 3
    assert cohort["reasons"] == {"厳選": 3, "安定": 2, "売買が多すぎないを満たさない": 1, "直近1週間に売買ありを満たさない": 1}
    assert [f["n"] for f in cohort["funnel"]] == [7, 7, 6, 6, 5, 4, 3, 3]
    snap = traders.snapshot()
    btc = next(c for c in snap["coins"] if c["coin"] == "BTC")
    assert snap["traders"] == 5 and btc["long_n"] == 2 and btc["short_n"] == 1
    funnel = next(x for x in traders.facts() if x["category"] == "trader_funnel")
    assert funnel["fields"]["strict_n"] == "3人" and funnel["fields"]["candidates"] == "7人"
    f = traders.facts()
    assert f[0]["fields"]["cohort"] == "安定上位トレーダー" and "60日以上プラス" in f[0]["summary"]
    assert "0x" not in json.dumps([x["fields"] for x in f])  # 投稿の材料にアドレスを入れない
    assert charts.smart_money_chart(snap, {"BTC"})[:4] == b"\x89PNG"
    # 1日後に、含み益だった買いを閉じていたら「利確とみられる」材料になる
    store.write_json("traders/20261025-10.json", snap)
    held[0] = []
    held[1] = []
    traders.snapshot()
    exits = [x for x in traders.facts() if x["category"] == "smart_money_exit"]
    assert exits and exits[0]["fields"]["closed_n"] == "2人"


def test_banned_trader_phrases():
    base = "HyperliquidのBTC無期限先物、資金調達率が年率+62.3%まで上がってる。ロングがショートに払い続けてる状態で、建玉も約45.2億ドルと高め。"
    assert any("最強" in e for e in checks.check_text(base + "この人が最強", "t"))


def test_note_article_generated_with_images_and_copy_link(wired, isolated, monkeypatch):
    from aiedit import note_article, traders
    monkeypatch.setenv("LINE_ADMIN_USER_ID", "Uabc")
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    store.write_json("traders/latest.json", {"at": "2026-10-26T01:00:00+00:00", "traders": 50, "with_positions": 40,
                                             "coins": [{"coin": "ETH", "long_n": 3, "short_n": 23, "long_usd": 1e7, "short_usd": 7e8,
                                                        "avg_leverage": 17.0, "pos_n": 26, "net_usd": -6.9e8}], "positions": {}})
    calls = []

    def fake(system, user):
        calls.append(user)
        facts = json.loads(user.split("# 材料（この差し込み記号だけ使える）\n")[1].split("\n\n# ")[0])
        eth = next(f["id"] for f in facts if f["category"] == "smart_money")
        body = (f"今週のHyperliquidで目立ったのは、上位トレーダーの売りへの偏りです。\n\n## いま何が起きているか\n\n"
                f"月間利益上位{{{{{eth}.top_n}}}}のうち、ETHを売っているのは{{{{{eth}.short_n}}}}です。\n\n"
                "![上位トレーダーの保有](chart:smart_money)\n\n" + "ただし、ヘッジ目的の売りも含まれます。" * 200)
        if len(calls) == 1:  # 1回目はわざと数字を書いて、直させる
            body = "上位五十人のうち、売りは23人でした。" + body
        return {"title": "上位トレーダーはなぜ売っているのか", "markdown": body, "hashtags": ["#Hyperliquid", "ETH"]}
    monkeypatch.setattr(llm, "generate_json", fake)
    meta = note_article.write("テスト")
    assert len(calls) == 2 and "前回の原稿で直すべき点" in calls[1]
    assert meta["title"] == "上位トレーダーはなぜ売っているのか" and meta["hashtags"] == ["Hyperliquid", "ETH"]
    md_text = store.read_text(f"note_articles/{meta['id']}/article.md")
    assert "▼ここに画像1（上位トレーダーの保有）を挿入" in md_text and "chart:" not in md_text
    assert "50人のうち、ETHを売っているのは23人" in md_text and "売買を勧めるものではありません" in md_text
    assert (store.DATA_DIR / f"note_articles/{meta['id']}/image1.png").read_bytes()[:4] == b"\x89PNG"
    msg = isolated[-1][0]["text"]
    assert "noteの記事ができました" in msg and f"/note/{meta['id']}?k=Uabc" in msg


def test_render_cleans_spacing_units_and_restatements():
    facts = {"f1": {"fields": {"long_n": "4人", "short_n": "20人", "top_n": "50人", "f": "+11.0%", "coin": "BTC"}}}
    assert checks.render("上位 {{f1.top_n}} のうち、買いは{{f1.long_n}} 人、資金調達率は{{f1.f}}%", facts) == \
        "上位50人のうち、買いは4人、資金調達率は+11.0%"
    assert checks.render("{{f1.coin}} の買いは", facts) == "BTC の買いは"  # 英字の後の空白は残す
    errors = checks.check_text("売りが20人ではなく23人になっている。" * 3, "t", "long")
    assert any("言い直し" in e for e in errors)
    from aiedit import fmt
    assert fmt.usd_amount_ja(2.0629e11) == "約2,063億ドル" and fmt.usd_amount_ja(3.39e9) == "約33.9億ドル"


def test_hedged_shorts_are_not_counted_as_bearish():
    from aiedit import traders
    pos = [{"coin": "HYPE", "side": "short", "size": 1000, "value": 78000, "upnl": 0, "leverage": 5},
           {"coin": "BTC", "side": "short", "size": 2, "value": 160000, "upnl": 0, "leverage": 5},
           {"coin": "ETH", "side": "short", "size": 10, "value": 23000, "upnl": 0, "leverage": 5}]
    traders.mark_hedges(pos, {"HYPE": 1200, "BTC": 2.0, "ETH": 1})  # BTCは現物UBTCを「BTC」として持つ
    assert [p["hedge"] for p in pos] == [True, True, False]
    agg = traders.aggregate([{"address": "a"}], {"a": pos})
    hype = next(c for c in agg["coins"] if c["coin"] == "HYPE")
    eth = next(c for c in agg["coins"] if c["coin"] == "ETH")
    assert hype["hedge_n"] == 1 and hype["short_n"] == 0 and eth["short_n"] == 1


# ---------------------------------------------------------------- note公開後の確認・改善会議・外部リサーチ

def _gen_article(article_id="20261027-0739"):
    store.write_json(f"note_articles/{article_id}/meta.json", {
        "id": article_id, "created_at": "2026-10-26T22:39:00+00:00", "title": "上位トレーダーはHYPEを売っている？",
        "images": [{"file": "image1.png", "label": "上位トレーダーの保有"}], "status": "ready"})
    store.write_text(f"note_articles/{article_id}/article.md",
                     "# 上位トレーダーはHYPEを売っている？\n\n安定上位トレーダー29人のうち、売りは15人です。\n\n"
                     "## いま何が起きているか\n資金調達率は+62.3%まで上がりました。" + "解説の文章です。" * 40 +
                     "\n\n---\n\n※上位トレーダーの数字は、公開データから算出した過去の成績です。")


def test_note_check_finds_paste_mistakes_and_notifies(monkeypatch, isolated):
    from aiedit import note_check
    _gen_article()
    monkeypatch.setattr(llm, "generate_json", lambda s, u: {"issues": []})
    monkeypatch.setattr(note_check, "published_list", lambda u: [
        {"key": "nabc", "name": "上位トレーダーはHYPEを売っている？", "publishAt": "2026-10-27T08:00:00+09:00", "likeCount": 3},
        {"key": "nold", "name": "昔の記事", "publishAt": "2024-05-01T08:00:00+09:00", "likeCount": 9},
    ])
    body = ("<p>安定上位トレーダー29人のうち、売りは16人です。</p><h2>いま何が起きているか</h2>"
            "<p>資金調達率は+62.3%まで上がりました。" + "解説の文章です。" * 40 + "</p>"
            "<p>**▼ここに画像1（上位トレーダーの保有）を挿入**</p>"
            "<p>※上位トレーダーの数字は、公開データから算出した過去の成績です。"
            "特定の銘柄の売買を勧めるものではありません。</p>")
    monkeypatch.setattr(note_check, "fetch_body", lambda k: {"name": "上位トレーダーはHYPEを売っている？", "body": body})
    res = note_check.run()
    assert [r["key"] for r in res] == ["nabc"]  # 確認開始日より前の記事は対象外
    issues = "\n".join(res[0]["issues"])
    assert "▼ここに画像" in issues and "太字の記号" in issues
    assert "画像が1枚の予定のところ0枚" in issues
    assert "16" in issues and "15" in issues  # 数字の食い違い
    assert isolated and "直したほうがよい点" in json.dumps(isolated, ensure_ascii=False)
    assert store.read_json("note_articles/20261027-0739/meta.json")["status"] == "published"
    # 2回目は同じ記事を確認しない
    assert note_check.run() == []


def test_note_check_silent_when_clean(monkeypatch, isolated):
    from aiedit import note_check
    _gen_article()
    monkeypatch.setattr(llm, "generate_json", lambda s, u: {"issues": []})
    monkeypatch.setattr(note_check, "published_list", lambda u: [
        {"key": "nok", "name": "上位トレーダーはHYPEを売っている？", "publishAt": "2026-10-27T08:00:00+09:00"}])
    body = ("<p>安定上位トレーダー29人のうち、売りは15人です。</p><h2>いま何が起きているか</h2>"
            "<p>資金調達率は+62.3%まで上がりました。" + "解説の文章です。" * 40 + "</p><figure><img src='x'></figure>"
            "<p>※上位トレーダーの数字は、公開データから算出した過去の成績です。特定の銘柄の売買を勧めるものではありません。</p>")
    monkeypatch.setattr(note_check, "fetch_body", lambda k: {"body": body})
    res = note_check.run()
    assert res[0]["issues"] == [] and isolated == []


def test_improve_meeting_updates_strategy_and_ledger(monkeypatch):
    from aiedit import improve, learn
    monkeypatch.setattr(llm, "generate_json", lambda s, u: {
        "role_reviews": {"編集長": "…"}, "evaluations": [], "decisions": "…",
        "actions": [{"owner": "投稿制作", "change": "長文の冒頭に一番大きな数字の異変を置く", "hypothesis": "…", "metric": "表示回数"},
                    {"owner": "社長", "change": "担当外は捨てる"}],
        "strategy": ["長文の冒頭1行目に異変を置く"], "note_strategy": ["見出しは問いかけにする"],
        "image_prefs": {"short": False, "evil": True}, "dev_backlog": ["図に前週比の矢印を足す"]})
    rec = improve.run(monthly=False)
    ledger = store.read_json("improvements.json")
    assert [a["id"] for a in ledger] == ["I001"] and ledger[0]["owner"] == "投稿制作"
    assert "異変を置く" in learn.playbook_text()  # 編集・執筆AIが毎回読む
    assert improve.image_prefs() == {"short": False, "long": True}
    assert "前週比の矢印" in store.read_text("improve/backlog.md")
    assert rec["kind"] == "weekly"
    # 2回目の会議で評価され、台帳が更新される
    monkeypatch.setattr(llm, "generate_json", lambda s, u: {
        "evaluations": [{"id": "I001", "verdict": "定着", "reason": "表示回数が増えた"}], "actions": []})
    improve.run(monthly=True)
    assert store.read_json("improvements.json")[0]["status"] == "定着"


def test_x_research_respects_monthly_budget(monkeypatch):
    from aiedit import sources
    calls = []
    monkeypatch.setattr(xapi, "search_recent", lambda q, n: calls.append(n) or (
        [{"id": "1", "text": "t", "author_id": "u", "public_metrics": {"like_count": 5}}] * n, {"u": {"username": "a"}}))
    monkeypatch.setattr(sources, "_cfg", lambda: {"x": {"enabled": True, "monthly_read_limit": 50, "per_query": 30,
                                                        "queries": ["a", "b", "c"], "accounts": [], "rotate": False}})
    sources.x_search()
    assert calls == [30, 20]  # 上限50件を超えて読まない
    assert sources.x_search() == []


def test_x_research_rotates_one_query_per_day(monkeypatch):
    from aiedit import sources
    seen = []
    monkeypatch.setattr(xapi, "search_recent", lambda q, n: seen.append(q) or ([], {}))
    monkeypatch.setattr(sources, "_cfg", lambda: {"x": {"enabled": True, "monthly_read_limit": 400, "per_query": 12,
                                                        "queries": ["a", "b", "c"], "accounts": []}})
    sources.x_search()
    assert len(seen) == 1


def test_orphan_unit_and_duplicate_asof_fixed():
    base = "Hyperliquidの上位300人を条件で絞ると見え方が変わるな👀\n\n"
    assert any("単位だけ" in e for e in checks.check_text(base + "日以上プラスは185人、直近の売買ありは29人ってこと。", "t"))
    assert not any("単位だけ" in e for e in checks.check_text(base + "60日以上プラスは185人、直近の売買ありは29人ってこと。", "t"))
    facts = {"f1": {"fields": {"asof": "11:19時点"}}}
    assert checks.render("建玉も膨らんでて{{f1.asof}}の時点でこの歪みは気になる", facts) == "建玉も膨らんでて11:19時点でこの歪みは気になる"


def test_relink_restores_copied_data_values_only():
    facts = {"f1": {"fields": {"asof": "日本時間10/27(火) 21:45時点", "price": "$496.25", "n": "29人"}}}
    t = checks.relink("【数字で見る】\n日本時間10/27(火) 21:45時点のデータ: 価格$496.25、{{f1.price}}", facts)
    assert t == "【数字で見る】\n{{f1.asof}}のデータ: 価格{{f1.price}}、{{f1.price}}"
    assert checks.check_template(t, facts) == []
    # 材料と一致しない数字は戻さない（捏造はチェックで弾く）
    assert checks.check_template(checks.relink("価格$500.00", facts), facts)


def test_slot_accepts_early_random_start(monkeypatch):
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-27T22:20:00+00:00")  # 07:20 JST（水曜）: 07:42枠の22分前
    settings.cache_clear()
    assert pipeline.pick_slot()["id"] == "0730"
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-27T22:00:00+00:00")  # 07:00 JST は早すぎる
    assert pipeline.pick_slot() is None


def test_health_finds_missed_slot_and_retries(monkeypatch):
    from aiedit import health
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-27T01:30:00+00:00")  # 10:30 JST（火曜）
    settings.cache_clear()
    store.record_post({"tweet_id": "1", "slot_key": "20261027-0730", "posted_at": "2026-10-26T23:30:00+00:00",
                       "posted_at_jst": "2026-10-27 08:30", "text": "t", "topic_key": "x"})
    called = []
    monkeypatch.setattr(health, "retry_slot", lambda: called.append(1) or True)
    monkeypatch.setattr(health, "failed_runs", lambda hours=6: [])
    rec = health.run()
    assert [i["slot"] for i in rec["issues"] if i["type"] == "missed_slot"] == []  # 10:17枠はまだ判定しない
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-27T02:10:00+00:00")  # 11:10 JST
    rec = health.run()
    assert [i["slot"] for i in rec["issues"] if i["type"] == "missed_slot"] == ["1000"]
    assert called and rec["actions"]


def test_late_slot_after_midnight_belongs_to_previous_day(monkeypatch):
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-27T15:15:00+00:00")  # 10/28(水) 00:15 JST
    settings.cache_clear()
    slot = pipeline.pick_slot()
    assert slot["id"] == "2300" and pipeline.slot_key(slot) == "20261027-2300"
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-28T14:00:00+00:00")  # 10/28(水) 23:00 JST は当日の枠
    assert pipeline.slot_key(pipeline.pick_slot()) == "20261028-2300"


def test_same_symbol_not_featured_twice_in_a_row():
    store.record_post({"tweet_id": "9", "slot_key": "x", "posted_at": "2026-10-26T00:00:00+00:00",
                       "posted_at_jst": "2026-10-26 09:00", "text": "$SKHX の資金調達率がすごい", "topic_key": "a"})
    text = "HIP-3で$SKHXの資金調達率が年率+130.1%まで跳ね上がってる。ロングがコストを払い続けてる歪み、かなり気になる👀"
    assert any("主役の銘柄" in e for e in checks.check_text(text, "b"))
    assert not any("主役の銘柄" in e for e in checks.check_text(text.replace("$SKHX", "$SNDK"), "b"))


def test_repair_fields_fixes_or_drops_bad_references():
    facts = {"f1": {"fields": {"total_oi": "約10億ドル", "hip3_share": "12%"}}}
    t, dropped = checks.repair_fields("建玉は{{f1.total_oi_}}です。出来高は{{f1.venues}}です。比率は{{f1.hip3_share}}。\n- {{f1.nope}}", facts)
    assert t == "建玉は{{f1.total_oi}}です。比率は{{f1.hip3_share}}。"
    assert len(dropped) == 2 and checks.check_template(t, facts) == []


def test_note_numbers_ignore_urls():
    from aiedit import note_check
    assert note_check._nums("フォローはこちら https://x.com/satoshikun_666 売りは15人") == {"15"}


def test_funnel_chart_renders():
    from aiedit import charts
    png = charts.funnel_chart([{"step": "審査した候補", "n": 300}, {"step": "60日以上プラス", "n": 219}],
                              datetime.fromisoformat("2026-10-28T00:00:00+00:00"))
    assert png[:4] == b"\x89PNG"


def test_note_reminder_until_published(monkeypatch, isolated):
    from aiedit import note_check
    store.write_json("note_articles/20261028-0917/meta.json", {"id": "20261028-0917", "title": "T", "status": "ready",
                                                               "created_at": "2026-10-28T00:17:00+00:00"})
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-28T02:00:00+00:00")  # 11:00 JST: まだ知らせない
    assert note_check.remind_unpublished() == []
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-28T03:30:00+00:00")  # 12:30 JST
    assert note_check.remind_unpublished() == ["20261028-0917"]
    assert note_check.remind_unpublished() == []  # 同じ時間帯に2回送らない
    meta = store.read_json("note_articles/20261028-0917/meta.json")
    meta["status"] = "published"
    store.write_json("note_articles/20261028-0917/meta.json", meta)
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-28T10:30:00+00:00")  # 19:30 JST: 公開済みなら送らない
    assert note_check.remind_unpublished() == []


def test_boilerplate_disclaimer_removed_from_posts():
    facts = {"f1": {"fields": {"funding_annual": "+62.0%"}}}
    t = "資金調達率が{{f1.funding_annual}}まで跳ね上がってる👀\n\n※情報共有であって売買の推奨ではありません"
    assert checks.render(t, facts) == "資金調達率が+62.0%まで跳ね上がってる👀"


def test_endings_vary():
    store.record_post({"tweet_id": "9", "slot_key": "x", "posted_at": "2026-10-26T00:00:00+00:00",
                       "posted_at_jst": "2026-10-26 09:00", "text": "資金調達率がすごいことになってる。\n\n気になるところだな👀", "topic_key": "a"})
    base = "Hyperliquidの$SOLの建玉が膨らんで、資金調達率もじわじわ上がってきてる。\n\n"
    assert any("絵文字" in e for e in checks.check_text(base + "これは注目だ😏", "b"))
    assert any("締めの言い回し" in e for e in checks.check_text(base + "ちょっと過熱気味だな", "b"))
    assert not any("締め" in e or "絵文字" in e for e in checks.check_text(base + "明日の決算で答え合わせ。", "b"))


def test_angle_family_and_week_change_from_closes():
    assert pipeline.angle_family("hl:xyz:SKHX:funding_extreme") == pipeline.angle_family("hl:xyz:SMSN:hip3_top") == "hl_flow"
    assert pipeline.angle_family("stock:SOXX:quote") == "stock"
    from aiedit import collect_stocks
    days = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27"]
    store.write_json("stock_closes.json", {"NVDA": {d: 100.0 + i * 2 for i, d in enumerate(days)}})
    f = collect_stocks.week_change_facts()[0]
    assert f["fields"]["week_change_pct"] == "+10.00%" and f["category"] == "week_change"


def test_markdown_stripped_and_cliches_not_repeated():
    facts = {"f1": {"fields": {"symbol": "SOXX", "change_pct": "+2.13%"}}}
    assert checks.render("`${{f1.symbol}}`が{{f1.change_pct}}", facts) == "$SOXXが+2.13%"
    store.record_post({"tweet_id": "9", "slot_key": "x", "posted_at": "2026-10-26T00:00:00+00:00",
                       "posted_at_jst": "2026-10-26 09:00", "text": "この歪み、地味だけどかなり重要", "topic_key": "a"})
    text = "Hyperliquidの$SOLの建玉が膨らんで、資金調達率もじわじわ上がってきてる。地味だけど面白い動き。"
    assert any("地味だけど" in e for e in checks.check_text(text, "b"))


def test_news_watch_verifies_and_queues(monkeypatch, isolated):
    from aiedit import news_watch
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-29T17:00:00+00:00")  # 02:00 JST でも熱いうちに出す
    monkeypatch.setattr(news_watch, "_dispatch_news_slot", lambda: True)
    monkeypatch.setattr(news_watch, "official_posts", lambda: [])
    monkeypatch.setattr(news_watch, "_corroborate", lambda items: items)
    monkeypatch.setattr(llm, "generate_json", lambda s, u: {"stories": [
        {"story_key": "ice-tradexyz", "headline": "ICEがtrade[XYZ]に出資", "summary_ja": "…", "importance": 5,
         "official": False, "sources": ["CoinDesk", "Bloomberg"], "fields": {"what": "提携と出資"}},
        {"story_key": "minor", "headline": "解説記事", "importance": 2, "sources": ["X"]}]})
    res = news_watch.run({"items": [{"title": "ICE teams up with trade.xyz", "source": "CoinDesk"}]})
    assert [s["story_key"] for s in res["stories"]] == ["ice-tradexyz"]
    assert "投稿を始めました" in res["actions"][0]
    assert store.read_json("news_facts.json")[0]["key"] == "news:ice-tradexyz"
    assert isolated and "速報をキャッチ" in json.dumps(isolated, ensure_ascii=False)
    # 同じ見出しは二度処理しない
    assert news_watch.run({"items": [{"title": "ICE teams up with trade.xyz", "source": "CoinDesk"}]})["new"] == 0
    assert any(f["category"] == "breaking_news" for f in pipeline.news_facts())
    # 3時間以内の同じ話題は出さない。3時間後も報道が続いていれば続報を出す
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-29T18:00:00+00:00")
    assert news_watch.run({"items": [{"title": "Druckenmiller backs trade.xyz", "source": "Bloomberg"}]})["stories"] == []
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-29T20:30:00+00:00")
    res = news_watch.run({"items": [{"title": "HYPE hits record open interest", "source": "CoinDesk"}]})
    assert res["stories"] == []  # 新しい事実がない続報は出さない


def test_reply_ideas_drop_unsupported_numbers(monkeypatch, isolated):
    from aiedit import reply_ideas, research
    monkeypatch.setattr(reply_ideas, "candidates", lambda: [
        {"id": "1", "author": "big", "followers": 30000, "age_h": 2, "text": "NVDAが+3.2%、半導体強い", "likes": 50,
         "replies": 5, "reposts": 3, "has_media": True}])
    monkeypatch.setattr(research, "watch_summary", lambda h=12: "HL資金調達率上位 SKHX +118.9%")
    monkeypatch.setattr(llm, "generate_json", lambda s, u: {"ideas": [
        {"id": "1", "reply": "HIP-3でもSKHXの資金調達率が+118.9%まで来てて、先物側も過熱気味です"},
        {"id": "1", "reply": "これ+50%いくと思います"}]})
    picks = reply_ideas.run()
    assert [p["reply"][:11] for p in picks] == ["HIP-3でもSKHX"]
    assert "今日の返信候補" in json.dumps(isolated, ensure_ascii=False)


def test_news_watch_saves_universe_baseline(monkeypatch):
    from aiedit import news_watch
    monkeypatch.setattr(news_watch, "official_posts", lambda: [])
    res = news_watch.run({"universe": ["xyz:NVDA", "BTC"], "universe_init": True, "items": []})
    assert store.read_json("hl_universe.json") == ["BTC", "xyz:NVDA"] and res["new"] == 0


def test_worker_ticks_match_cron():
    """Workerの起動条件が、Cron（wrangler.toml）の刻みで実際に当たるか（当たらない条件は一度も動かない）。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    js = (root / "worker/src/index.js").read_text(encoding="utf-8")
    toml = (root / "worker/wrangler.toml").read_text(encoding="utf-8")
    step = int(re.search(r'"\*/(\d+) \* \* \* \*"', toml).group(1))
    for mod, val in re.findall(r"getUTCMinutes\(\) % (\d+) === (\d+)", js):
        assert any(m % int(mod) == int(val) for m in range(0, 60, step)), f"% {mod} === {val} は一度も当たらない"
    for val in re.findall(r"getUTCMinutes\(\) === (\d+)", js):
        assert int(val) % step == 0, f"=== {val} は {step}分刻みでは当たらない"


def test_health_flags_silent_news_watch(monkeypatch):
    from aiedit import health

    class R:
        ok = True
        def __init__(self, body): self._b = body
        def json(self): return self._b
    monkeypatch.setattr(health.requests, "get", lambda *a, **k: R({"news_en": {"ok": True}, "official": {"ok": False, "error": "401"}}))
    monkeypatch.setattr(health, "_gh", lambda *a, **k: R({"workflow_runs": [{"created_at": "2026-10-25T00:00:00Z"}]}))
    types = {i["type"] for i in health.worker_checks()}
    assert types == {"worker_watch_broken", "news_watch_silent"}


def test_news_slot_only_uses_pending_news():
    store.write_json("news_facts.json", [
        {"key": "news:a", "status": "posted", "expires_at": "2026-12-31T00:00:00+00:00", "fields": {"headline": "A"}, "summary": "A"},
        {"key": "news:b", "status": "pending", "expires_at": "2026-12-31T00:00:00+00:00", "fields": {"headline": "B"}, "summary": "B"}])
    assert [f["key"] for f in pipeline.news_facts(pending_only=True)] == ["news:b"]
    assert len(pipeline.news_facts()) == 2


def test_name_fixes_applied():
    facts = {"f1": {"fields": {"x": "+1.0%"}}}
    assert checks.render("ドルッケンミラー氏がTrade[XYZ]に出資、{{f1.x}}", facts) == "ドラッケンミラー氏がtrade[XYZ]に出資、+1.0%"


def test_health_retries_announced_but_unposted_news(monkeypatch):
    from aiedit import health
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-29T19:00:00+00:00")
    store.write_json("news/stories.json", {"druck": {"at": "x", "last_post_at": "2026-10-29T18:26:00+00:00"}})
    store.write_json("news_facts.json", [{"key": "news:druck", "status": "pending", "expires_at": "2026-12-31T00:00:00+00:00",
                                          "fields": {}, "summary": ""},
                                         {"key": "news:same-as-posted", "status": "posted", "expires_at": "2026-12-31T00:00:00+00:00",
                                          "fields": {}, "summary": ""}])
    store.write_json("news/stories.json", {"druck": {"at": "x", "last_post_at": "2026-10-29T18:26:00+00:00"},
                                           "same-as-posted": {"at": "x", "last_post_at": "2026-10-29T17:47:00+00:00"}})
    sent = []
    monkeypatch.setattr(health, "_dispatch_news", lambda: sent.append(1) or True)
    actions = []
    issues = health.announced_not_posted(actions)
    assert [i["story"] for i in issues] == ["druck"] and sent and actions  # 投稿済みと記録された話題は出し直さない
    assert store.read_json("news_facts.json")[0]["status"] == "pending"
    assert health.announced_not_posted([]) and len(sent) == 1  # 出し直しは1回だけ


def test_nightly_digest(monkeypatch, isolated):
    from aiedit import digest
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-28T12:35:00+00:00")
    store.record_post({"tweet_id": "1", "slot_key": "20261028-0730", "posted_at": "2026-10-27T22:39:00+00:00",
                       "posted_at_jst": "2026-10-28 07:39", "text": "今朝の米国株は…\n・SOXX", "topic_key": "a"})
    store.record_post({"tweet_id": "2", "slot_key": "20261027-2030", "posted_at": "2026-10-27T12:05:00+00:00",
                       "posted_at_jst": "2026-10-27 21:05", "text": "夜の長文", "topic_key": "b"})
    store.write_json("metrics.json", {"2": {"24h": {"impressions": 69, "likes": 0, "bookmarks": 0}}})
    text = digest.run()
    assert "今日の投稿 1件" in text and "表示69" in text and len(isolated) == 1


def test_system_audit_flags_stale_parts(monkeypatch):
    from aiedit import health, line
    monkeypatch.setenv("AIEDIT_NOW", "2026-10-31T01:00:00+00:00")  # 10:00 JST（土曜）
    monkeypatch.setattr(line, "quota", lambda: {"used": 150, "limit": 200})
    types = {i["type"] for i in health.system_audit()}
    assert {"stale", "learn_missing", "note_article_missing", "line_quota", "followers_missing"} <= types


def test_quote_official_post_for_news():
    d = {"facts": [{"category": "breaking_news", "source": {"url": "https://x.com/tradexyz/status/2115802211305365564"}}]}
    assert pipeline._quote_target(d) == "2115802211305365564"
    d["facts"][0]["source"]["url"] = "https://x.com/someone/status/1"
    assert pipeline._quote_target(d) is None


def test_worker_watches_exchange_incidents():
    # 10/30 バイナンスのハッキング・出金停止を見逃した反省: 大手取引所の重大事件も見張る
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("worker/src/index.js").read_text(encoding="utf-8")
    block = src.split("const NEWS_QUERIES")[1].split("];")[0]
    assert "Binance" in block and "出金停止" in block


def test_news_slot_not_blocked_by_daily_limit(wired, isolated):
    # 10/30 バイナンスの速報が「1日の投稿上限（通常の8件）」で止まった反省: 速報は別枠で数える
    from aiedit.common import now_utc
    d = pipeline.generate()[0]
    for i in range(8):
        store.record_post({"tweet_id": f"t{i}", "slot_key": f"20261026-x{i}", "topic_key": f"x:{i}",
                           "posted_at": now_utc().isoformat(), "text": "x"})
    assert pipeline.publish(d["id"], "approve") == "skipped"
    news = {**d, "id": "20261026-news1200-1", "slot_key": "20261026-news1200", "status": "pending"}
    store.save_draft(news)
    assert pipeline.publish(news["id"], "approve") == "posted"


def test_worker_watches_market_shocks():
    # ハッキングや大きな発表は報道より先に値動きに出る。BTC等の急変も見張る
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("worker/src/index.js").read_text(encoding="utf-8")
    assert "const SHOCKS" in src and "BTC" in src.split("const SHOCKS")[1].split(";")[0]
    assert "急変" in Path(__file__).resolve().parents[1].joinpath("prompts/news_triage.md").read_text(encoding="utf-8")


def test_hot_searches_parsed(isolated, monkeypatch):
    from aiedit import sources
    xml = b"""<?xml version="1.0"?><rss xmlns:ht="https://trends.google.com/trending/rss"><channel>
    <item><title>\xe3\x83\x90\xe3\x82\xa4\xe3\x83\x8a\xe3\x83\xb3\xe3\x82\xb9</title><ht:approx_traffic>50000+</ht:approx_traffic>
    <ht:news_item><ht:news_item_title>Binance pauses withdrawals</ht:news_item_title></ht:news_item></item>
    </channel></rss>"""

    class R:
        content = xml
        def raise_for_status(self): pass
    monkeypatch.setattr(sources.requests, "get", lambda *a, **k: R())
    rows = sources.hot_searches()
    assert rows[0]["traffic"] == "50000+" and rows[0]["news"] == ["Binance pauses withdrawals"]
    assert "バイナンス（50000+）: Binance pauses withdrawals" in sources.hot_digest()


def test_note_article_uses_week_news(isolated):
    # noteはその週いちばん話題になった出来事を主役にする（10/30 noteの分析）
    from datetime import timedelta
    from aiedit import note_article
    from aiedit.common import now_utc
    store.write_json("news_facts.json", [
        {"key": "news:a", "asof": now_utc().isoformat(), "status": "posted", "fields": {"headline": "新しい話題"}, "summary": "s"},
        {"key": "news:old", "asof": (now_utc() - timedelta(days=9)).isoformat(), "status": "posted", "fields": {"headline": "古い"}, "summary": "s"},
    ])
    got = note_article._week_news_facts()
    assert [f["key"] for f in got] == ["news:a"] and got[0]["category"] == "news"


def test_news_same_event_and_list_triage(isolated, monkeypatch):
    # 10/30: 同じバイナンスの件を別の話題として扱った／AIが配列で返して見張りが止まった／見送りの材料で投稿した
    from aiedit import news_watch
    assert news_watch.same_event("バイナンスで出金を一時停止 影響額は約63億ドル", "バイナンスで史上最大規模の63億ドルハッキング")
    assert not news_watch.same_event("Hyperliquidで新しい市場が上場", "Hyperliquidの建玉が過去最高 HYPE")
    monkeypatch.setattr(news_watch.llm, "generate_json", lambda *a, **k: [{"story_key": "x"}, "bad"])
    monkeypatch.setattr(news_watch.store, "read_json", lambda *a, **k: [])
    assert news_watch.triage([], [], None) == [{"story_key": "x"}]


def test_news_slot_ignores_skipped_facts(isolated):
    from datetime import timedelta
    from aiedit.common import now_utc
    exp = (now_utc() + timedelta(hours=5)).isoformat()
    store.write_json("news_facts.json", [{"key": "news:a", "status": "skipped", "expires_at": exp, "fields": {}},
                                         {"key": "news:b", "status": "pending", "expires_at": exp, "fields": {}}])
    assert [f["key"] for f in pipeline.news_facts(pending_only=True)] == ["news:b"]


def test_near_duplicate_word_rejected():
    from aiedit import checks
    errs = checks.check_text("【速報】バイナンスで史上最大規模となるバイナンスがハッキングを受け、資金が流出した。中央の取引所のリスクが改めて意識される。", "news:x")
    assert any("近くで繰り返されている" in e for e in errs)


def test_stale_story_not_posted_as_breaking(isolated):
    # 10/31 1:11 前日のバイナンスの件を【速報】で出した反省: 最初の報道から3時間を過ぎた話題は速報枠で使わない
    from datetime import timedelta
    from aiedit.common import now_utc
    exp = (now_utc() + timedelta(hours=10)).isoformat()
    store.write_json("news_facts.json", [
        {"key": "news:old", "asof": (now_utc() - timedelta(hours=9)).isoformat(), "status": "posted", "expires_at": exp, "fields": {}},
        {"key": "news:old-3", "asof": now_utc().isoformat(), "status": "pending", "expires_at": exp, "fields": {}},
        {"key": "news:new", "asof": now_utc().isoformat(), "status": "pending", "expires_at": exp, "fields": {}},
    ])
    assert [f["key"] for f in pipeline.news_facts(pending_only=True)] == ["news:new"]
    from aiedit import news_watch
    assert news_watch.MAX_POSTS_PER_STORY == 1


def test_skipped_news_not_retried(isolated, monkeypatch):
    # 見送りにした話題（古いICEの件）を、見回りが「投稿されていない」として出し直さない
    from datetime import timedelta
    from aiedit import health
    from aiedit.common import now_utc
    t = (now_utc() - timedelta(minutes=40)).isoformat()
    store.write_json("news/stories.json", {"ice": {"at": t, "last_post_at": t, "posts": 1}})
    store.write_json("news_facts.json", [{"key": "news:ice-3", "status": "skipped", "fields": {}}])
    called = []
    monkeypatch.setattr(health, "_dispatch_news", lambda: called.append(1) or True)
    assert health.announced_not_posted([]) == [] and called == []


def test_old_event_not_announced(isolated, monkeypatch):
    from aiedit import news_watch
    monkeypatch.setattr(news_watch, "triage", lambda *a: [{"story_key": "ice-again", "headline": "古い話", "importance": 5,
                                                            "buzz_fit": 6, "in_scope": True, "fresh": False,
                                                            "sources": ["A", "B"]}])
    monkeypatch.setattr(news_watch, "_corroborate", lambda items: items)
    monkeypatch.setattr(news_watch, "official_posts", lambda: [])
    sent = []
    monkeypatch.setattr(news_watch.line, "notify", lambda m: sent.append(m))
    r = news_watch.run({"items": [{"title": "ICEの件の再報道", "source": "X"}]})
    assert r["stories"] == [] and sent == []


def test_breaking_means_first_on_the_net(isolated, monkeypatch):
    # 管理者の定義: 速報＝ネット上でいち早くキャッチした情報。ネットに出てから時間がたったものは速報にしない
    from datetime import timedelta
    from aiedit import checks, news_watch
    from aiedit.common import now_utc
    old = (now_utc() - timedelta(hours=7)).isoformat()
    monkeypatch.setattr(news_watch, "triage", lambda *a: [{"story_key": "late", "headline": "遅い話", "importance": 5, "buzz_fit": 6,
                                                            "in_scope": True, "fresh": True, "first_reported_at": old,
                                                            "sources": ["A", "B"]}])
    monkeypatch.setattr(news_watch, "_corroborate", lambda items: items)
    monkeypatch.setattr(news_watch, "official_posts", lambda: [])
    monkeypatch.setattr(news_watch.line, "notify", lambda m: None)
    assert news_watch.run({"items": [{"title": "遅れて見つけた記事", "source": "X"}]})["stories"] == []
    assert any("速報" in e for e in checks.check_text("【速報】バイナンスでハッキング、出金を一時停止している。中央の取引所の弱さが出た形。", "hl:BNB:oi"))


def test_note_check_ignores_title_numbers_and_captions():
    # 10/31 タイトルの数字・画像の説明文で、正しく貼られた記事に誤った指摘を送った
    from datetime import datetime
    from aiedit import note_check
    gen = {"md": "# 【歪み】HYPEは$89.27\n\n本文の数字は12.5%です。\n\n![HYPEの値動き](chart:hl:HYPE)\n\nこれまでの流れ。\n\n---\n免責",
           "images": ["a"], "created_at": "2026-10-31T07:44:00+09:00", "title": "【歪み】HYPEは$89.27"}
    text = note_check._plain("<p>本文の数字は12.5%です。</p><figure><img src='x'><figcaption>HYPEの値動き</figcaption></figure><p>これまでの流れ。</p>")
    assert "値動き\nこれまで" in text
    issues = note_check.review("【歪み】HYPEは$89.27", text + "\n売買を勧めるものではありません", 1, None, gen)
    assert not any("89.27" in i for i in issues)


def test_repeated_asof_stamp_collapsed():
    from aiedit import checks
    by_id = {"f1": {"fields": {"a": "-0.56%", "s": "10:27時点"}}, "f2": {"fields": {"a": "-1.73%", "s": "10:27時点"}}}
    out = checks.render("・SP500 {{f1.a}}（{{f1.s}}）\n・XYZ100 {{f2.a}}（{{f2.s}}）", by_id)
    assert out.count("10:27時点") == 1


def test_junk_latin_and_bad_cashtag_rejected():
    from aiedit import checks
    e1 = checks.check_text("値動きが一気に急変するリスクがあるacylglycerと推測している。上位の偏りは残ったまま。", "hl:x")
    assert any("意味不明な英字" in e for e in e1)
    e2 = checks.check_text("関連する$Intercontinental Exchange (ICE)の動きとともにHYPEへの注目が集まっている。", "news:x")
    assert any("銘柄記号ではない" in e for e in e2)
    assert not any("銘柄記号" in e for e in checks.check_text("$NVDAが下げる中、$xyz:NVDA の先物は強い。建玉も増えている。", "hl:x"))


def test_name_fix_xyz100():
    from aiedit import checks
    out = checks.render("・XYZ100（HIP-3） 価格{{f1.a}}\n・XYZ100 {{f1.a}}\n②主要指数であるXYZ100とSP500の価格", {"f1": {"fields": {"a": "$1"}}})
    assert "XYZ100" not in out and "ナスダック100 $1" in out


def test_meeting_gets_scope_and_deletion_reasons(isolated):
    # 11/1 月次会議が「管理者が削除したバイナンス関連はノイズ」と誤解して範囲を狭めた反省
    from aiedit import improve
    store.write_text("improve/incidents.md", "- 削除の理由: 重複と遅れた速報\n")
    mat = improve.materials(monthly=False)
    assert "重複" in "".join(mat["incidents_and_deletion_reasons"])
    assert "前提" in mat["account_scope_set_by_admin"]


def test_old_event_detected_by_earlier_reports(isolated, monkeypatch):
    # 11/1 9月に発表済みのKraken(Payward)の件を、韓国の新しい記事1本から「速報をキャッチ」と2回知らせた
    from datetime import timedelta
    from aiedit import news_watch
    from aiedit.common import now_utc
    old = (now_utc() - timedelta(days=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    monkeypatch.setattr(news_watch.sources, "news_items", lambda q, w, lang="ja": [
        {"title": "Kraken parent Payward plans regulated US perpetual futures on Hyperliquid", "published": old}])
    story = {"headline": "【速報】Kraken親会社Payward、Hyperliquidインフラで米国向け無期限先物へ参入", "summary_ja": ""}
    assert news_watch._reported_earlier(story)
    monkeypatch.setattr(news_watch.sources, "news_items", lambda q, w, lang="ja": [])
    assert not news_watch._reported_earlier(story)


def test_x_search_runs_with_small_per_query(isolated, monkeypatch):
    # 10/30〜11/1 per_query=8（APIの最低10件未満）で毎回止まり、同ジャンルの調査が3日止まった
    from aiedit import sources, xapi
    calls = []
    monkeypatch.setattr(xapi, "search_recent", lambda q, n: calls.append(n) or ([], {}))
    monkeypatch.setattr(sources, "_cfg", lambda: {"x": {"enabled": True, "per_query": 8, "monthly_read_limit": 250, "queries": ["a"]}})
    sources.x_search()
    assert calls == [10]


def test_followup_candidates_surface_to_inspection(isolated, monkeypatch):
    # 11/1 扱い済みのバイナンスの件の新しい動き（出金再開・FBI）を、自動の続報を止めた結果、黙って捨てていた
    from datetime import timedelta
    from aiedit import health, news_watch
    from aiedit.common import now_utc
    t = (now_utc() - timedelta(hours=10)).isoformat()
    store.write_json("news_facts.json", [{"key": "news:binance", "asof": t, "status": "posted", "summary": "",
                                          "fields": {"headline": "バイナンスで出金停止"}}])
    store.write_json("news/stories.json", {"binance": {"at": t, "posts": 1, "posted_or_queued": True, "last_post_at": t}})
    monkeypatch.setattr(news_watch, "triage", lambda *a: [{"story_key": "binance", "headline": "バイナンスが出金を再開",
                        "importance": 5, "buzz_fit": 5, "in_scope": True, "fresh": True, "new_development": True,
                        "sources": ["CoinDesk"]}])
    monkeypatch.setattr(news_watch, "_corroborate", lambda items: items)
    monkeypatch.setattr(news_watch, "official_posts", lambda: [])
    monkeypatch.setattr(news_watch, "_reported_earlier", lambda s: False)
    news_watch.run({"items": [{"title": "Binance resumes withdrawals", "source": "CoinDesk"}]})
    assert store.read_jsonl("news/followups.jsonl")[-1]["headline"] == "バイナンスが出金を再開"
    from aiedit import line
    monkeypatch.setattr(line, "quota", lambda: None)
    assert any(i["type"] == "followup_candidates" for i in health.system_audit())


def test_repeated_phrase_rejected():
    from aiedit import checks
    errs = checks.check_text("米上場企業が保有するビットコインの売却や担保設定を決議したを決議したってマジか。連鎖が不気味。", "news:x")
    assert any("2回続いている" in e for e in errs)
    assert not any("2回続いている" in e for e in checks.check_text("建玉が増えた。資金調達率も上がった。偏りが強い。", "hl:x"))


def test_note_promoted_on_x_once(isolated, monkeypatch):
    # 11/2 noteの記事の表示が27〜61どまり。Xのフォロワーをnoteへつなぐ導線がなかった
    from aiedit import note_check, xapi
    store.write_json("note_articles/20261031-0744/meta.json", {"id": "20261031-0744", "title": "【歪み】ICEの参入", "status": "published",
                     "check_ok": True, "note_key": "abc", "published_at": "2026-10-31T07:56:14+09:00", "created_at": "2026-10-31T07:44:00+09:00"})
    store.write_text("note_articles/20261031-0744/article.md", "# 【歪み】ICEの参入\n\n今週は伝統金融が動いた歴史的な1週間でした。\n\n## 見出し\n本文")
    posted = []
    monkeypatch.setattr(xapi, "post", lambda text, **k: posted.append(text) or "t1")
    meta = store.read_json("note_articles/20261031-0744/meta.json")
    note_check.promote(meta, "https://note.com/charisma_hl/n/abc")
    note_check.promote(store.read_json("note_articles/20261031-0744/meta.json"), "https://note.com/charisma_hl/n/abc")
    assert len(posted) == 1 and "https://note.com/charisma_hl/n/abc" in posted[0] and "【歪み】ICEの参入" in posted[0]
    assert store.posts()[-1]["topic_key"] == "note:20261031-0744"


def test_stale_close_contrast_rejected(monkeypatch):
    # 11/2 12:28・20:46 10/30の$INTCの終値と、今の24時間のナスダック100を対比する誤りを2回出した
    from datetime import datetime
    from aiedit import checks, common
    monkeypatch.setattr(common, "now_jst", lambda: datetime(2026, 11, 2, 20, 46, tzinfo=common.JST))
    bad = "$INTCが前日終値比-28.58%（米国10/30終値）と急落したのに、ナスダック100は24時間騰落率+0.45%と崩れていない。温度差が大きい。"
    assert any("対比" in e for e in checks.check_text(bad, "stock:INTC:big_move", "long"))
    ok = "$AMDが前日終値比-10.88%（米国11/1終値）と急落する一方、先物の24時間の資金調達率はマイナス。"
    assert not any("対比" in e for e in checks.check_text(ok, "stock:AMD:big_move", "long"))
