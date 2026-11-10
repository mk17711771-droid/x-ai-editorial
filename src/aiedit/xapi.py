"""X公式API v2 での投稿（OAuth 1.0a ユーザー認証）。"""
from __future__ import annotations

import requests
from requests_oauthlib import OAuth1

from .common import env

API = "https://api.x.com/2"


class XAuthError(Exception):
    """認証・権限エラー。自動運用を停止すべきもの。"""


class XRateLimitError(Exception):
    pass


class XCreditsError(Exception):
    """X APIの利用クレジットが尽きた（402 credits depleted）。管理者の支払いが必要。"""


class XPostError(Exception):
    pass


def _auth() -> OAuth1:
    return OAuth1(
        env("X_API_KEY"),
        env("X_API_SECRET"),
        env("X_ACCESS_TOKEN"),
        env("X_ACCESS_TOKEN_SECRET"),
    )


def upload_image(png: bytes) -> str:
    """画像をアップロードして media_id を返す。"""
    r = requests.post(
        f"{API}/media/upload",
        files={"media": ("chart.png", png, "image/png")},
        data={"media_category": "tweet_image", "media_type": "image/png"},
        auth=_auth(),
        timeout=60,
    )
    if r.status_code >= 400:
        # 画像の失敗では自動運用を止めない（本文だけで投稿する）
        raise XPostError(f"画像アップロード {r.status_code}: {r.text[:300]}")
    data = r.json().get("data", r.json())
    return str(data.get("id") or data.get("media_id_string") or data["media_id"])


def post(text: str, reply_to: str | None = None, media_ids: list[str] | None = None, quote_id: str | None = None) -> str:
    """投稿してIDを返す。

    重複投稿を防ぐため、リクエスト送信後の失敗（タイムアウトなど）では再試行しない。
    送信前の接続失敗のみ1回だけ再試行する。
    """
    body: dict = {"text": text}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}
    if quote_id:
        body["quote_tweet_id"] = quote_id  # 引用投稿（公式の発表を引用すると、その投稿を見ている人にも届く）
    elif media_ids:
        body["media"] = {"media_ids": media_ids}
    for attempt in range(2):
        try:
            r = requests.post(f"{API}/tweets", json=body, auth=_auth(), timeout=30)
            break
        except requests.ConnectionError:
            if attempt == 1:
                raise
    else:  # pragma: no cover
        raise XPostError("unreachable")

    if r.status_code in (401, 403):
        if "duplicate" in r.text.lower():
            raise XPostError(f"重複投稿として拒否されました: {r.text[:300]}")
        if r.status_code == 403 and ("cashtag" in r.text.lower() or "reply to or quote" in r.text.lower()):
            # 本文・投稿の形の問題（キャッシュタグの数／他人の投稿の引用）。認証の問題ではないので自動運用は止めない
            raise XPostError(f"本文がXの制限にかかりました: {r.text[:300]}")
        raise XAuthError(f"{r.status_code}: {r.text[:300]}")
    if r.status_code == 429:
        raise XRateLimitError(r.text[:300])
    if r.status_code == 402:
        raise XCreditsError(r.text[:300])
    if r.status_code >= 400:
        raise XPostError(f"{r.status_code}: {r.text[:300]}")
    return r.json()["data"]["id"]


def delete(tweet_id: str) -> None:
    r = requests.delete(f"{API}/tweets/{tweet_id}", auth=_auth(), timeout=30)
    if r.status_code in (401, 403):
        raise XAuthError(f"{r.status_code}: {r.text[:300]}")
    if r.status_code >= 400:
        raise XPostError(f"{r.status_code}: {r.text[:300]}")


def get_tweets(ids: list[str]) -> list[dict]:
    """自分の投稿の反応（表示回数・いいね・プロフィールクリックなど）を取得する。"""
    out = []
    for i in range(0, len(ids), 100):
        params = {"ids": ",".join(ids[i:i + 100]),
                  "tweet.fields": "public_metrics,non_public_metrics,created_at"}
        r = requests.get(f"{API}/tweets", params=params, auth=_auth(), timeout=30)
        if r.status_code in (400, 403):
            # 非公開指標が取れない場合は公開指標だけで取り直す
            params["tweet.fields"] = "public_metrics,created_at"
            r = requests.get(f"{API}/tweets", params=params, auth=_auth(), timeout=30)
        if r.status_code >= 400:
            raise XPostError(f"投稿の取得 {r.status_code}: {r.text[:300]}")
        out += r.json().get("data", [])
    return out


def me() -> dict:
    r = requests.get(f"{API}/users/me", params={"user.fields": "public_metrics"}, auth=_auth(), timeout=30)
    if r.status_code >= 400:
        raise XPostError(f"アカウント情報の取得 {r.status_code}: {r.text[:300]}")
    return r.json()["data"]


def search_recent(query: str, max_results: int = 30) -> tuple[list[dict], dict]:
    """直近7日の投稿を検索する（同ジャンルのリサーチ用。読んだ件数ぶん課金される）。"""
    params = {
        "query": query, "max_results": max(10, min(max_results, 100)), "sort_order": "relevancy",
        "tweet.fields": "public_metrics,created_at,author_id,attachments",
        "expansions": "author_id", "user.fields": "username,public_metrics",
    }
    r = requests.get(f"{API}/tweets/search/recent", params=params, auth=_auth(), timeout=30)
    if r.status_code >= 400:
        raise XPostError(f"検索 {r.status_code}: {r.text[:300]}")
    body = r.json()
    users = {u["id"]: u for u in body.get("includes", {}).get("users", [])}
    return body.get("data", []), users


def users(usernames: list[str]) -> list[dict]:
    """アカウントの公開情報（認証バッジの種類・作成日・フォロワー数）を調べる。本人確認の参考にする。"""
    r = requests.get(f"{API}/users/by", params={"usernames": ",".join(usernames),
                     "user.fields": "verified,verified_type,created_at,public_metrics,description,affiliation"},
                     auth=_auth(), timeout=30)
    if r.status_code >= 400:
        raise XPostError(f"ユーザー取得 {r.status_code}: {r.text[:300]}")
    return r.json().get("data", [])


def user_tweets(user_id: str, since_id: str | None = None) -> list[dict]:
    """アカウントの新しい投稿（返信・リポストを除く）。since_id より新しいものだけ（初回は最新5件）。"""
    params = {"max_results": 5 if not since_id else 20, "exclude": "retweets,replies", "tweet.fields": "created_at"}
    if since_id:
        params["since_id"] = since_id
    r = requests.get(f"{API}/users/{user_id}/tweets", params=params, auth=_auth(), timeout=30)
    if r.status_code >= 400:
        raise XPostError(f"投稿の取得 {r.status_code}: {r.text[:300]}")
    return r.json().get("data", [])
