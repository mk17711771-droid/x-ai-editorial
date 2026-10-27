"""投稿前の安全チェック。1つでも引っかかったら投稿しない。"""
from __future__ import annotations

import re
import unicodedata

from . import store
from .common import read_list, settings

PLACEHOLDER = re.compile(r"\{\{\s*(f\d+)\.(\w+)\s*\}\}")
DIGIT = re.compile(r"[0-9０-９]")
KANJI_QUANTITY = re.compile(r"[一二三四五六七八九十百千万億兆]+(?:割|%|％|倍|億|万|兆|ドル|円|ポイント|銘柄|ヶ月|か月|年)")
URL = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|io|xyz|jp|net|org)\b", re.I)
MENTION = re.compile(r"(?<![\w])@\w+")
HASHTAG = re.compile(r"[#＃]\w+")
# 「20人ではなく23人」「+6.19%ではなく+4.04%」のような、数字を言い直す不自然な文
RESTATE = re.compile(r"[0-9][0-9.,]*\s*(?:人|倍|%|％|ドル|円)?\s*ではなく\s*[+\-$約]?[0-9]")
CASHTAG = re.compile(r"\$[A-Za-z][A-Za-z0-9.]*")
# 「日以上プラス」のように、数字が抜けて単位だけ残った文（AIが数字を書けずに落としたもの）
ORPHAN_UNIT = re.compile(r"(?:^|[\s、。，,（(「：:・])(?:日|時間|週間|か月|ヶ月|人|倍|件)(?:以上|以下|以内|未満|前|間|ぶり)")


def weighted_length(text: str) -> int:
    """X（twitter-text v3）の文字数カウント。日本語など全角は2、半角英数は1。上限280。"""
    total = 0
    for ch in unicodedata.normalize("NFC", text):
        cp = ord(ch)
        if cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247:
            total += 1
        else:
            total += 2
    return total


def length_limits(fmt: str = "short") -> tuple[int, int, int]:
    """(最小文字数, 目安の最大文字数, 重み付き上限)"""
    if fmt == "long" and settings()["account"]["premium"]:
        # X Premiumの長文投稿（上限25,000）。読み切れる長さに抑える
        return 450, 1400, 25000
    # 短文はタイムラインで全文が見える長さ（重み付き280 = 日本語でおよそ140文字）
    return 60, 135, 280


UNITS = ("時点", "終値", "人", "倍", "%", "％", "ドル", "円")
TICKER = re.compile(r"^[A-Za-z][A-Za-z0-9.:\-]*$")
SPACES = " \u3000"


def _is_jp(ch: str) -> bool:
    return bool(ch) and ord(ch) > 0x7F


def render(template: str, facts: dict[str, dict]) -> str:
    out: list[str] = []
    pos = 0
    for m in PLACEHOLDER.finditer(template):
        value = facts[m.group(1)]["fields"][m.group(2)]
        before = template[pos:m.start()]
        # 日本語の直後の空白（「上位 50人」）は消す。ティッカー（英字）の前後の空白は残す
        stripped = before.rstrip(SPACES)
        if stripped != before and _is_jp(stripped[-1:]) and not TICKER.match(value):
            before = stripped
        out.append(before)
        out.append(value)
        pos = m.end()
        rest = template[pos:]
        rest_l = rest.lstrip(SPACES)
        # 値に含まれる単位をAIがもう一度書いた場合（「4人 人」「+11.0%%」）は、後ろの単位を消す
        for unit in UNITS:
            if value.endswith(unit) and rest_l.startswith(unit):
                rest_l = rest_l[len(unit):]
                pos += len(rest) - len(rest_l)
                rest = rest_l
                break
        # 数字の値の直後の空白（「50人 のうち」）は、続きが日本語なら消す
        if rest != rest_l and _is_jp(rest_l[:1]) and not TICKER.match(value):
            pos += len(rest) - len(rest_l)
    out.append(template[pos:])
    text = normalize_cashtags("".join(out).strip())
    # 差し込みデータとAIの文章で語が重なったもの（例:「時点時点」）を整える
    for dup, fixed in (("時点の時点", "時点"), ("時点時点", "時点"), ("終値終値", "終値"), ("約約", "約"), ("$$", "$")):
        text = text.replace(dup, fixed)
    return text


def normalize_cashtags(text: str, limit: int = 1) -> str:
    """キャッシュタグ（$NVDA）はX APIの制限で1投稿1つまで。最初に出てきた銘柄を1回だけ残し、それ以外は「$」を外す。"""
    kept: list[str] = []

    def sub(m: re.Match) -> str:
        tag = m.group(0)
        if tag not in kept and len(kept) < limit:
            kept.append(tag)
            return tag
        return tag[1:]
    return CASHTAG.sub(sub, text)


def check_template(template: str, facts: dict[str, dict]) -> list[str]:
    """AIが数字を捏造していないか、存在しないデータを参照していないかを確認する。"""
    errors = []
    for fid, field in PLACEHOLDER.findall(template):
        if fid not in facts:
            errors.append(f"存在しないファクト {fid}")
        elif field not in facts[fid]["fields"]:
            errors.append(f"存在しないフィールド {fid}.{field}")
    if "{{" in PLACEHOLDER.sub("", template) or "}}" in PLACEHOLDER.sub("", template):
        errors.append("差し込み記号の書式が不正")
    stripped = PLACEHOLDER.sub("", template)
    for term in sorted(read_list("allowed_numeric_terms.txt"), key=len, reverse=True):
        stripped = stripped.replace(term, "")
    if DIGIT.search(stripped):
        errors.append(f"データ以外の数字が含まれている: {DIGIT.findall(stripped)}")
    if KANJI_QUANTITY.search(stripped):
        errors.append(f"漢数字による数量表現: {KANJI_QUANTITY.findall(stripped)}")
    return errors


def check_text(text: str, topic_key: str, fmt: str = "short") -> list[str]:
    errors = []
    cfg = settings()["limits"]
    min_chars, _, max_weighted = length_limits(fmt)

    if len(text) < min_chars:
        errors.append(f"短すぎる（{len(text)}文字）")
    if weighted_length(text) > max_weighted:
        errors.append(f"文字数上限超過（重み付き{weighted_length(text)}/{max_weighted}）")
    for phrase in read_list("banned_phrases.txt"):
        if phrase in text:
            errors.append(f"禁止表現「{phrase}」")
    if RESTATE.search(text):
        errors.append("数字の言い直し（「〜ではなく〜」）がある")
    if URL.search(text):
        errors.append("URLが含まれている（リンクは本文に入れない）")
    if MENTION.search(text):
        errors.append("@メンションが含まれている")
    if HASHTAG.search(text):
        errors.append("ハッシュタグは使わない")
    if ORPHAN_UNIT.search(text):
        errors.append("数字が抜けて単位だけ残っている（例:「日以上」）。条件の名前は材料の rule・label の値を差し込む")
    if len(CASHTAG.findall(text)) > 1:
        errors.append("キャッシュタグが多すぎる")

    sim, similar_to = max_similarity(text)
    if sim >= cfg["similarity_threshold"]:
        errors.append(f"過去の投稿と類似（類似度{sim:.2f}）: {similar_to[:40]}")
    for p in store.recent_posts(cfg["same_topic_cooldown_hours"]):
        if p.get("topic_key") == topic_key:
            errors.append(f"同じ話題を{cfg['same_topic_cooldown_hours']}時間以内に投稿済み")
            break
    return errors


def _shingles(text: str) -> set[str]:
    # 数字を揃えて比較し、「数字だけ違う同じ構文」も類似とみなす
    t = re.sub(r"[0-9０-９.,]+", "0", re.sub(r"\s+", "", text))
    return {t[i:i + 3] for i in range(max(len(t) - 2, 0))}


def similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def max_similarity(text: str) -> tuple[float, str]:
    best, best_text = 0.0, ""
    for prev in store.recent_texts():
        s = similarity(text, prev)
        if s > best:
            best, best_text = s, prev
    return best, best_text
