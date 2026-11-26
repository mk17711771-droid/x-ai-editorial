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
