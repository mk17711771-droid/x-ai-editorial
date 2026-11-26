from aiedit import llm


def test_gemma_body_has_no_system_or_json_mode():
    body = {"systemInstruction": {"parts": [{"text": "SYS"}]},
            "contents": [{"role": "user", "parts": [{"text": "USER"}]}],
            "generationConfig": {"temperature": 0.5, "responseMimeType": "application/json"}}
    g = llm._body_for("gemma-3-27b-it", body)
    assert "systemInstruction" not in g and "responseMimeType" not in g["generationConfig"]
    assert "SYS" in g["contents"][0]["parts"][0]["text"] and "USER" in g["contents"][0]["parts"][0]["text"]
    assert llm._body_for("gemini-flash-latest", body) is body
