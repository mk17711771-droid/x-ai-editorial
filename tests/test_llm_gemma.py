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
