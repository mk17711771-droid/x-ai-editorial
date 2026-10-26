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
CASHTAG = re.compile(r"\$[A-Za-z][A-Za-z0-9.]*")


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


def length_limits() -> tuple[int, int, int]:
    """(最小文字数, 目安の最大文字数, 重み付き上限)"""
    if settings()["account"]["premium"]:
        return 60, 220, 25000
    # 無料アカウントは重み付き280まで = 日本語でおよそ140文字
    return 60, 135, 280


def render(template: str, facts: dict[str, dict]) -> str:
    def sub(m: re.Match) -> str:
        return facts[m.group(1)]["fields"][m.group(2)]
    text = PLACEHOLDER.sub(sub, template).strip()
    # 差し込みデータとAIの文章で語が重なったもの（例:「時点時点」）を整える
    for dup, fixed in (("時点時点", "時点"), ("終値終値", "終値"), ("約約", "約")):
        text = text.replace(dup, fixed)
    return text


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


def check_text(text: str, topic_key: str) -> list[str]:
    errors = []
    cfg = settings()["limits"]
    min_chars, _, max_weighted = length_limits()

    if len(text) < min_chars:
        errors.append(f"短すぎる（{len(text)}文字）")
    if weighted_length(text) > max_weighted:
        errors.append(f"文字数上限超過（重み付き{weighted_length(text)}/{max_weighted}）")
    for phrase in read_list("banned_phrases.txt"):
        if phrase in text:
            errors.append(f"禁止表現「{phrase}」")
    if URL.search(text):
        errors.append("URLが含まれている（リンクは本文に入れない）")
    if MENTION.search(text):
        errors.append("@メンションが含まれている")
    if HASHTAG.search(text):
        errors.append("ハッシュタグは使わない")
    if len(CASHTAG.findall(text)) > 2:
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
