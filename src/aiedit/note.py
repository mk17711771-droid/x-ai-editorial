"""note連携。まずは公開情報（プロフィール・記事一覧）の取得から。"""
from __future__ import annotations

import requests

from . import store
from .common import now_utc

API = "https://note.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; x-ai-editorial/1.0)"}


def fetch_profile(urlname: str) -> dict:
    """クリエイターの公開プロフィールと記事一覧を取得して data/note/ に保存する。"""
    r = requests.get(f"{API}/v2/creators/{urlname}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    creator = r.json().get("data", {})
    notes = []
    rn = requests.get(f"{API}/v2/creators/{urlname}/contents", params={"kind": "note", "page": 1},
                      headers=HEADERS, timeout=30)
    if rn.ok:
        notes = rn.json().get("data", {}).get("contents", [])
    out = {
        "fetched_at": now_utc().isoformat(),
        "nickname": creator.get("nickname"),
        "urlname": creator.get("urlname"),
        "profile": creator.get("profile"),
        "follower_count": creator.get("followerCount"),
        "following_count": creator.get("followingCount"),
        "note_count": creator.get("noteCount"),
        "has_icon": bool(creator.get("profileImageUrl")) and "default" not in str(creator.get("profileImageUrl")),
        "header_image_url": creator.get("headerImageUrl"),
        "profile_image_url": creator.get("profileImageUrl"),
        "socials": creator.get("socials") or creator.get("twitterNickname"),
        "notes": [{"name": n.get("name"), "price": n.get("price"), "likeCount": n.get("likeCount"),
                   "publishAt": n.get("publishAt")} for n in notes[:20]],
        "raw_keys": sorted(creator.keys()),
    }
    store.write_json(f"note/profile_{urlname}.json", out)
    # アイコンとヘッダーも確認用に保存する
    for name, url in (("icon.jpg", out["profile_image_url"]), ("header.png", out["header_image_url"])):
        if url:
            img = requests.get(url.split("?")[0] + "?width=1280", headers=HEADERS, timeout=30)
            if img.ok:
                store.write_bytes(f"note/{urlname}_{name}", img.content)
    return out


def audit_notes(urlname: str) -> list[dict]:
    """公開中の記事を全件取得し、本文中のリンク・紹介コード・勧誘表現を洗い出す（整理の判断用）。"""
    import re

    items, page = [], 1
    while page < 20:
        r = requests.get(f"{API}/v2/creators/{urlname}/contents", params={"kind": "note", "page": page},
                         headers=HEADERS, timeout=30)
        if not r.ok:
            break
        data = r.json().get("data", {})
        items += data.get("contents", [])
        if data.get("isLastPage", True):
            break
        page += 1
    risky = re.compile(r"(紹介コード|招待コード|リファラル|referral|ref=|登録はこちら|今すぐ|買うべき|億り人|爆益|確定|稼げ|GET|購入)", re.I)
    out = []
    for n in items:
        body = ""
        d = requests.get(f"{API}/v3/notes/{n['key']}", headers=HEADERS, timeout=30)
        if d.ok:
            body = d.json().get("data", {}).get("body", "") or ""
        text = re.sub(r"<[^>]+>", " ", body)
        links = sorted(set(re.findall(r'href="([^"]+)"', body)))
        out.append({
            "key": n["key"], "title": n.get("name"), "publishAt": n.get("publishAt"),
            "likes": n.get("likeCount"), "price": n.get("price"), "chars": len(text),
            "external_links": [u for u in links if "note.com" not in u][:10],
            "risky_words": sorted(set(m.group(0) for m in risky.finditer(text + " " + (n.get("name") or ""))))[:10],
            "head": re.sub(r"\s+", " ", text)[:200],
        })
    store.write_json(f"note/audit_{urlname}.json", out)
    return out
