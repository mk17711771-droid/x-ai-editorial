"""投稿前の安全チェック。1つでも引っかかったら投稿しない。"""
from __future__ import annotations

import re
import unicodedata

from . import store
from .common import read_list, settings

PLACEHOLDER = re.compile(r"\{\{\s*(f\d+)\.(\w+)\s*\}\}")
DIGIT = re.compile(r"[0-9０-９]")
PLACEHOLDER_ANY = re.compile(r"(\{\{[^}]*\}\})")
KANJI_QUANTITY = re.compile(r"[一二三四五六七八九十百千万億兆]+(?:割|%|％|倍|億|万|兆|ドル|円|ポイント|銘柄|ヶ月|か月|年)"
                            r"|[二三四五六七八九十百千][一二三四五六七八九十百千]*人")  # 人数（「一人」は数に数えない）
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
    # 最後の段落の最後の文から1文ずつ落とす（段落ごと落とすと短すぎて諦めていた。11/7 281/280などで3案を破棄）
    paras = [p for p in text.split("\n\n") if p.strip()]
    while len(paras) > 1 and weighted_length("\n\n".join(paras)) > max_weighted:
        last = paras[-1].rstrip()
        body = last.rstrip("。")
        if "。" in body:
            paras[-1] = body.rsplit("。", 1)[0] + "。"
        else:
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
        if m.group(2) == "funding_annual" and "年率" not in before[-8:] and not value.startswith("年率"):
            # 年率の資金調達率に「年率」が付いていないと、1回分の率に見える（11/4「資金調達率が-246.5%」）
            before += "年率"
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
    # 市場の内部の記号「xyz:TTWO」は本文では銘柄名だけにする（11/10 00:43 速報「新しい市場「xyz:TTWO」が取引開始」）
    text = re.sub(r"「?(?<![$A-Za-z])xyz:([A-Z][A-Z0-9]*)」?", r"\1", text)
    # 「〜になる見ている」「〜だ考えている」のように引用の「と」が抜けた崩れを直す（11/10 09:11「予兆になる見ている」）
    text = re.sub(r"(?<=[るいただ])(見ている|考えている|思っている|みている)", r"と\1", text)
    text = text.replace("ととみている", "とみている").replace("とと見ている", "と見ている")
    # 表記の統一（過去の表記ミスを config/name_fixes.txt に貯めて、投稿の直前に直す）
    for ln in read_list("name_fixes.txt"):
        wrong, _, right = ln.partition("\t")
        if wrong and right:
            text = text.replace(wrong, right)
    # 時点の重複（「米国11/20終値が-14.01%（米国11/20終値）」）を1つにする（11/21 note記事）
    text = re.sub(r"(米国\d{1,2}/\d{1,2}(?:\([月火水木金土日]\))?終値)([^。\n]{0,30}?)（\1）", r"\1\2", text)
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
    used = {fid for fid, _ in PLACEHOLDER.findall(template)}
    # 「40人」「8人」のような短い人数も、原稿がすでに使っている材料の値なら差し込みに戻す
    # （11/8 上位トレーダーの短文が「40人のうち8人…買い2人・売り6人」と正しい人数を書いて破棄された）
    pairs = sorted(((str(v), f"{{{{{fid}.{k}}}}}") for fid, f in facts.items() for k, v in f["fields"].items()
                    if DIGIT.search(str(v)) and (len(str(v)) >= 4 or (fid in used and re.fullmatch(r"\d{1,3}(?:人|%)", str(v))))),
                   key=lambda p: -len(p[0]))
    parts = PLACEHOLDER_ANY.split(template)
    for i in range(0, len(parts), 2):  # 既存の差し込み記号の中は触らない
        for value, ph in pairs:
            parts[i] = parts[i].replace(value, ph)
    return add_close_asof(align_line_labels("".join(parts), facts), facts)


def add_close_asof(template: str, facts: dict[str, dict]) -> str:
    """Hyperliquidの今の数字と並べる米国株の終値に、時点（米国◯/◯終値）がなければ自動で付ける。
    11/19 20時台: 時点の書き忘れで $QQQ（ナスダック100）の案が2つとも破棄され、なじみの薄い $SKHX が主役になった。"""
    ids = [fid for fid, _ in PLACEHOLDER.findall(template)]
    if not any((facts.get(f) or {}).get("kind") == "hl" for f in ids):
        return template
    for fid in dict.fromkeys(ids):
        f = facts.get(fid) or {}
        asof = str((f.get("fields") or {}).get("asof", ""))
        if f.get("kind") != "stock" or "終値" not in asof or f.get("category") == "week_change":
            continue
        if f"{{{{{fid}.asof}}}}" in template or asof in template:
            continue
        last = [m for m in PLACEHOLDER_ANY.finditer(template) if m.group(0).startswith(f"{{{{{fid}.")][-1:]
        if last:
            m = last[0]
            # 同じ行にすでに「終値」と書いてあれば足さない（「前日終値比」は時点ではないので足す）
            line = template[template.rfind("\n", 0, m.start()) + 1: template.find("\n", m.end()) if "\n" in template[m.end():] else len(template)]
            if re.search(r"(?<!前日)終値", line):
                continue
            template = template[:m.end()] + f"（{{{{{fid}.asof}}}}）" + template[m.end():]
    return template


LABEL_FIELDS = {"market", "asof", "basis"}


def align_line_labels(template: str, facts: dict[str, dict]) -> str:
    """箇条書きの1行で、ラベル（市場の種類・時点・基準）だけ別の材料を指していたら、その行の主役の材料に揃える。
    11/7「・{{f7.coin}}（{{f6.market}}）…」で行の混在チェックに当たり、週末長文の枠を落とした。数字の項目は直さない。"""
    out = []
    for ln in template.split("\n"):
        if ln.strip().startswith(("・", "-")):
            ids = [fid for fid, _ in PLACEHOLDER.findall(ln)]
            if ids:
                main = max(set(ids), key=ids.count)
                if ids.count(main) >= 2 and main in facts:
                    def fix(m: re.Match) -> str:
                        fid, field = m.group(1), m.group(2)
                        if fid != main and field in LABEL_FIELDS and field in facts[main]["fields"]:
                            return f"{{{{{main}.{field}}}}}"
                        return m.group(0)
                    ln = PLACEHOLDER.sub(fix, ln)
        out.append(ln)
    return "\n".join(out)


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
        if str(topic_key).startswith(prefix) or (prefix.startswith(":") and str(topic_key).endswith(prefix)):
            return hours
    return cfg["same_topic_cooldown_hours"]


# ラベルと差し込む値の取り違え（11/3 「安値」の後に高値を差し込んだ）。ラベルの直後の差し込みは、このフィールドでなければならない
# 取り違えると意味が逆になるラベル（安値に高値を入れる、など）。ラベルの直後の差し込みが反対の値ならはじく
LABEL_WRONG = {"安値": "high", "高値": "low", "売り比率": "long_ratio", "買い比率": "short_ratio"}
# 11/21 08:12「上位トレーダーの売り比率が4%」は買い比率（long_ratio）だった（同じ投稿で40人中24人が売り）
LABELED = re.compile(r"(安値|高値|売り比率|買い比率)(?:は|が|も|の)?[\s:：]?\{\{(f\d+)\.(\w+)\}\}")


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
    # 「{{f8.x}}から{{f6.x}}へ」: 別々の材料（別の銘柄）の値を、1つの値の変化のように書かない
    # （11/8 10:08「資金調達率が年率-16.4%から年率-18.5%へと」はSNDKとBRENTOILの別々の値だった）
    for m in re.finditer(r"\{\{\s*(f\d+)\.(\w+)\s*\}\}\s*から[^。\n]{0,12}?\{\{\s*(f\d+)\.(\w+)\s*\}\}\s*(へ|に|まで)", template):
        if m.group(1) != m.group(3) and m.group(2) == m.group(4):
            errors.append(f"別々の材料（{m.group(1)}・{m.group(3)}）の値を「から〜へ」で1つの変化のように書いている")
    # 「記録的」「歴史的」「過去最高」は、記録の材料（過去最高値・速報の記録）があるときだけ（11/8 SNDKの1週間-28.80%を「記録的な急落」と書いた）
    if re.search(r"記録的|歴史的|過去最高|史上最高|史上最大|過去最大", PLACEHOLDER.sub("", template)):
        used = [facts[f] for f, _ in PLACEHOLDER.findall(template) if f in facts]
        if not any(f.get("category") in ("record", "ath", "breaking_news") or "記録" in str(f.get("summary", ""))
                   or "最高" in str(f.get("summary", "")) for f in used):
            errors.append("「記録的・歴史的・過去最高」と書いているが、記録を示す材料がない")
    # 2日以上前の米国の終値（週明けの朝の金曜終値など）を、時点を書かずに「今日の動き」として使わない。
    # Hyperliquidの24時間の動きと対比するのも不可（11/9 08:10「現物が大きく買われる日に」がAMDの金曜+11.04%だった）
    stale = _stale_close_facts(template, facts)
    if stale:
        names = "・".join(sorted(stale))
        used_ids = {fid for fid, _ in PLACEHOLDER.findall(template)}
        if not all(f"{{{{{fid}.asof}}}}" in template for fid in stale.values()):
            errors.append(f"{names}は2日以上前の終値なのに、時点（asof）を書かずに使っている")
        # 時点を書かずにHyperliquidの今の動きと対比するのは不可。時点を書けば対比は可（24時間騰落率との対比は _stale_close_contrast が見る）
        hl_now = any(facts[f].get("kind") == "hl" for f in used_ids if f in facts)
        missing = [n for n, fid in stale.items() if f"{{{{{fid}.asof}}}}" not in template]
        if hl_now and missing and any(w in PLACEHOLDER.sub("", template) for w in CONTRAST):
            errors.append(f"{'・'.join(missing)}の2日以上前の終値の動きを、時点なしでHyperliquidの今の動きと対比している（期間が違う）")
    # 前日の終値でも、Hyperliquidの「日本時間◯時点」の数字と並べるなら終値の時点を書く
    # （11/18 21:10 「$QQQ 前日終値比-0.73%」が11/17の終値なのに「日本時間11/18 21:07時点」の見出しの下にあった）
    used_ids = {fid for fid, _ in PLACEHOLDER.findall(template)}
    if any((facts.get(f) or {}).get("kind") == "hl" for f in used_ids):
        prev = {n: fid for n, fid in _stale_close_facts(template, facts, min_days=1).items() if n not in (stale or {})}
        miss = [n for n, fid in prev.items() if f"{{{{{fid}.asof}}}}" not in template]
        if miss:
            errors.append(f"{'・'.join(miss)}は米国の終値。Hyperliquidの今の数字と並べるので、終値の時点（{{{{fN.asof}}}}）を書く")
    # 速報の内部の項目（status: 確定/確認中）を本文に出さない。説明文（what）を名詞のように「〜の{{what}}が」で埋め込まない
    # （11/9 14:51「Wintermuteの空売りポジションの利益が…1億1,500万ドル超にが確認されていて、ステータスは確定」）
    for fid, field in PLACEHOLDER.findall(template):
        if field == "status" and (facts.get(fid) or {}).get("category") == "breaking_news":
            errors.append("速報の内部の項目（status）を本文に書いている")
    # 速報の見出し（headline）・説明（what）は、文として置くか「」で囲む。助詞でつないで文の途中に埋め込まない
    # （11/14 20:51「ビットコインが{{who}}の急落で{{headline}}となる中」で日本語が崩れた）
    if re.search(r"(?<!「)\{\{\s*f\d+\.(headline|what)\s*\}\}(?!」)\s*(となる|となり|が|を|は|で|の|に|と)", template):
        errors.append("速報の見出し・説明を文の途中に助詞でつないで埋め込んでいる（文として置くか「」で囲む）")
    if re.search(r"\{\{\s*f\d+\.what\s*\}\}\s*(が|を|は)", template):
        errors.append("速報の説明文（what）を名詞のように「〜が・〜を」で埋め込んでいる（文として使う）")
    # 「建玉が拡大・増加・膨らんでいる」は、変化率の材料（*_change_pct）を使うときだけ（11/14 07:52 建玉合計123.8億ドルを
    # 「規模が一段と拡大」と書いたが、11/8の約146億ドルから減っていた）
    plain = PLACEHOLDER.sub("", template)
    if re.search(r"(建玉|オープンインタレスト|規模|出来高)[^。\n]{0,25}(拡大|増加|膨らん|急増|伸び|積み上が|膨張)", plain) or \
            re.search(r"(拡大|増加|膨らん|急増|積み上が)[^。\n]{0,10}(建玉|オープンインタレスト)", plain):
        fields_used = {f for _, f in PLACEHOLDER.findall(template)}
        if not any(f.endswith("change_pct") or f in ("oi_change", "volume_change") for f in fields_used):
            errors.append("建玉・出来高が「拡大・増加・膨らんだ」と書いているが、変化を示す材料（〜の24時間変化など）を使っていない")
    # 箇条書きで同じ数字を繰り返さない（11/14 12:14 SOXLの行が2本、同じ値を並べた）
    bullet_sets = [frozenset(f"{a}.{b}" for a, b in PLACEHOLDER.findall(ln)) for ln in template.split("\n")
                   if ln.strip().startswith(("・", "-")) and PLACEHOLDER.search(ln)]
    for i, a in enumerate(bullet_sets):
        if any(len(a & b) >= 3 for b in bullet_sets[i + 1:]):
            errors.append("箇条書きの2つの行で同じ数字を繰り返している（1つの行にまとめる）")
            break
    # HIP-3・Hyperliquidの先物の価格を「現物価格」と書かない（11/14 21:18「現物価格$56.00」はSOXLの先物の価格）
    for m in re.finditer(r"現物(?:の)?(?:価格|値)\s*\{\{\s*(f\d+)\.(price|change_pct)\s*\}\}", template):
        f = facts.get(m.group(1)) or {}
        if f.get("kind") == "hl" or "先物" in str((f.get("fields") or {}).get("market", "")):
            errors.append("Hyperliquidの先物の数字を「現物」と書いている")
            break
    # Hyperliquidの新しい市場（無期限先物）の追加を「現物の取り扱い」と書かない（11/18 0:43 io:INTCの上場を「現物の取り扱いが拡充」）
    used_hl = any((facts.get(fid) or {}).get("kind") == "hl" for fid, _ in PLACEHOLDER.findall(template))
    if used_hl and re.search(r"現物(?:の|が)?(?:取り扱い|取扱|上場|追加|銘柄|ラインナップ)", template):
        errors.append("Hyperliquidの市場は無期限先物。上場・追加を「現物」と書かない")
    # 1つの箇条書きの行に、別々の銘柄の材料を混ぜない（11/6 21:29「・$SOXX 価格{{f5}}…高値{{f14}}」で高値・安値が別銘柄）
    for ln in template.split("\n"):
        if not ln.strip().startswith(("・", "-")):
            continue
        syms = {str((facts.get(fid, {}).get("fields") or {}).get("symbol") or (facts.get(fid, {}).get("fields") or {}).get("coin") or "")
                .split(":")[-1].upper() for fid, _ in PLACEHOLDER.findall(ln)} - {""}
        if len(syms) > 1:
            errors.append(f"1つの行に別々の銘柄の値を混ぜている: {'・'.join(sorted(syms))}")
    # 「・QQQ … {{fN.change_pct}}」のfNが別の銘柄（AMD）だった（11/6 朝の長文でQQQにAMDの騰落率が入った）
    for ln in template.split("\n"):
        head = re.match(r"^[・\-]\s*\$?([A-Z][A-Z0-9.]{1,9})(?![A-Za-z])", ln.strip())
        if not head:
            continue
        for fid, _ in PLACEHOLDER.findall(ln):
            f = facts.get(fid, {}).get("fields", {})
            sym = str(f.get("symbol") or f.get("coin") or "").split(":")[-1].upper()
            if sym and sym != head.group(1) and sym not in head.group(1) and head.group(1) not in sym:
                errors.append(f"「{head.group(1)}」の行に別の銘柄（{sym}・{fid}）の値を差し込んでいる")
                break
    # 1行目で「$AMDの先物」と言うなら、その銘柄の先物（HIP-3）の材料を使っていること（11/6 夜の長文で、AMDの先物と言いながら数字はSKHXだった）
    first = template.strip().split("\n")[0]
    m = re.search(r"\$([A-Z][A-Z0-9.]{1,9})[^。\n]{0,12}(先物|資金調達率|建玉|金利)", first)
    if m:
        sym = m.group(1)
        used = {fid for fid, _ in PLACEHOLDER.findall(template)}
        hl_syms = {str(facts[f]["fields"].get("coin") or facts[f]["fields"].get("symbol") or "").split(":")[-1].upper()
                   for f in used if f in facts and "先物" in str(facts[f]["fields"].get("market", ""))}
        if hl_syms and sym not in hl_syms:
            errors.append(f"1行目で{sym}の先物を語っているが、先物の数字は別の銘柄（{'・'.join(sorted(hl_syms))}）")
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


# 指数の先物と、比べてよい現物（同じ指数に連動するもの）。セクターETF（SOXXなど）と比べて「先物と現物のずれ」とは言わない
INDEX_PAIRS = {"ナスダック100": ("QQQ", "ナスダック"), "XYZ100": ("QQQ", "ナスダック"), "SP500": ("SPY", "S&P"), "S&P500": ("SPY", "S&P")}
SECTOR_ETFS = ("SOXX", "SMH", "SOXL", "XLK", "XLF", "ARKK")


PRICE_LABEL = re.compile(r"先物|HIP-3|終値|現物|引け|前日比")


def unlabeled_dual_prices(title: str, body: str) -> list[str]:
    """同じ銘柄に違う価格が2つ出るのに、どちらが先物・終値かを書いていない文（11/18 noteの記事: NVDAの$161.75（HIP-3先物）と
    $155.46（現物の終値）を「NVDAは」とだけ書き、公開後の確認で食い違いと指摘された）。"""
    sentences = [x for x in re.split(r"(?<=[。\n])", title + "\n" + body) if x.strip()]
    by_sym: dict[str, dict[str, list[str]]] = {}
    tok = re.compile(r"(?<![A-Za-z$])\$?([A-Z]{2,6})(?![A-Za-z])|(\$[0-9][0-9,]*(?:\.[0-9]+)?)")
    for sen in sentences:
        sym = ""
        for m in tok.finditer(sen):  # 価格は、その直前に出た銘柄のものとして数える
            if m.group(1):
                if m.group(1) not in ("EPS", "ETF", "USD", "USDC", "HIP", "CEO", "AI", "GDP", "CPI", "FOMC", "OI"):
                    sym = m.group(1)
            elif sym:
                by_sym.setdefault(sym, {}).setdefault(m.group(2), []).append(sen)
    bad = []
    for sym, prices in by_sym.items():
        if len(prices) >= 2:
            bad += [f"{sym}: {sen.strip()[:60]}" for sens in prices.values() for sen in sens if not PRICE_LABEL.search(sen)]
    return bad


MISSING_NUMBER = re.compile(r"(?<![0-9０-９.一二三四五六七八九十百千])(倍|%|％)(から|に|へ|まで|を|と)|(から|の)(倍|%|％)(に|へ|まで|から)")


def missing_number(text: str) -> str:
    """数字の抜け（「従来の倍から倍に」）。差し込みや文の修正で数字だけ消えたときに出る（11/18 noteの記事）。"""
    m = MISSING_NUMBER.search(text)
    return m.group(0) if m else ""


def _sector_vs_index(text: str) -> str:
    if not any(w in text for w in CONTRAST + ("違う", "穏やか", "とどま")):
        return ""
    for idx in INDEX_PAIRS:
        if idx in text:
            for etf in SECTOR_ETFS:
                if re.search(rf"(?<![A-Z]){etf}(?![A-Z])", text):
                    return f"セクターETFの{etf}と指数の先物（{idx}）の値動きの差を比べている（別の指数どうし。比べるなら{INDEX_PAIRS[idx][0]}と）"
    return ""


CLOSE_DATE = re.compile(r"(\d{1,2})/(\d{1,2})(?:\([月火水木金土日]\))?終値")
CONTRAST = ("のに", "一方", "対し", "コントラスト", "温度差", "乖離", "崩れてない", "崩れていない", "平気", "半面", "反面", "裏で", "裏腹")


def _stale_close_facts(template: str, facts: dict[str, dict], min_days: int = 2) -> dict[str, str]:
    """原稿が使っている米国株の材料のうち、終値が2日以上前のもの（銘柄→fid）。"""
    from .common import now_jst
    today = now_jst().date()
    out = {}
    for fid, field in PLACEHOLDER.findall(template):
        f = facts.get(fid) or {}
        if f.get("kind") != "stock" or field == "asof":
            continue
        m = re.search(r"米国(\d{1,2})/(\d{1,2})", str(f.get("fields", {}).get("asof", "")))
        if not m or "終値" not in str(f["fields"].get("asof", "")):
            continue
        try:
            close = today.replace(month=int(m.group(1)), day=int(m.group(2)))
        except ValueError:
            continue
        if close > today:
            close = close.replace(year=close.year - 1)
        if (today - close).days >= min_days and f.get("category") != "week_change":  # 週の振り返りは期間を書く前提
            out[str(f["fields"].get("symbol", fid))] = fid
    return out


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
# 「全体相場全体」のように、漢字2字以上の語が3字以内ですぐ繰り返される崩れ（11/19 07:45 朝の長文）
KANJI_DUP = re.compile(r"([一-龥]{2,})[一-龥]{0,3}\1")


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
    first = text.strip().split("\n")[0]
    for term in ("HIP-3", "HIP-4", "S&P500", "SP500", "ナスダック100", "XYZ100", "trade[XYZ]", "24時間", "24/7"):
        first = first.replace(term, "")  # 名前に含まれる数字は数えない（11/6「$MUのHIP-3先物で何が起きてるのか」がすり抜けた）
    if fmt == "short" and not str(topic_key).startswith("news:") and not re.search(r"[0-9０-９]", first):
        # 伸びた短文は1行目に数字が1つある（11/4 $SOXL 年率-304.1% → 380表示）。数字のない1行目（「〜について解説する」）は弱い
        errors.append("1行目に数字がない（大きな名前＋一番強い数字1つで言い切る）")
    if re.search(r"\b(asof|change_pct|funding_annual|basis|oi_change_pct|us_change_pct|venue|market|symbol|coin)\b|venue:|市場:", text):
        errors.append("データの項目名（asof など）が本文にそのまま出ている")
    cross = _sector_vs_index(text)
    if cross:  # 11/10 09:16 半導体ETF（SOXX +7.49%）とナスダック100先物（+3.92%）の差を「現物との違い」とした
        errors.append(cross)
    if missing_number(text):
        errors.append(f"数字が抜けている（「{missing_number(text)}」）")
    # 指数（QQQ・SPY・ナスダック・S&P）を「半導体指数」と呼ばない（11/18 08:09「主要な半導体指数であるQQQおよびSPY」）
    if re.search(r"半導体[^。\n]{0,4}指数(?:である|の|、|：|:)?\s*[「（(]?(?:QQQ|SPY|ナスダック|S&P)|(?:QQQ|SPY|ナスダック100|S&P500)(?:など|という|の)?(?:主要な)?半導体[^。\n]{0,4}指数", text):
        errors.append("QQQ・SPY（ナスダック100・S&P500）は半導体の指数ではない（半導体ならSOXX・SMH）")
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
    # 参考アカウントの文章を真似しない（他者の文章の転載・模倣は禁止。型だけを参考にする）
    from . import sources as _sources
    for r in _sources.reference_posts(days=30, n=50):
        if similarity(text, r.get("text", "")) >= 0.3:
            errors.append(f"参考アカウント（@{r['author']}）の投稿と文章が似すぎている。型だけ参考にして、自分の言葉で書き直す")
            break
    # 米国市場の取引時間中に「オープン前・寄り付き前」と書かない（11/19 23:33「米国市場オープン前」は開場の3分後）
    if re.search(r"(オープン|寄り付き|寄付き|開場|取引開始)(を控えた|前)", text):
        from zoneinfo import ZoneInfo
        from .common import now_utc as _now
        ny = _now().astimezone(ZoneInfo("America/New_York"))
        if ny.weekday() < 5 and (ny.hour, ny.minute) >= (9, 30) and ny.hour < 16:
            errors.append("米国市場はすでに開いている（取引時間中）のに「オープン前」と書いている")
    kd = KANJI_DUP.search(text)
    if kd:
        errors.append(f"同じ語「{kd.group(1)}」がすぐ繰り返されている（「{kd.group(0)}」）")
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
        # 1週間の振り返り（week_change）は、銘柄が違っても週1回（11/22 10:24 前日の振り返りとほぼ同じ長文が出た）
        if str(topic_key).endswith(":week_change") and str(p.get("topic_key", "")).endswith(":week_change"):
            errors.append(f"1週間の振り返りは{hours}時間以内に投稿済み（週1回）")
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
    # いちばん通じる主役（top_names: QQQ・SOXLなど。中央値300）は、前日の同じ枠の後（20時間）なら続けて使ってよい
    # （11/19 20時台: 前夜21:10の$QQQから23.5時間で弾かれ、$SKHXが主役になった）
    top = {n.upper() for n in read_list("top_names.txt")}
    recent_top = {t for p in store.recent_posts(cfg.get("top_symbol_cooldown_hours", 20)) for t in CASHTAG.findall(p["text"])}
    recent_tags = {t for t in recent_tags if t.lstrip("$").upper() not in top or t in recent_top}
    if tags & recent_tags:
        errors.append(f"主役の銘柄{'・'.join(sorted(tags & recent_tags))}を直近に投稿済み（別の銘柄を主役にする）")
    return errors


# 事実を伝える定型の語（データの行）は、どの投稿にも出るので類似度の計算から外す
# （11/15 23時 銘柄も数字も違う短文が、定型の行が同じため類似度0.47〜0.50で全案破棄され枠を落とした）
SIM_BOILERPLATE = ("Hyperliquidの株式先物（HIP-3）で", "Hyperliquidの商品先物（HIP-3）で", "Hyperliquidの無期限先物", "日本時間",
                   "時点", "24時間騰落率", "24時間出来高", "資金調達率", "（年率）", "年率", "建玉は約", "建玉", "価格は", "価格",
                   "となっている", "を記録している", "に達している", "まで")


def _shingles(text: str) -> set[str]:
    # 数字を揃えて比較し、「数字だけ違う同じ構文」も類似とみなす。定型の語は除く
    t = re.sub(r"\s+", "", text)
    for w in SIM_BOILERPLATE:
        t = t.replace(w, "")
    t = re.sub(r"[0-9０-９.,]+", "0", t)
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
