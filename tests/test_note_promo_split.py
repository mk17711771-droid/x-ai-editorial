from aiedit import note_check


def test_split_keeps_parenthetical_period():
    ln = "資金調達率（手数料。買いが多いとプラスになる）は年率-64.8%まで低下しています。次の文"
    out = note_check.split_sentences(ln)
    assert out[0].startswith("資金調達率（") and out[0].endswith("低下しています。")
    assert out[1] == "次の文。"
