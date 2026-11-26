from aiedit import pipeline


def test_unused_headline_does_not_need_human():
    w = {"template": "${{f5.symbol}} {{f42.oi}}",
         "facts": [{"id": "f5", "confidence": "confirmed"}, {"id": "f42", "confidence": "confirmed"},
                   {"id": "f31", "confidence": "headline"}]}
    assert not pipeline.needs_human(w)
    w["template"] += " {{f31.title}}"
    assert pipeline.needs_human(w)
