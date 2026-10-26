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
    t = re.sub(r"<br\s*/?>|</p>|</h\d>|</li>", "\n", body_html)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{2,}", "\n", t).strip()


def _md_plain(md_text: str) -> str:
    t = re.sub(r"^#+\s*", "", md_text, flags=re.M)
    t = t.replace("**", "")
    return re.sub(r"\n{2,}", "\n", t).strip()


def _nums(text: str) -> set[str]:
    out = set()
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

    src = _md_plain(gen["md"].split("\n---\n")[0])
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
        if not key or key in checked or (pub_at and pub_at < since):
            continue
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
        checked[key] = {"title": title, "at": res["checked_at"], "ok": not (issues or typos)}
        if gen:
            meta = store.read_json(f"note_articles/{gen['id']}/meta.json")
            meta.update(status="published", note_key=key, published_at=pub)
            store.write_json(f"note_articles/{gen['id']}/meta.json", meta)
        if issues or typos:
            _notify(res, urlname)
        results.append(res)
    store.write_json("note/checked.json", checked)
    _record_stats(urlname)
    return results


def _notify(res: dict, urlname: str) -> None:
    lines = [f"📝 noteの記事を確認しました\n「{res['title']}」\n", "直したほうがよい点があります。"]
    for i, s in enumerate(res["issues"] + res["proofread"], 1):
        lines.append(f"{i}. {s}")
    lines.append(f"\nhttps://note.com/{urlname}/n/{res['key']}")
    line.notify("\n".join(lines)[:4800])


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
