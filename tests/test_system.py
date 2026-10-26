"""外部APIを使わずに、チェック機能と編集部の一連の流れを検証する。"""
from __future__ import annotations

import json

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
        facts = json.loads(user.split("# 材料 (facts)\n")[1])
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
    monkeypatch.setattr(xapi, "post", lambda text, reply_to=None, media_ids=None: posted.append(text) or "999")
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
    monkeypatch.setattr(xapi, "post", lambda text, reply_to=None, media_ids=None: boom(text))
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
    notice = isolated[-1][0]
    assert notice["type"] == "flex" and "削除" in json.dumps(notice, ensure_ascii=False)
    deleted = []
    monkeypatch.setattr(xapi, "delete", lambda tid: deleted.append(tid))
    assert pipeline.publish(d["id"], "delete") == "deleted"
    assert deleted == ["999"] and store.load_draft(d["id"])["status"] == "deleted"
    assert pipeline.publish(d["id"], "delete") == "deleted"  # 二重に押しても再削除しない
    assert deleted == ["999"]



def test_post_includes_chart_image(wired, isolated, monkeypatch):
    sent = {}
    monkeypatch.setattr(xapi, "post", lambda text, reply_to=None, media_ids=None: sent.update(media=media_ids) or "999")
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


def test_cashtags_normalized_to_two():
    out = checks.normalize_cashtags("$SOXXと$MU、$SKHXの話。$SOXXはまた上昇。価格は$496.27")
    assert out == "$SOXXとMU、SKHXの話。SOXXはまた上昇。価格は$496.27".replace("$SOXXとMU", "$SOXXと$MU")


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
