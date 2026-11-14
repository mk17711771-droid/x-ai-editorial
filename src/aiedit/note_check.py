"""noteに公開された記事を確認し、間違いがあればLINEで知らせる（問題がなければ知らせない）。

半自動運用なので、管理者が貼り付けて公開した記事を、自動で作った原稿と突き合わせる。
  - 貼り付けの事故（画像の目印や記号が残っている、本文の欠け、画像の入れ忘れ）
  - 数字の食い違い（原稿にない数字、原稿の数字が消えている）
  - 免責・Xへの案内の有無、禁止表現・外部リンク
  - 作成から時間がたって数字が古くなっていないか
  - AIによる校正（誤字脱字・文のつながり・数字と説明の矛盾）
"""
from __future__ import annotations

import difflib
import html
import json
import re
from datetime import datetime, timedelta

import requests

from . import checks, line, llm, store
from .common import JST, log, now_utc, settings

API = "https://note.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; x-ai-editorial/1.0)"}
NUM = re.compile(r"[+\-−]?\$?\d[\d,]*(?:\.\d+)?")
LEFTOVER = [
    (re.compile(r"▼ここに画像"), "「▼ここに画像〜を挿入」の目印が残っています（画像に差し替えて目印を消してください）"),
    (re.compile(r"chart:"), "図の指定（chart:）が残っています"),
    (re.compile(r"\{\{|\}\}"), "差し込み記号（{{ }}）が残っています"),
    (re.compile(r"\*\*[^*\n]+\*\*"), "太字の記号（**）がそのまま表示されています（スマホで貼ると起きます。PCで貼り直すと直ります）"),
    (re.compile(r"(^|\n)\s*#{2,3}\s"), "見出しの記号（##）がそのまま表示されています"),
]


def _plain(body_html: str) -> str:
    t = re.sub(r"<br\s*/?>|</p>|</h\d>|</li>|</figcaption>|</figure>", "\n", body_html)  # 画像の説明文が次の段落とつながらないように
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{2,}", "\n", t).strip()


def _md_plain(md_text: str) -> str:
    t = re.sub(r"^#+\s*", "", md_text, flags=re.M)
    t = t.replace("**", "")
    return re.sub(r"\n{2,}", "\n", t).strip()


def _nums(text: str) -> set[str]:
    out = set()
    text = re.sub(r"https?://\S+|@\w+", "", text)  # URLやアカウント名の数字（satoshikun_666 など）は比べない
    for m in NUM.finditer(text):
        v = m.group(0).replace("−", "-").replace(",", "").lstrip("+$")
        if len(v.strip("-")) >= 2 or "." in v:  # 1桁の数字（①の代わりの1や「2つ」など）は比べない
            out.add(v)
    return out


def published_list(urlname: str) -> list[dict]:
    r = requests.get(f"{API}/v2/creators/{urlname}/contents", params={"kind": "note", "page": 1},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json().get("data", {}).get("contents", [])


def fetch_body(key: str) -> dict:
    r = requests.get(f"{API}/v3/notes/{key}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json().get("data", {})


def _generated(days: int = 21) -> list[dict]:
    out = []
    for p in sorted(store.DATA_DIR.glob("note_articles/*/meta.json")):
        meta = json.loads(p.read_text())
        if now_utc() - datetime.fromisoformat(meta["created_at"]) > timedelta(days=days):
            continue
        meta["md"] = (p.parent / "article.md").read_text() if (p.parent / "article.md").exists() else ""
        out.append(meta)
    return out


def match(title: str, text: str, generated: list[dict]) -> dict | None:
    best, score = None, 0.0
    for g in generated:
        s_title = difflib.SequenceMatcher(None, title, g["title"]).ratio()
        s_body = difflib.SequenceMatcher(None, text[:600], _md_plain(g["md"])[:600]).ratio()
        s = max(s_title, s_body)
        if s > score:
            best, score = g, s
    return best if score >= 0.5 else None


def review(title: str, text: str, n_images: int, published_at: datetime | None, gen: dict | None) -> list[str]:
    """機械的に確かめられる問題の一覧（空なら問題なし）。"""
    issues: list[str] = []
    for pat, msg in LEFTOVER:
        if pat.search(text):
            issues.append(msg)
    if "売買を勧めるものではありません" not in text and "売買の推奨ではありません" not in text:
        issues.append("末尾の免責（売買を勧めるものではありません）が見当たりません")
    if "上位トレーダー" in text and "過去の成績" not in text:
        issues.append("上位トレーダーを扱っていますが「過去の成績です。今後も同じとは限りません」の注意書きがありません")
    bad = [p for p in checks.read_list("banned_phrases.txt") if p in title + text]
    if bad:
        issues.append(f"禁止表現が含まれています: {'、'.join(bad)}")
    links = [u for u in re.findall(r"https?://[^\s<>\"）)]+", text) if not re.search(r"(note\.com|x\.com|twitter\.com)", u)]
    if links:
        issues.append(f"外部リンクがあります（紹介リンクはPR表記が必要です）: {links[:3]}")
    if not gen:
        return issues

    # タイトル（# の行）は本文と別の欄に貼るので、本文の比較から外す（10/31 タイトルの数字を「本文にない」と誤って知らせた）
    src = _md_plain(re.sub(r"\A#\s[^\n]*\n", "", gen["md"].split("\n---\n")[0]))
    if len(text) < len(src) * 0.8:
        issues.append(f"本文が原稿より短くなっています（原稿{len(src)}字→公開{len(text)}字）。貼り付けの途中で切れていないか確認してください")
    want = len(gen.get("images", []))
    if n_images < want:
        issues.append(f"画像が{want}枚の予定のところ{n_images}枚です（入れ忘れの可能性）")
    missing = sorted(_nums(src) - _nums(text))
    extra = sorted(_nums(text) - _nums(src) - _nums(title) - _nums(gen["title"]))
    if missing:
        issues.append(f"原稿にあった数字が見当たりません: {', '.join(missing[:8])}（書き換えた場合は根拠を確認）")
    if extra:
        issues.append(f"原稿にない数字があります: {', '.join(extra[:8])}（データの根拠があるか確認）")
    if published_at:
        lag = published_at - datetime.fromisoformat(gen["created_at"])
        if lag > timedelta(hours=24):
            issues.append(f"記事の作成から{lag.days}日{lag.seconds // 3600}時間たってから公開されています。数字は作成時点のものなので、本文の「〜時点」の表記があるか確認してください")
    return issues


def proofread(title: str, text: str) -> list[str]:
    """AIによる校正。明らかな誤りだけを返す（好みの問題は返さない）。"""
    try:
        out = llm.generate_json(llm.prompt("note_review"), f"# タイトル\n{title}\n\n# 本文\n{text[:12000]}")
    except Exception as e:  # noqa: BLE001 校正の失敗で確認全体を止めない
        log.warning("AI校正に失敗: %s", e)
        return []
    return [f"{i.get('where', '')}: {i.get('problem', '')}" + (f" → {i['fix']}" if i.get("fix") else "")
            for i in out.get("issues", [])[:6] if i.get("problem")]


def run() -> list[dict]:
    cfg = settings().get("note", {})
    urlname = cfg.get("urlname", "charisma_hl")
    since = datetime.fromisoformat(cfg.get("check_since", "2026-10-27T00:00:00+09:00"))
    checked = store.read_json("note/checked.json", {})
    results = []
    generated = _generated()
    for n in published_list(urlname):
        key = n.get("key")
        pub = n.get("publishAt")
        pub_at = datetime.fromisoformat(pub) if pub else None
        # 確認済みで問題なしの記事は飛ばす。指摘があった記事は、管理者が直したあと見直す（11/13 直しても再確認されず紹介されなかった）
        if not key or checked.get(key, {}).get("ok") or (pub_at and pub_at < since):
            continue
        prev = checked.get(key)
        data = fetch_body(key)
        title = data.get("name") or n.get("name") or ""
        body = data.get("body") or ""
        text = _plain(body)
        n_images = len(re.findall(r"<img\b", body))
        gen = match(title, text, generated)
        issues = review(title, text, n_images, pub_at, gen)
        typos = proofread(title, text)
        res = {"key": key, "title": title, "checked_at": now_utc().isoformat(), "published_at": pub,
               "matched": gen["id"] if gen else None, "issues": issues, "proofread": typos,
               "chars": len(text), "images": n_images, "likes": n.get("likeCount")}
        store.write_json(f"note/checks/{key}.json", res)
        # 「作成から時間がたって公開」は直せない注意なので、紹介を止める理由にしない
        blocking = [i for i in issues if not i.startswith("記事の作成から")]
        # 文章の手直しの提案（AIの校正）は紹介を止めない。止めるのは機械の確認の指摘（目印の残り・数字の食い違いなど）だけ
        # （11/13 画像と句点を直したあと、言い回しの提案だけで紹介されなかった）
        ok = not blocking
        sig = "|".join(sorted(issues + typos))
        checked[key] = {"title": title, "at": res["checked_at"], "ok": ok, "sig": sig}
        if gen:
            meta = store.read_json(f"note_articles/{gen['id']}/meta.json")
            meta.update(status="published", note_key=key, published_at=pub, check_ok=ok)
            store.write_json(f"note_articles/{gen['id']}/meta.json", meta)
        if (blocking and not (prev and prev.get("sig") == sig)) or (typos and not prev):
            _notify(res, urlname)  # 同じ指摘は2回知らせない（LINEの無料枠）
        results.append(res)
    store.write_json("note/checked.json", checked)
    # 公開から3日以内で、確認に問題がなく、まだXで紹介していない記事を紹介する（指摘があった記事は直ってから）
    # Xで紹介するのは見られる時間帯（日本時間19〜23時）だけ。日中の紹介は25〜27表示どまり、同じ日の夜の長文は300〜800表示
    # （11/10 noteのダッシュボード: 1記事のビュー2〜8。Xからの流入を増やす）
    from .common import now_jst
    evening = 19 <= now_jst().hour < 23
    for g in generated:
        meta = store.read_json(f"note_articles/{g['id']}/meta.json") or {}
        if (evening and meta.get("status") == "published" and meta.get("check_ok") and not meta.get("promoted") and meta.get("published_at")
                and now_utc() - datetime.fromisoformat(meta["published_at"]) < timedelta(days=3)):
            promote(meta, f"https://note.com/{urlname}/n/{meta['note_key']}")
    _record_stats(urlname)
    return results


def promote_text(meta: dict, url: str) -> str:
    """公開したnote記事をXで知らせる文（数字はタイトル・リード文＝実データから作った原稿のものだけ）。"""
    md_path = store.DATA_DIR / f"note_articles/{meta['id']}/article.md"
    body = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    lines = [re.sub(r"\*\*", "", ln.strip()) for ln in body.splitlines()[1:]
             if ln.strip() and not ln.startswith(("#", "!", "<", "|", "※"))]
    sentences = [x.strip() + "。" for ln in lines for x in ln.split("。") if x.strip()]
    # 1行目は記事の中で一番強い「数字の入った一文」（Xで伸びる型＝大きな名前＋数字1つ。11/7 定型の書き出しの紹介は22表示）
    numeric = [x for x in sentences if re.search(r"\d[\d.,]*\s*(%|％|ドル|倍|億|万)", x) and 15 <= len(x) <= 70]
    # 銘柄名（NVDA・HYPEなど）が入った文を優先する（11/13 「価格は$168.89となり…」と主語のない1行目になった）
    # 「ETHに関しても…」のように前の文を受ける書き出しは、1行目だけでは意味が通らない（11/14 19:36）
    numeric = [x for x in numeric if not re.search(r"^.{0,12}(に関しても|についても|も同様|も、)", x)] or numeric
    hook = next((x for x in numeric if re.search(r"[A-Z]{2,}|エヌビディア|ハイパーリキッド|Hyperliquid", x)), numeric[0] if numeric else "")
    hook = re.sub(r"^(また|さらに|一方で|一方|そして|なお)、", "", hook)
    lead = sentences[0] if sentences else ""
    fallback = "Xでは書ききれない数字と図を、記事にまとめました。"
    cands = []
    if hook:
        cands += [f"{hook}\n\nこの理由をnoteで深掘りしました。\n\n「{meta['title']}」\n\n{url}",
                  f"{hook}\n\nnoteで深掘りしました。\n\n{url}"]
    cands += [f"noteで深掘りしました。\n\n「{meta['title']}」\n\n" + (f"{b}\n\n" if b else "") + url
              for b in (lead, fallback, "")]
    for text in cands:
        if checks.weighted_length(text) <= 280:  # 文の途中で切らず、収まらなければ次の形にする
            return text
    return text


def promote(meta: dict, url: str) -> None:
    """noteの記事をXで1回だけ紹介する（Xのフォロワーをnoteへ。11/2 Xからnoteへの導線がなく、記事の表示が27〜61どまり）。"""
    from . import xapi
    if meta.get("promoted"):
        return
    text = promote_text(meta, url)
    media = None
    img = store.DATA_DIR / f"note_articles/{meta['id']}/image1.png"
    if img.exists():
        # リンクだけの投稿は表示が伸びにくい（11/2 紹介投稿は28表示）。記事の図を付けて目に留まりやすくする
        try:
            media = [xapi.upload_image(img.read_bytes())]
        except Exception as e:  # noqa: BLE001 図の失敗では紹介を止めない
            log.warning("紹介投稿の図のアップロードに失敗: %s", e)
    try:
        tweet_id = xapi.post(text, media_ids=media)
    except Exception as e:  # noqa: BLE001 紹介の失敗で確認を止めない
        log.warning("noteの紹介投稿に失敗: %s", e)
        return
    now = now_utc()
    store.record_post({"tweet_id": tweet_id, "slot_key": f"{now.astimezone(JST):%Y%m%d}-note{now.astimezone(JST):%H%M}",
                       "topic_key": f"note:{meta['id']}", "posted_at": now.isoformat(),
                       "posted_at_jst": f"{now.astimezone(JST):%Y-%m-%d %H:%M}", "text": text, "format": "note",
                       "kind": "note", "has_image": bool(media), "decided_by": "auto"})
    meta["promoted"] = now.isoformat()
    store.write_json(f"note_articles/{meta['id']}/meta.json", meta)


def _notify(res: dict, urlname: str) -> None:
    lines = [f"📝 noteの記事を確認しました\n「{res['title']}」\n", "直したほうがよい点があります。"]
    for i, s in enumerate(res["issues"] + res["proofread"], 1):
        lines.append(f"{i}. {s}")
    lines.append(f"\nhttps://note.com/{urlname}/n/{res['key']}")
    line.notify("\n".join(lines)[:4800], essential=True)


def _record_stats(urlname: str) -> None:
    """自分のnote記事の反応（スキ数）を毎日記録する。改善会議で使う。"""
    try:
        items = published_list(urlname)
    except requests.RequestException:
        return
    day = now_utc().astimezone(JST).strftime("%Y-%m-%d")
    stats = store.read_json("note/stats.json", {})
    for n in items:
        stats.setdefault(n["key"], {"title": n.get("name"), "published": n.get("publishAt"), "likes": {}})
        stats[n["key"]]["likes"][day] = n.get("likeCount")
    store.write_json("note/stats.json", stats)


REMIND_AT_JST = (19,)  # この時刻を過ぎても未公開なら1回だけ知らせる（11/13 LINEは必要最低限）


def remind_unpublished() -> list[str]:
    """今日作った記事がまだnoteに公開されていなければ、LINEでもう一度知らせる。"""
    from .note_article import viewer_url

    now = now_utc().astimezone(JST)
    sent = []
    for p in sorted(store.DATA_DIR.glob(f"note_articles/{now:%Y%m%d}-*/meta.json")):
        meta = json.loads(p.read_text())
        if meta.get("status") == "published":
            continue
        reminded = meta.get("reminded", [])
        due = [h for h in REMIND_AT_JST if now.hour >= h and h not in reminded]
        if not due:
            continue
        line.notify(f"📝【リマインド】今日のnote記事がまだ公開されていません\n\n「{meta['title']}」\n\n"
                    f"↓タイトルと本文のコピーはこちら\n{viewer_url(meta['id'])}", essential=True)
        meta["reminded"] = reminded + due
        store.write_json(f"note_articles/{meta['id']}/meta.json", meta)
        sent.append(meta["id"])
    return sent
