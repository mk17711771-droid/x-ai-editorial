"""速報の見張り: Hyperliquid・trade[XYZ]・HIP-3・株式Perpのニュースを見つけ、裏取りして、投稿と管理者への通知を行う。

  見つける: Cloudflare Workerが10分おきにGoogle ニュースとHYPEの急変を見て、見つけたときだけ起動する
            ＋ 2時間ごとのリサーチで公式アカウント（X）の新しい投稿を見る
  裏取り:   公式アカウント・公式発表、または別々の2社以上の報道で確認できたものだけを「確認済み」にする
  動く:     重要度が高く確認済みなら、速報の材料（data/news_facts.json）にして速報枠で投稿し、LINEで知らせる
            深夜（1:00〜6:30）は保留し、朝6:50に投稿する
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

from . import line, llm, sources, store
from .common import JST, env, log, now_utc, settings

# 管理者の指示: 話題が熱いうちに出す。時間帯で保留しない（深夜でもXは動いている）
FOLLOWUP_GAP_HOURS = 3   # 同じ話題が盛り上がり続けていれば、3時間あけて続報を出す
# 自動の続報は出さない（10/30 AIが「新しい事実あり」と誤判定し、ICE・バイナンスの焼き直しを3回出した。管理者の指示: 遅い速報は場違い）
MAX_POSTS_PER_STORY = 1
STORY_FRESH_HOURS = 3
# 大きな出来事の余波は何日も報道が続く。同じ出来事かは7日さかのぼって比べる（11/2 3日前のバイナンスの件の余波を新しい速報として出した）
SAME_EVENT_DAYS = 7


def _cfg() -> dict:
    return settings().get("news_watch", {})


def _id(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode()).hexdigest()[:12]


def official_posts() -> list[dict]:
    """公式アカウントの新しい投稿（前回以降）。読んだ件数ぶん課金されるので月の上限を守る。"""
    from . import xapi

    cfg = _cfg()
    accounts = cfg.get("official_accounts", [])
    if not accounts:
        return []
    month = now_utc().astimezone(JST).strftime("%Y-%m")
    state = store.read_json("news/official_state.json", {})
    used = state.get("reads", {}).get(month, 0)
    if used >= cfg.get("official_monthly_read_limit", 300):
        return []
    ids = state.get("ids") or {}
    missing = [a for a in accounts if a not in ids]
    if missing:
        for u in xapi.users(missing):
            ids[u["username"]] = u["id"]
    out = []
    for name in accounts:
        uid = ids.get(name)
        if not uid:
            continue
        since = state.get("since", {}).get(name)
        try:
            tweets = xapi.user_tweets(uid, since)
        except xapi.XPostError as e:
            log.warning("公式アカウントの取得に失敗 %s: %s", name, e)
            continue
        used += len(tweets)
        if tweets:
            state.setdefault("since", {})[name] = max(t["id"] for t in tweets)
        if since:  # 初回は基準点を作るだけ（過去の投稿を速報扱いしない）
            out += [{"title": t["text"][:400], "source": f"@{name}（公式）", "official": True,
                     "published": t.get("created_at", ""), "url": f"https://x.com/{name}/status/{t['id']}"} for t in tweets]
    state["ids"] = ids
    state.setdefault("reads", {})[month] = used
    store.write_json("news/official_state.json", state)
    return out


def _corroborate(items: list[dict]) -> list[dict]:
    """同じ話題を報じている記事を、直近24時間のニュースから集める（別々の媒体の数で裏取りする）。"""
    extra = []
    for q in _cfg().get("verify_queries", []):
        extra += sources.news_items(q, "when:1d", lang="en") + sources.news_items(q, "when:1d", lang="ja")
    seen = {(i["title"], i.get("source")) for i in items}
    return items + [e for e in extra if (e["title"], e.get("source")) not in seen]


def triage(new: list[dict], context: list[dict], hype_1h: float | None) -> list[dict]:
    """AIで、重要なニュースかどうか・裏取りできているかを判定し、話題ごとにまとめる。"""
    from .common import CONFIG_DIR
    known = [{"story_key": f["key"].removeprefix("news:"), "headline": f["fields"].get("headline", f["summary"][:60])}
             for f in store.read_json("news_facts.json", []) or []]
    payload = {"new_items": new, "related_items_last_24h": context[:60], "hype_1h_change_pct": hype_1h,
               "known_stories": known[-15:]}
    rubric = (CONFIG_DIR / "buzz_patterns.md").read_text(encoding="utf-8")
    out = llm.generate_json(llm.prompt("news_triage") + "\n\n" + rubric, json.dumps(payload, ensure_ascii=False))
    if isinstance(out, list):  # AIが {"stories": [...]} ではなく配列だけを返すことがある（10/30 20:46に停止）
        return [s for s in out if isinstance(s, dict)]
    return [s for s in out.get("stories", []) if isinstance(s, dict)]


# どの話題にも出てくる語（同じ出来事かの判定には使わない）
COMMON_TOKENS = {"hyperliquid", "hype", "hip-3", "trade[xyz]", "tradexyz", "trade.xyz", "ハイパーリキッド", "無期限先物",
                 "暗号資産", "仮想通貨", "ビットコイン", "btc", "eth", "速報", "発表", "市場", "米国", "取引所",
                 "cftc", "sec", "fcm", "etf", "perp", "perps", "dex", "nasdaq", "ナスダック"}  # 規制当局・商品の種類は社名ではない（11/7 KalshiとHL StrategiesをCFTCだけで結んだ）
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.\[\]\-]{1,}|[ァ-ヶー]{3,}|\d[\d,.]*億|[一-龥]{2,}氏")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in TOKEN.findall(text or "")} - COMMON_TOKENS


def same_event(a: str, b: str) -> bool:
    """同じ出来事か（社名・人名・金額などの固有の語が2つ以上重なる）。AIの判定が外れても重複投稿しないための機械の判定。"""
    return len(_tokens(a) & _tokens(b)) >= 2


def _hours_since(iso: str | None) -> float:
    """日時（ISO8601 か RSS の形式）から何時間たったか。読めなければ0（ほかの判定に任せる）。"""
    if not iso:
        return 0.0
    from email.utils import parsedate_to_datetime
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        try:
            t = parsedate_to_datetime(str(iso))
        except (TypeError, ValueError):
            return 0.0
    if t.tzinfo is None:
        return 0.0
    return (now_utc() - t).total_seconds() / 3600


def _reported_earlier(story: dict) -> bool:
    """同じ出来事を報じた記事が、直近30日のうち3時間より前に出ていれば True。"""
    text = f"{story.get('headline', '')} {story.get('summary_ja', '')}"
    words = [w for w in _tokens(text) if not re.match(r"^\d", w)][:3]
    if len(words) < 2:
        return False
    for lang in ("en", "ja"):
        for it in sources.news_items(" ".join(words), "when:30d", lang=lang):
            if _hours_since(it.get("published")) > STORY_FRESH_HOURS and same_event(text, it.get("title", "")):
                return True
    return False


# 他社の記事を集めて載せるだけのサイト（独立した報道として裏取りに数えない。11/2 まとめサイト2つだけの話題を確認済みとして2回投稿した）
AGGREGATORS = ("biggo", "jinacoin", "mexc", "binance square", "coingecko", "tradingview", "coinmarketcap", "kucoin",
               "investing.com", "yahoo", "msn", "smartnews", "gunosy", "livedoor", "goo")


def _verified(story: dict) -> bool:
    outlets = {s.strip() for s in story.get("sources", []) if s and s.strip()
               and not any(a in s.lower() for a in AGGREGATORS)}
    # 「公式」はAIの判断ではなく、出典が公式アカウント・オンチェーン（こちらが直接取った一次情報）のときだけ認める
    # （11/3 AIが報道1社の話題を official=true とし、創業者の発言を未確認のまま投稿した）
    official = any(("（公式）" in s or "（オンチェーン）" in s) for s in story.get("sources", []) if s)
    return official or len(outlets) >= 2


def _notify(story: dict, action: str) -> None:
    line.notify(f"🚨 速報をキャッチ（{'確認済み' if _verified(story) else '確認中'}）\n\n{story['headline']}\n\n"
                f"{story.get('summary_ja', '')[:400]}\n\n出典: {'・'.join(story.get('sources', [])[:4])}\n→ {action}")


def _dispatch_news_slot() -> bool:
    import requests
    token, repo = env("GITHUB_TOKEN", required=False), env("GITHUB_REPOSITORY", required=False)
    if not token or not repo:
        return False
    r = requests.post(f"https://api.github.com/repos/{repo}/dispatches", timeout=30,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                      json={"event_type": "generate", "client_payload": {"slot": "news"}})
    return r.status_code < 300


def _posts_today() -> int:
    today = now_utc().astimezone(JST).strftime("%Y%m%d")
    return sum(1 for p in store.posts() if str(p.get("slot_key", "")).startswith(f"{today}-news"))


def add_fact(story: dict) -> None:
    facts = store.read_json("news_facts.json", []) or []
    key = f"news:{story['story_key']}"
    if any(f["key"] == key for f in facts):
        return
    fields = {k: v for k, v in (story.get("fields") or {}).items() if isinstance(v, str) and v}
    fields.setdefault("headline", story["headline"])
    facts.append({
        "key": key, "kind": "hl", "asof": now_utc().isoformat(),
        "expires_at": (now_utc() + timedelta(hours=36)).isoformat(), "status": "pending",
        "first_reported_at": story.get("first_reported_at") or now_utc().isoformat(),
        "summary": story.get("summary_ja", story["headline"]), "fields": fields, "raw": {},
        "source": {"name": "・".join(story.get("sources", [])[:4]), "url": story.get("url", "")},
    })
    store.write_json("news_facts.json", facts)


def run(payload: dict | None = None) -> dict:
    payload = payload or {}
    seen = store.read_json("news/seen.json", {})
    result = {"at": now_utc().isoformat(), "new": 0, "stories": [], "actions": []}

    if payload.get("flush"):
        # 朝: 深夜に保留した速報があれば投稿する
        pending = [f for f in store.read_json("news_facts.json", []) or [] if f.get("status") == "pending"
                   and datetime.fromisoformat(f["expires_at"]) > now_utc()]
        if pending and _posts_today() < _cfg().get("max_posts_per_day", 3) and _dispatch_news_slot():
            result["actions"].append("保留していた速報を投稿")
        store.write_json("news/last_run.json", result)
        return result

    if payload.get("universe"):
        # Hyperliquidの上場銘柄の一覧（Workerが新規上場を見つけるための基準）を更新する
        store.write_json("hl_universe.json", sorted(payload["universe"]))
        if payload.get("universe_init") and not payload.get("items"):
            store.write_json("news/last_run.json", result)
            return result
    for r in payload.get("records") or []:
        from . import records
        records.update(r["coin"], float(r["price"]))  # 基準を引き上げて、同じ記録で何度も速報しない
    items = list(payload.get("items") or []) + official_posts()
    new = [i for i in items if _id(i["title"]) not in seen]
    for i in new:
        seen[_id(i["title"])] = now_utc().isoformat()
    # 古い既読は捨てる（1週間）
    cutoff = now_utc() - timedelta(days=7)
    seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v) > cutoff}
    store.write_json("news/seen.json", seen)
    result["new"] = len(new)
    hype = payload.get("hype_1h")
    if not new and not (hype is not None and abs(hype) >= 5):
        store.write_json("news/last_run.json", result)
        return result

    try:
        stories = triage(new, _corroborate(new), hype)
    except Exception:
        # AIが一時的に使えないとき（11/4 Geminiの503）は、既読を取り消して次の見張りで判定し直す（ネタを取りこぼさない）
        for i in new:
            seen.pop(_id(i["title"]), None)
        store.write_json("news/seen.json", seen)
        raise
    handled = store.read_json("news/stories.json", {})
    known_keys = {f["key"].removeprefix("news:"): f for f in store.read_json("news_facts.json", []) or []}
    for s in stories:
        # AIが既存の話題の story_key を付けても、見出しが別の出来事なら新しい話題にする
        # （11/7 KalshiのCFTC承認・株式Perpの開始を、Hyperliquid StrategiesのFCM申請の「続報」にまとめて見落とした）
        prev_f = known_keys.get(s.get("story_key"))
        prev_h = (handled.get(s.get("story_key")) or {}).get("headline", "")
        if prev_f or prev_h:
            from .checks import similarity
            text = f"{s.get('headline', '')} {s.get('summary_ja', '')}"
            ref = f"{(prev_f or {}).get('fields', {}).get('headline', '')} {(prev_f or {}).get('summary', '')} {prev_h}"
            if not (_tokens(text) & _tokens(ref)) and similarity(s.get("headline", ""), ref) < 0.3:
                import hashlib
                s["story_key"] = "story-" + hashlib.sha1(s.get("headline", "").encode()).hexdigest()[:8] + "x"
        # 既に扱った話題と同じ出来事なら、同じ story_key にそろえる（同じニュースを別の話題として二度投稿しない）
        if s.get("story_key") not in known_keys:
            from .checks import similarity
            text = f"{s.get('headline', '')} {s.get('summary_ja', '')}"
            same = next((k for k, f in known_keys.items()
                         if similarity(s.get("headline", ""), f["fields"].get("headline", "")) >= 0.3
                         or (now_utc() - datetime.fromisoformat(f["asof"]) < timedelta(days=SAME_EVENT_DAYS)
                             and same_event(text, f"{f['fields'].get('headline', '')} {f.get('summary', '')}"))), None)
            if not same:  # まだ裏取り前（確認中）で知らせた話題とも比べる（11/1 同じKrakenの件を別の名前で2回知らせた）
                same = next((k for k, h in handled.items() if h.get("headline")
                             and now_utc() - datetime.fromisoformat(h["at"]) < timedelta(days=SAME_EVENT_DAYS)
                             and same_event(text, h["headline"])), None)
            if same:
                s["story_key"] = same.rsplit("-", 1)[0] if same.rsplit("-", 1)[-1].isdigit() else same
                handled.setdefault(s["story_key"], {"at": now_utc().isoformat(), "posted_or_queued": True,
                                                    "notified": True, "posts": 1, "last_post_at": now_utc().isoformat()})
        # 重要度が高い、またはバズる条件に4つ以上当てはまるなら速報にする（記録更新など、重要度3でも伸びるネタを逃さない）
        hot = s.get("importance", 0) >= _cfg().get("min_importance", 4) or s.get("buzz_fit", 0) >= 4
        # アカウントの属性からぶれない: 範囲外の話題はどれだけ熱くても出さない
        # 速報＝ネット上でいち早くキャッチした情報。ネットに最初に出てから3時間を過ぎていたら、もう速報ではない（管理者の定義）
        if _hours_since(s.get("first_reported_at")) > STORY_FRESH_HOURS:
            continue
        # 機械の確認: 同じ出来事の報道が3時間より前にすでにあれば、もう速報ではない（11/1 9月発表のKrakenの件を速報扱いした）
        if _reported_earlier(s):
            continue
        # 出来事そのものが古い（昨日・一昨日の話の再報道）なら速報にしない（10/31 2:05 ICEの件を「速報をキャッチ」と知らせた反省）
        # 特定のトレーダー・アドレスの売買を主役にした話題は扱わない（上位トレーダーは集計だけ。11/2「利益上位トレーダー2名が…」）
        individual = re.search(r"(トレーダー|クジラ|大口|whale|アドレス)[^。]{0,6}[0-9０-９一二三]\s*(名|人|人の|つ)", s.get("headline", "") + s.get("summary_ja", ""))
        if not s.get("story_key") or not hot or s.get("in_scope") is False or s.get("fresh") is False or individual:
            continue
        prev = handled.get(s["story_key"], {})
        verified = _verified(s)
        posts = prev.get("posts", 1 if prev.get("posted_or_queued") else 0)
        if posts:
            # 続報: 同じ話題に新しい報道・公式発表が続いている（＝まだ熱い）なら、時間をあけて別の角度で出す
            last = datetime.fromisoformat(prev.get("last_post_at", prev["at"]))
            # 続報は「新しい事実」があるときだけ（報道が続いているだけの焼き直しは出さない。10/30に同じ話題を4回出した反省）
            # 最初の報道から24時間を過ぎた話題は続報にしない（10/30 ICEの件を18時間後に焼き直した反省）
            stale = now_utc() - datetime.fromisoformat(prev["at"]) > timedelta(hours=STORY_FRESH_HOURS)
            if (posts >= MAX_POSTS_PER_STORY or now_utc() - last < timedelta(hours=FOLLOWUP_GAP_HOURS) or not verified
                    or not s.get("new_development") or stale):
                if s.get("new_development"):
                    # 自動の続報は出さないが、黙って捨てない。定期点検が裏取りして判断する（11/1 バイナンスの出金再開・FBIの発表を拾えなかった）
                    store.append_jsonl("news/followups.jsonl", {
                        "at": now_utc().isoformat(), "story_key": s["story_key"], "headline": s.get("headline", ""),
                        "summary": s.get("summary_ja", "")[:300], "sources": s.get("sources", []), "verified": verified})
                continue
        elif prev.get("notified") and not verified:
            continue
        result["stories"].append({k: s.get(k) for k in ("story_key", "headline", "importance", "buzz_fit",
                                                         "buzz_reasons", "sources", "official")})
        if verified:
            if posts:
                s["story_key"] = f"{s['story_key']}-{posts + 1}"  # 続報は別の材料として扱う
            add_fact(s)
            from .pipeline import recent_news_post
            if _posts_today() >= _cfg().get("max_posts_per_day", 3):
                action = "今日の速報投稿の上限に達したため、通常の投稿の材料に回します"
            elif recent_news_post(minutes=120):
                action = "直前に速報を出したばかりなので、2時間あけてから投稿します（自動で出し直します）"
            elif _dispatch_news_slot():
                action = "続報の投稿を始めました（投稿できたら投稿のお知らせが届きます）" if posts else "速報の投稿を始めました（投稿できたら投稿のお知らせが届きます）"
            else:
                action = "投稿の起動に失敗。次の見回りで再試行します"
            key = s["story_key"].rsplit("-", 1)[0] if posts else s["story_key"]
            handled[key] = {"at": prev.get("at", now_utc().isoformat()), "posted_or_queued": True, "notified": True,
                            "headline": s.get("headline", ""),
                            "posts": posts + 1, "last_post_at": now_utc().isoformat()}
        else:
            action = "まだ1社の報道だけなので、裏取りできしだい投稿します"
            handled[s["story_key"]] = {"at": now_utc().isoformat(), "notified": True, "headline": s.get("headline", "")}
        _notify(s, action)
        result["actions"].append(f"{s['story_key']}: {action}")
    store.write_json("news/stories.json", handled)
    store.append_jsonl("news/log.jsonl", result)
    store.write_json("news/last_run.json", result)
    return result
