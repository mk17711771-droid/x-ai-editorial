"""外部リサーチ: ニュース・noteの人気記事・Xの同ジャンルの伸びた投稿を集める。

ここで集めたものは「流れ・型・テーマ」の参考にだけ使い、投稿の数字には使わない（数字は必ず材料から）。
他者の文章は転載しない。改善会議では「なぜ伸びたか（書き出し・構成・画像の使い方）」だけを抽出する。
"""
from __future__ import annotations

import html
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

from . import store
from .common import JST, log, now_utc, settings

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; x-ai-editorial/1.0)"}


def _cfg() -> dict:
    return settings().get("research", {})


def _error(kind: str, query: str, e: object) -> None:
    """取得に失敗した理由を残す（Actionsのログを見なくても原因がわかるように）。"""
    store.append_jsonl("sources/errors.jsonl", {"at": now_utc().isoformat(), "kind": kind, "query": query,
                                                "error": str(e)[:300]})


# ---------- ニュース（Google ニュースのRSS。無料） ----------

def news() -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for q in _cfg().get("news_queries", []):
        url = f"https://news.google.com/rss/search?q={quote(q)}+when:1d&hl=ja&gl=JP&ceid=JP:ja"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except (requests.RequestException, ET.ParseError) as e:
            log.warning("ニュースの取得に失敗 %s: %s", q, e)
            continue
        for it in list(root.iter("item"))[:10]:
            title = (it.findtext("title") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            src = it.find("source")
            items.append({"query": q, "title": title, "source": src.text if src is not None else "",
                          "published": it.findtext("pubDate") or ""})
    if items:
        # 2時間ごとに集め、その日の分に新しい見出しを足していく（新しいものを先頭に）
        path = f"sources/news/{now_utc().astimezone(JST):%Y%m%d}.json"
        old = store.read_json(path, []) or []
        known = {i["title"] for i in old}
        store.write_json(path, [i for i in items if i["title"] not in known] + old)
    return items


def news_items(q: str, window: str = "when:1d", lang: str = "ja") -> list[dict]:
    """Google ニュースの検索結果（見出し・媒体・日時）。速報の裏取りに使う。"""
    loc = "hl=ja&gl=JP&ceid=JP:ja" if lang == "ja" else "hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(f"https://news.google.com/rss/search?q={quote(q)}+{quote(window)}&{loc}", headers=HEADERS, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as e:
        _error("news", q, e)
        return []
    out = []
    for it in list(root.iter("item"))[:20]:
        src = it.find("source")
        out.append({"title": (it.findtext("title") or "").strip(), "source": src.text if src is not None else "",
                    "published": it.findtext("pubDate") or "", "url": it.findtext("link") or ""})
    return out


def hot_searches() -> list[dict]:
    """日本でいま検索が急増している言葉（Googleトレンド。無料）。旬の話題をつかむ参考にする。"""
    try:
        r = requests.get("https://trends.google.com/trending/rss?geo=JP", headers=HEADERS, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as e:
        _error("trends", "geo=JP", e)
        return []
    ns = {"ht": "https://trends.google.com/trending/rss"}
    out = []
    for it in list(root.iter("item"))[:20]:
        news = [n.findtext("ht:news_item_title", "", ns) for n in it.findall("ht:news_item", ns)]
        out.append({"term": (it.findtext("title") or "").strip(), "traffic": it.findtext("ht:approx_traffic", "", ns),
                    "news": [x for x in news if x][:2]})
    if out:
        store.write_json(f"sources/hot/{now_utc().astimezone(JST):%Y%m%d%H}.json", out)
    return out


def hot_digest(n: int = 12) -> str:
    """編集長AIに渡す、直近の検索トレンド（範囲内の話題があれば旬として使う）。"""
    d = store.DATA_DIR / "sources/hot"
    files = sorted(d.glob("*.json")) if d.exists() else []
    if not files:
        return ""
    rows = store.read_json(f"sources/hot/{files[-1].name}", []) or []
    return "\n".join(f"- {r['term']}（{r.get('traffic', '')}）" + (f": {r['news'][0]}" if r.get("news") else "")
                     for r in rows[:n])


def news_digest(n: int = 8) -> str:
    """編集長AIに渡す、直近のニュース見出し。"""
    today = now_utc().astimezone(JST)
    for d in (today, today - timedelta(days=1)):
        items = store.read_json(f"sources/news/{d:%Y%m%d}.json")
        if items:
            return "\n".join(f"- {i['title']}" for i in items[:n])
    return ""


# ---------- noteの人気記事（公開の検索。無料） ----------

NOTE_ENDPOINTS = [
    # 検索API（v3）は Actions から 403 になることがある。ハッシュタグの一覧も順に試し、取れたものを使う
    ("https://note.com/api/v3/searches", lambda q: {"context": "note", "q": q, "size": 20, "start": 0, "sort": "popular"}),
    ("https://note.com/api/v3/hashtags/{tag}/notes", lambda q: {"order": "popular", "page": 1}),
    ("https://note.com/api/v2/hashtags/{tag}/notes", lambda q: {"order": "popular", "page": 1}),
]


def _find_notes(obj) -> list[dict]:
    """APIの形が変わっても拾えるよう、「タイトル＋スキ数」を持つ要素を探す。"""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) and "name" in x and ("like_count" in x or "likeCount" in x) for x in obj):
            return obj
        for x in obj:
            if (found := _find_notes(x)):
                return found
    elif isinstance(obj, dict):
        for v in obj.values():
            if (found := _find_notes(v)):
                return found
    return []


def note_trends() -> list[dict]:
    """同ジャンルのnoteの人気記事（タイトル・スキ数・価格・書き手）。noteの戦略づくりに使う。"""
    out: list[dict] = []
    probe = {}
    for q in _cfg().get("note_queries", []):
        notes = []
        for url, params in NOTE_ENDPOINTS:
            u = url.format(tag=quote(q.lstrip("#")))
            try:
                r = requests.get(u, params=params(q), headers=HEADERS, timeout=30)
                r.raise_for_status()
                notes = _find_notes(r.json())
                probe[f"{url}|{q}"] = f"ok {len(notes)}"
            except (requests.RequestException, ValueError) as e:
                probe[f"{url}|{q}"] = str(e)[:120]
                continue
            if notes:
                break
        if not notes:
            _error("note", q, "どの取得方法でも0件")
        for n in notes:
            out.append({
                "query": q, "title": n.get("name"),
                "likes": n.get("like_count", n.get("likeCount")),
                "price": n.get("price"),
                "creator": (n.get("user") or {}).get("urlname"),
                "creator_followers": (n.get("user") or {}).get("follower_count", (n.get("user") or {}).get("followerCount")),
                "published": n.get("publish_at", n.get("publishAt")),
                "hashtags": [h.get("hashtag", {}).get("name") if isinstance(h, dict) else h for h in (n.get("hashtags") or [])][:8],
            })
    store.write_json("sources/note_probe.json", probe)
    if out:
        store.write_json(f"sources/note/{now_utc().astimezone(JST):%Y%m%d}.json", out)
    return out


# ---------- Xの同ジャンルの投稿（有料API。月の上限つき） ----------

def _reads_this_month() -> int:
    month = now_utc().astimezone(JST).strftime("%Y-%m")
    return sum(r["n"] for r in store.read_jsonl("sources/x_reads.jsonl") if r["month"] == month)


def x_search() -> list[dict]:
    """同ジャンルで伸びた投稿を検索する。読んだ件数ぶん課金されるので、月の上限を守る。"""
    from . import xapi

    cfg = _cfg().get("x", {})
    if not cfg.get("enabled"):
        return []
    budget = cfg.get("monthly_read_limit", 400) - _reads_this_month()
    queries = list(cfg.get("queries", []))
    if cfg.get("rotate", True) and queries:
        # 毎日1つずつ順番に検索する（毎日新しい情報を入れつつ、月の上限内に収める）
        queries = [queries[now_utc().astimezone(JST).toordinal() % len(queries)]]
    accounts = cfg.get("accounts", [])
    if accounts:
        queries.append("(" + " OR ".join(f"from:{a.lstrip('@')}" for a in accounts) + ") -is:retweet -is:reply")
    out: list[dict] = []
    for q in queries:
        # X APIの検索は1回10件から（10/30に8件にしたら毎回ここで止まり、3日間調べられていなかった）
        size = min(max(cfg.get("per_query", 30), 10), budget)
        if size < 10:
            log.info("Xリサーチは今月の上限に達しました")
            break
        try:
            tweets, users = xapi.search_recent(q, size)
        except xapi.XPostError as e:
            log.warning("Xの検索に失敗 %s: %s", q, e)
            _error("x", q, e)
            continue
        budget -= len(tweets)
        store.append_jsonl("sources/x_reads.jsonl", {"month": now_utc().astimezone(JST).strftime("%Y-%m"),
                                                     "at": now_utc().isoformat(), "n": len(tweets)})
        for t in tweets:
            m = t.get("public_metrics", {})
            u = users.get(t.get("author_id"), {})
            followers = (u.get("public_metrics") or {}).get("followers_count") or 0
            out.append({
                "query": q, "author": u.get("username"), "author_followers": followers,
                "text": html.unescape(t.get("text", ""))[:400], "created_at": t.get("created_at"),
                "has_media": bool((t.get("attachments") or {}).get("media_keys")),
                "likes": m.get("like_count", 0), "reposts": m.get("retweet_count", 0) + m.get("quote_count", 0),
                "bookmarks": m.get("bookmark_count", 0), "impressions": m.get("impression_count", 0),
            })
    if out:
        out.sort(key=lambda x: x["likes"] + 2 * x["reposts"] + 2 * x["bookmarks"], reverse=True)
        store.write_json(f"sources/x/{now_utc().astimezone(JST):%Y%m%d}.json", out)
    return out


def reference_accounts() -> int:
    """管理者が「良い投稿で勉強になる」と選んだアカウントの投稿を毎日読む（11/19 管理者の指示: @yozawape）。

    反応が出そろった投稿だけ見るため、24〜48時間前の投稿を1日1回だけ読む（同じ投稿を二度読まない＝費用を抑える）。
    data/sources/ref/<アカウント>.jsonl に貯め、編集長・執筆AIが「伸びた型」だけを参考にする（文章は真似しない）。"""
    from . import xapi

    cfg = _cfg().get("x", {})
    accounts = [a.lstrip("@").lower() for a in cfg.get("reference_accounts", [])]
    if not accounts:
        return 0
    budget = cfg.get("monthly_read_limit", 400) - _reads_this_month()
    ids = store.read_json("sources/ref_ids.json", {}) or {}
    missing = [a for a in accounts if a not in ids]
    if missing:
        try:
            for u in xapi.users(missing):
                ids[u["username"].lower()] = u["id"]  # 表示上の大文字・小文字（YozawaPe）に左右されない
            store.write_json("sources/ref_ids.json", ids)
        except xapi.XPostError as e:
            _error("x", "reference_accounts", e)
            return 0
    now = now_utc().replace(minute=0, second=0, microsecond=0)
    start, end = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ"), (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = 0
    for a in accounts:
        if a not in ids or budget < 5:
            continue
        done = store.read_json("sources/ref_done.json", {}) or {}
        if done.get(a) == now.strftime("%Y-%m-%d"):
            continue  # 1日1回だけ
        try:
            tweets = xapi.user_tweets_window(ids[a], start, end, max_results=min(20, budget))
        except xapi.XPostError as e:
            _error("x", f"ref:{a}", e)
            continue
        budget -= len(tweets)
        total += len(tweets)
        store.append_jsonl("sources/x_reads.jsonl", {"month": now_utc().astimezone(JST).strftime("%Y-%m"),
                                                     "at": now_utc().isoformat(), "n": len(tweets)})
        for t in tweets:
            m = t.get("public_metrics", {})
            store.append_jsonl(f"sources/ref/{a}.jsonl", {
                "id": t.get("id"), "created_at": t.get("created_at"), "text": html.unescape(t.get("text", ""))[:600],
                "has_media": bool((t.get("attachments") or {}).get("media_keys")),
                "likes": m.get("like_count", 0), "reposts": m.get("retweet_count", 0) + m.get("quote_count", 0),
                "bookmarks": m.get("bookmark_count", 0), "impressions": m.get("impression_count", 0)})
        done[a] = now.strftime("%Y-%m-%d")
        store.write_json("sources/ref_done.json", done)
    return total


def reference_posts(days: int = 14, n: int = 6) -> list[dict]:
    """参考アカウントの直近の投稿のうち、反応がよかったもの（保存・リポストを重く見る）。"""
    cfg = _cfg().get("x", {})
    since = now_utc() - timedelta(days=days)
    rows = []
    for a in [a.lstrip("@").lower() for a in cfg.get("reference_accounts", [])]:
        for r in store.read_jsonl(f"sources/ref/{a}.jsonl"):
            try:
                if datetime.fromisoformat(str(r.get("created_at", "")).replace("Z", "+00:00")) < since:
                    continue
            except ValueError:
                continue
            rows.append({**r, "author": a})
    rows.sort(key=lambda r: r.get("likes", 0) + 3 * r.get("reposts", 0) + 3 * r.get("bookmarks", 0), reverse=True)
    return rows[:n]


def reference_digest() -> str:
    """編集長・執筆AIに渡す、参考アカウントの伸びた投稿（型の参考。文章・数字は使わない）。"""
    rows = reference_posts()
    if not rows:
        return ""
    lines = [f"- @{r['author']}（いいね{r.get('likes', 0)}・保存{r.get('bookmarks', 0)}・図{'あり' if r.get('has_media') else 'なし'}）: "
             + r["text"].replace("\n", " / ")[:220] for r in rows]
    return "\n".join(lines)


def latest(kind: str, days: int = 8) -> list:
    """sources/<kind>/ の直近のファイルを新しい順にまとめて返す。"""
    today = now_utc().astimezone(JST)
    rows: list = []
    for i in range(days):
        rows += store.read_json(f"sources/{kind}/{today - timedelta(days=i):%Y%m%d}.json", []) or []
    return rows


def trend_digest() -> str:
    """集めた情報（同ジャンルで伸びた投稿・ニュース・自分の投稿の反応）から「いま注目される型と話題」をまとめる。

    編集長・執筆AIが毎回読む（data/trends.md）。数字は投稿に使わせない。
    """
    from . import llm

    mine = store.read_json("metrics_now.json", {}).get("posts", [])
    material = {
        "x_top_posts": [{k: x.get(k) for k in ("author_followers", "text", "has_media", "likes", "reposts", "bookmarks")}
                        for x in latest("x", 7)[:25]],
        "news_headlines": [n["title"] for n in latest("news", 2)][:40],
        # 管理者が選んだ参考アカウント（@yozawape など）の伸びた投稿。型だけを抜き出す
        "reference_account_top_posts": [{k: x.get(k) for k in ("author", "text", "has_media", "likes", "reposts", "bookmarks")}
                                        for x in reference_posts()],
        "my_recent_posts": [{k: r.get(k) for k in ("text", "impressions", "likes", "bookmarks", "has_image", "format")}
                            for r in mine][-12:],
    }
    out = llm.generate_json(llm.prompt("trends"), json.dumps(material, ensure_ascii=False))
    lines = [f"- {s}" for s in (out.get("patterns", []) + out.get("topics", []))[:10]]
    if lines:
        text = f"（{now_utc().astimezone(JST):%m/%d %H:%M}更新・外部リサーチから）\n" + "\n".join(lines) + "\n"
        store.write_text("trends.md", text)
        return text
    return ""


def run(daily: bool, weekly: bool, news_only: bool = False) -> dict:
    res = {"news": len(news()), "hot": len(hot_searches())}
    if news_only:
        return res
    if daily or weekly:
        res["note"] = len(note_trends())
        res["x"] = len(x_search())
        try:
            res["ref"] = reference_accounts()
        except Exception as e:  # noqa: BLE001 参考アカウントの失敗でほかの収集を止めない
            log.warning("参考アカウントの取得に失敗: %s", e)
        try:
            res["digest"] = bool(trend_digest())
        except Exception as e:  # noqa: BLE001 まとめの失敗で収集結果を捨てない
            log.warning("トレンドのまとめに失敗: %s", e)
    return res
