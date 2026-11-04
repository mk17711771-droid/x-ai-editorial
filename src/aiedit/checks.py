"""投稿前の安全チェック。1つでも引っかかったら投稿しない。"""
from __future__ import annotations

import re
import unicodedata

from . import store
from .common import read_list, settings

PLACEHOLDER = re.compile(r"\{\{\s*(f\d+)\.(\w+)\s*\}\}")
DIGIT = re.compile(r"[0-9０-９]")
PLACEHOLDER_ANY = re.compile(r"(\{\{[^}]*\}\})")
KANJI_QUANTITY = re.compile(r"[一二三四五六七八九十百千万億兆]+(?:割|%|％|倍|億|万|兆|ドル|円|ポイント|銘柄|ヶ月|か月|年)")
URL = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|io|xyz|jp|net|org)\b", re.I)
MENTION = re.compile(r"(?<![\w])@\w+")
HASHTAG = re.compile(r"[#＃]\w+")
# 「20人ではなく23人」「+6.19%ではなく+4.04%」のような、数字を言い直す不自然な文
RESTATE = re.compile(r"[0-9][0-9.,]*\s*(?:人|倍|%|％|ドル|円)?\s*ではなく\s*[+\-$約]?[0-9]")
CASHTAG = re.compile(r"\$[A-Za-z][A-Za-z0-9.]*")
BOILERPLATE = re.compile(r"^[ \t]*[※＊*][^\n]*(推奨|自己責任|助言|勧誘|保証)[^\n]*$", re.M)
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
    if fmt == "summary" and settings()["account"]["premium"]:
        # 朝のまとめ（箇条書きの一覧）。タイムラインで「さらに表示」1回で読み切れる長さ
        return 100, 320, 25000
    # 短文はタイムラインで全文が見える長さ（重み付き280 = 日本語でおよそ140文字）
    return 60, 135, 280


def fit_length(text: str, fmt: str = "short") -> str:
    """重み付きの上限を少し超えた短文を、末尾の段落・文から落として収める（数字を含む最初の段落は残す）。"""
    min_chars, _, max_weighted = length_limits(fmt)
    if weighted_length(text) <= max_weighted:
        return text
    paras = [p for p in text.split("\n\n") if p.strip()]
    while len(paras) > 1 and weighted_length("\n\n".join(paras)) > max_weighted:
        paras.pop()
    out = "\n\n".join(paras)
    while weighted_length(out) > max_weighted and "。" in out.rstrip("。"):
        out = out.rstrip("。").rsplit("。", 1)[0] + "。"
    return out if len(out) >= min_chars and weighted_length(out) <= max_weighted else text


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
        if before.endswith("$") and not TICKER.match(value) and not re.match(r"^\$?\d", value):
            # 「$」は銘柄記号と価格の前だけ。騰落率や「米国11/3終値」の前の「$」は消す（11/4「前日比$+5.53%」）
            before = before[:-1]
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
    # 同じ「（10:27時点）」が行ごとに並ぶと読みにくい（10/31 週の振り返り）。2つ目以降は消す
    for stamp in set(ASOF_PAREN.findall(text)):
        first = text.find(stamp) + len(stamp)
        text = text[:first] + text[first:].replace(stamp, "")
    # 「※情報共有であって売買の推奨ではありません」のような定型の注意書きは、自動投稿っぽく見えるので外す
    # （売買を勧める表現そのものは禁止表現のチェックで防ぐ。noteの注意書きは記事の末尾にシステムが付ける）
    text = BOILERPLATE.sub("", text)
    # trade[XYZ]の銘柄名は読者に通じないので一般の呼び名にする（$XYZ100・xyz:XYZ100 は銘柄記号なのでそのまま）
    text = re.sub(r"(?<![$:A-Za-z])XYZ100(?![A-Za-z0-9])", "ナスダック100", text)
    # 表記の統一（過去の表記ミスを config/name_fixes.txt に貯めて、投稿の直前に直す）
    for ln in read_list("name_fixes.txt"):
        wrong, _, right = ln.partition("\t")
        if wrong and right:
            text = text.replace(wrong, right)
    # Markdownの記号（`コード`・**太字**）はXではそのまま表示されてしまうので外す
    text = text.replace("`", "").replace("**", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


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


def relink(template: str, facts: dict[str, dict]) -> str:
    """AIが材料の値をそのまま書き写した箇所（例:「日本時間10/27(火) 21:45時点」）を差し込み記号に戻す。

    値が材料と完全に一致するときだけ置き換えるので、捏造の数字はそのまま残り、チェックで弾かれる。
    """
    pairs = sorted(((str(v), f"{{{{{fid}.{k}}}}}") for fid, f in facts.items() for k, v in f["fields"].items()
                    if DIGIT.search(str(v)) and len(str(v)) >= 4), key=lambda p: -len(p[0]))
    parts = PLACEHOLDER_ANY.split(template)
    for i in range(0, len(parts), 2):  # 既存の差し込み記号の中は触らない
        for value, ph in pairs:
            parts[i] = parts[i].replace(value, ph)
    return "".join(parts)


def repair_fields(template: str, facts: dict[str, dict]) -> tuple[str, list[str]]:
    """存在しない項目名の差し込み記号を直す。似た名前があれば置き換え、なければその文ごと外す（note記事用）。"""
    import difflib
    dropped: list[str] = []

    def fix(m: re.Match) -> str:
        fid, field = m.group(1), m.group(2)
        if fid not in facts or field in facts[fid]["fields"]:
            return m.group(0)
        near = difflib.get_close_matches(field, list(facts[fid]["fields"]), n=1, cutoff=0.8)
        return f"{{{{{fid}.{near[0]}}}}}" if near else m.group(0)
    template = PLACEHOLDER.sub(fix, template)

    def bad(s: str) -> bool:
        return any(fid not in facts or field not in facts[fid]["fields"] for fid, field in PLACEHOLDER.findall(s))
    lines = []
    for ln in template.split("\n"):
        if bad(ln):
            parts = re.split(r"(?<=[。！？])", ln)
            kept = [p for p in parts if not bad(p)]
            dropped += [p for p in parts if bad(p)]
            ln = "".join(kept)
            if not ln.strip() or ln.strip() in ("-", "・"):
                continue
        lines.append(ln)
    return "\n".join(lines), dropped


EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]")


def _ending(text: str) -> dict:
    """最後の行の締め（末尾の絵文字の有無と、語尾の2文字）。"""
    last = [ln for ln in text.strip().splitlines() if ln.strip()][-1:] or [""]
    tail = last[0].rstrip()
    emoji = bool(EMOJI.search(tail[-3:]))
    core = EMOJI.sub("", tail).rstrip("。！？!? 　")
    # 比べるのは口語の語尾（〜だな／〜かも／〜っぽい など）だけ。普通の言い切りは続いても不自然ではない
    casual = next((t for t in ("だな", "かも", "っぽい", "ってこと", "じゃん", "よね", "な") if core.endswith(t)), "")
    return {"emoji": emoji, "tail": casual}


def topic_cooldown_hours(topic_key: str) -> int:
    """話題ごとの再投稿までの間隔。定番企画（絞り込みなど）は長めに空ける。"""
    cfg = settings()["limits"]
    for prefix, hours in cfg.get("topic_cooldown_overrides", {}).items():
        if str(topic_key).startswith(prefix):
            return hours
    return cfg["same_topic_cooldown_hours"]


# ラベルと差し込む値の取り違え（11/3 「安値」の後に高値を差し込んだ）。ラベルの直後の差し込みは、このフィールドでなければならない
# 取り違えると意味が逆になるラベル（安値に高値を入れる、など）。ラベルの直後の差し込みが反対の値ならはじく
LABEL_WRONG = {"安値": "high", "高値": "low"}
LABELED = re.compile(r"(安値|高値)(?:は|が|も|の)?[\s:：]?\{\{(f\d+)\.(\w+)\}\}")


def drop_unsafe_sentences(template: str, facts: dict[str, dict]) -> tuple[str, list[str]]:
    """データ以外の数字・不正な差し込みを含む文だけを外す（note記事用。記事は公開前に人が読むので、全体は捨てない）。
    11/4 今週の速報の説明文の数字（63億ドルなど）をAIが直接書き、3回とも記事ごと破棄されて水曜の記事が出なかった。"""
    dropped: list[str] = []
    lines = []
    for ln in template.split("\n"):
        if not ln.strip() or ln.lstrip().startswith("!["):
            lines.append(ln)
            continue
        parts = re.split(r"(?<=[。！？])", ln)
        kept = [s for s in parts if not s.strip() or not check_template(s, facts)]
        dropped += [s for s in parts if s.strip() and check_template(s, facts)]
        new = "".join(kept)
        if new.strip() and new.strip() not in ("-", "・", "#", "##", "###"):
            lines.append(new)
    return "\n".join(lines), dropped


def check_template(template: str, facts: dict[str, dict]) -> list[str]:
    """AIが数字を捏造していないか、存在しないデータを参照していないかを確認する。"""
    errors = []
    for label, fid, field in LABELED.findall(template):
        if LABEL_WRONG[label] in field:
            errors.append(f"「{label}」のあとに別の値（{fid}.{field}）を差し込んでいる")
    for fid, field in PLACEHOLDER.findall(template):
        if fid not in facts:
            errors.append(f"存在しないファクト {fid}")
        elif field not in facts[fid]["fields"]:
            errors.append(f"存在しないフィールド {fid}.{field}（{fid}で使えるのは: {', '.join(facts[fid]['fields'])}）")
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


# レバレッジ型ETFと元の指数（3倍・逆3倍などは値動きの差が当然。差を「歪み」と言わない）
LEVERAGED = {"SOXL": "SOXX", "SOXS": "SOXX", "TQQQ": "QQQ", "SQQQ": "QQQ", "UPRO": "SPY", "SPXL": "SPY", "SPXU": "SPY",
             "NVDL": "NVDA", "TSLL": "TSLA", "FNGU": "QQQ"}


def _leveraged_vs_underlying(text: str) -> str:
    if not any(w in text for w in CONTRAST + ("歪み", "さらに大きく", "上回る")):
        return ""
    for lev, base in LEVERAGED.items():
        if lev in text and re.search(rf"(?<![A-Z]){base}(?![A-Z])", text):
            return f"レバレッジ型の{lev}と元の{base}の値動きの差を比べている（倍率の差で当然。歪みではない）"
    return ""


CLOSE_DATE = re.compile(r"(\d{1,2})/(\d{1,2})(?:\([月火水木金土日]\))?終値")
CONTRAST = ("のに", "一方", "対し", "コントラスト", "温度差", "乖離", "崩れてない", "崩れていない", "平気")


def _stale_close_contrast(text: str) -> str:
    """2日以上前の米国の終値の動きと、今の24時間の動きを並べて対比していないか。"""
    from .common import now_jst
    if "24時間" not in text or not any(w in text for w in CONTRAST):
        return ""
    today = now_jst().date()
    for m, d in CLOSE_DATE.findall(text):
        try:
            close = today.replace(month=int(m), day=int(d))
        except ValueError:
            continue
        if close > today:
            close = close.replace(year=close.year - 1)
        if (today - close).days >= 2:
            return f"{m}/{d}の終値の動きと今の24時間の動きを対比している（期間が違う数字どうしの対比は誤解を招く）"
    return ""


# 「を決議したを決議した」のような、同じ語句がそのまま2回続く崩れ（11/2 15:51）
REPEAT_PHRASE = re.compile(r"([ぁ-んァ-ヶ一-龥ー]{4,12})\1")
JUNK_LATIN = re.compile(r"(?<=[ぁ-んァ-ヶ一-龥])([a-z]{5,})(?=[ぁ-んァ-ヶ一-龥])")
CASHTAG_ANY = re.compile(r"\$([A-Za-z][A-Za-z0-9:.]*)")
TICKER_OK = re.compile(r"^(xyz:)?[A-Z][A-Z0-9.]*$")
ASOF_PAREN = re.compile(r"[（(][^（）()\n]{0,20}時点[）)]")
NEAR_DUP = re.compile(r"([ァ-ヶー]{4,}|[A-Za-z]{4,})[^。\n]{0,10}?\1")


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
    if "速報" in text and not topic_key.startswith("news:"):
        errors.append("通常の投稿に「速報」と書いている（速報はネットでいち早くキャッチした話題の速報枠だけ）")
    lev = _leveraged_vs_underlying(text)
    if lev:  # 11/4 SOXL(3倍)とSOXX(1倍)の値動きの差を「歪み」と書いた
        errors.append(lev)
    stale = _stale_close_contrast(text)
    if stale:  # 何日も前の終値と今の24時間の動きを対比した（11/2 12:28と20:46に同じ誤り）
        errors.append(stale)
    rep = REPEAT_PHRASE.search(text)
    if rep:
        errors.append(f"同じ語句「{rep.group(1)}」がそのまま2回続いている（文が崩れている）")
    junk = JUNK_LATIN.search(text)
    if junk:  # AIの出力に紛れた意味不明な英字（10/31「リスクがあるacylglycerと推測」）
        errors.append(f"文中に意味不明な英字「{junk.group(1)}」が混ざっている")
    bad_tags = [c for c in CASHTAG_ANY.findall(text) if not TICKER_OK.match(c)]
    if bad_tags:  # 「$Intercontinental Exchange (ICE)」のように銘柄記号でない語に $ が付いた（10/31）
        errors.append(f"$ の後が銘柄記号ではない: {bad_tags[:3]}")
    m = NEAR_DUP.search(text)
    if m:  # 「バイナンスで…バイナンスが」のような同じ語のすぐの繰り返し（10/30 21:11の速報）
        errors.append(f"同じ語「{m.group(1)}」が近くで繰り返されている（文を組み直す）")
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
    hours = topic_cooldown_hours(topic_key)
    for p in store.recent_posts(hours):
        if p.get("topic_key") == topic_key or str(p.get("topic_key", "")).startswith(topic_key + ":"):
            errors.append(f"同じ話題を{hours}時間以内に投稿済み")
            break
    # 締めの「語尾＋絵文字」（〜な👀 など）が直近の投稿と続くと、自動投稿っぽく単調になる
    end = _ending(text)
    recent_ends = [_ending(p["text"]) for p in store.recent_posts(48)[-3:]]
    if end["emoji"] and any(e["emoji"] for e in recent_ends):
        errors.append("直近の投稿も絵文字で締めている。今回は絵文字なしで、別の締め方にする")
    elif end["tail"] and end["tail"] in [e["tail"] for e in recent_ends]:
        errors.append(f"締めの言い回し（〜{end['tail']}）が直近の投稿と同じ。別の締め方にする")
    # 決まり文句（「地味だけど〜」など）を直近の投稿で使っていたら、言い回しを変えさせる
    # 1週間に1回まで（以前は直近12投稿だけ見ていて、2日で同じ決まり文句が戻ってきた。11/3「えぐいことになってる」）
    recent_texts = [p["text"] for p in store.recent_posts(24 * 7)]
    for phrase in read_list("cliche_phrases.txt"):
        if phrase in text and any(phrase in t for t in recent_texts):
            errors.append(f"「{phrase}」は最近も使った言い回し。別の言葉で書く")
    # 主役（キャッシュタグの銘柄）が直近の投稿と同じなら、話題の名前が違っても続けて出さない
    tags = set(CASHTAG.findall(text)) if not str(topic_key).startswith("news:") else set()  # 速報は同じ銘柄でも出す
    recent_tags = {t for p in store.recent_posts(cfg.get("same_symbol_cooldown_hours", 18)) for t in CASHTAG.findall(p["text"])}
    if tags & recent_tags:
        errors.append(f"主役の銘柄{'・'.join(sorted(tags & recent_tags))}を直近に投稿済み（別の銘柄を主役にする）")
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
