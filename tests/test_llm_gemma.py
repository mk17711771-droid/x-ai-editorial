from aiedit import llm


def test_gemma_body_has_no_system_or_json_mode():
    body = {"systemInstruction": {"parts": [{"text": "SYS"}]},
            "contents": [{"role": "user", "parts": [{"text": "USER"}]}],
            "generationConfig": {"temperature": 0.5, "responseMimeType": "application/json"}}
    g = llm._body_for("gemma-3-27b-it", body)
    assert "systemInstruction" not in g and "responseMimeType" not in g["generationConfig"]
    assert "SYS" in g["contents"][0]["parts"][0]["text"] and "USER" in g["contents"][0]["parts"][0]["text"]
    assert llm._body_for("gemini-flash-latest", body) is body


def test_settings_file_is_auto_mode():
    # X は全自動。ファイルが approval だと generate 以外のワークフローで承認依頼のLINEが出る（11/26）
    import yaml
    from aiedit.common import CONFIG_DIR
    assert yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8"))["mode"] == "auto"


def test_total_and_ijou_on_placeholder_rejected():
    from aiedit import checks
    facts = {f: {"id": f, "fields": {"coin": "BTC", "short_usd": "約7,000万ドル", "holders": "5人"}} for f in ("f28", "f30")}
    errs = checks.check_template("{{f30.coin}}の{{f30.holders}}も。合計{{f28.short_usd}}以上の売り", facts)
    assert any("以上" in e for e in errs) and any("合計" in e for e in errs)


def test_soxl_vs_soxx_with_dake_rejected():
    from aiedit import checks
    t = "半導体の現物SOXXが-0.50%と重い中で、$SOXL先物だけ+7.74%と火を噴いてる。"
    assert checks._leveraged_vs_underlying(t)


def test_news_post_must_use_headline():
    from aiedit import checks
    facts = {"f1": {"fields": {"headline": "GCEXがPerpを提供開始", "what": "x"}}, "f25": {"fields": {"volume": "約4.5億ドル"}}}
    assert checks.news_fact_missing("GCEXの動きを見るに…{{f25.volume}}", facts)
    assert not checks.news_fact_missing("{{f1.headline}}。{{f25.volume}}", facts)


def test_ratio_drop_and_move_need_data():
    from aiedit import checks
    facts = {"f28": {"id": "f28", "fields": {"coin": "BTC", "long_ratio": "7%"}}}
    errs = checks.check_template("${{f28.coin}}の反発の裏で、買い比率が{{f28.long_ratio}}まで落ち込んでる。", facts)
    assert any("比率が" in e for e in errs) and any("値動き" in e for e in errs)


def test_oi_growth_needs_change_of_same_fact():
    from aiedit import checks
    facts = {"f2": {"id": "f2", "fields": {"symbol": "MU", "change_pct": "-4.53%"}},
             "f9": {"id": "f9", "fields": {"symbol": "SKHX", "oi": "約3.8億ドル"}}}
    errs = checks.check_template("MUが{{f2.change_pct}}。建玉が{{f9.oi}}まで積み上がっている。", facts)
    assert any("建玉・出来高" in e for e in errs)


def test_hl_change_not_called_spot():
    from aiedit import checks
    facts = {"f16": {"id": "f16", "kind": "hl", "fields": {"venue": "Hyperliquid", "change_pct": "+0.40%", "oi_change_pct": "+58.2%"}}}
    errs = checks.check_template("建玉が{{f16.oi_change_pct}}。現物価格は{{f16.change_pct}}の小動き", facts)
    assert any("現物" in e for e in errs)


def test_us_close_stale_after_holiday(monkeypatch):
    from datetime import datetime, timezone
    from aiedit import pipeline, store
    monkeypatch.setattr(store, "read_json", lambda *a, **k: {"MU": {"2026-11-25": 1}})
    assert pipeline.us_close_stale(datetime(2026, 11, 27, 3, 15, tzinfo=timezone.utc))      # 感謝祭明け 11/27 12:15 JST
    assert not pipeline.us_close_stale(datetime(2026, 11, 26, 13, 0, tzinfo=timezone.utc))  # 11/25終値の15時間後
    monkeypatch.setattr(store, "read_json", lambda *a, **k: {"MU": {"2026-11-23": 1}})
    assert not pipeline.us_close_stale(datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc))  # 火曜の取引中（NY 12:00）


def test_tiny_trader_cohort_tracking_rejected():
    from aiedit import checks
    facts = {"f33": {"id": "f33", "fields": {"strict_n": "2人", "candidates": "300人"}}}
    errs = checks.check_template("上位{{f33.candidates}}のうち{{f33.strict_n}}。③絞り込まれた{{f33.strict_n}}のポジション移動", facts)
    assert any("個人の特定" in e for e in errs)


def test_single_coin_value_after_list_rejected():
    from aiedit import checks
    mk = lambda c, lev: {"fields": {"coin": c, "cohort": "安定上位トレーダー", "short_n": "5人", "avg_leverage": lev, "asof": "x"}}
    facts = {"f27": mk("BTC", "26.6倍"), "f28": mk("ETH", "20.9倍")}
    t = "・$BTC：{{f27.short_n}}\n・{{f28.coin}}：{{f28.short_n}}\n平均レバレッジ{{f27.avg_leverage}}で売ってる。"
    errs = checks.check_template(t, facts)
    assert any("銘柄名なし" in e for e in errs) and checks.trader_asof_missing(t, facts)


def test_oi_growth_not_supported_by_us_change():
    from aiedit import checks
    facts = {"f9": {"id": "f9", "fields": {"symbol": "SNDK", "oi": "約4,600万ドル", "us_change_pct": "+15.96%"}}}
    assert any("建玉・出来高" in e for e in checks.check_template("現物{{f9.us_change_pct}}。建玉も{{f9.oi}}まで膨らんでる", facts))


def test_weekend_long_loopholes():
    from aiedit import checks
    facts = {"f6": {"id": "f6", "kind": "hl", "fields": {"venue": "Hyperliquid", "week_change_pct": "+3.90%", "funding_annual": "-9.9%"}}}
    errs = checks.check_template("指数が週間で{{f6.week_change_pct}}を超える上昇。ショートポジションが積み上がる。②週間{{f6.week_change_pct}}を記録したSP500の現物買い", facts)
    assert any("以上・超" in e for e in errs) and any("建玉・出来高" in e for e in errs) and any("現物" in e for e in errs)


def test_same_market_different_angle_allowed():
    from aiedit import checks
    mk = lambda k: {"kind": "hl", "key": k, "fields": {"coin": "SOXL", "symbol": "SOXL", "oi": "約1.1億ドル", "funding_annual": "+100%"}}
    facts = {"f12": mk("hl:xyz:SOXL:funding_extreme"), "f22": mk("hl:xyz:SOXL:hip3_top"), "f30": mk("hl:abc:SOXL:hip3_top")}
    t = "・{{f12.funding_annual}}\n・{{f22.oi}}"
    assert not any("別々の市場" in e for e in checks.check_template(t, facts))
    assert any("別々の市場" in e for e in checks.check_template("・{{f12.funding_annual}}\n・{{f30.oi}}", facts))


def test_note_enumeration_count_fixed():
    from aiedit import note_article
    t = "注目すべきポイントを3点にまとめます。\n①建玉\n②資金調達率\n\n## 次"
    assert "2点" in note_article.fix_enumeration_counts(t)
    ok = "3つの理由。\n①a\n②b\n③c"
    assert note_article.fix_enumeration_counts(ok) == ok
