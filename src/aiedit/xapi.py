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


def post(text: str, reply_to: str | None = None, media_ids: list[str] | None = None) -> str:
    """投稿してIDを返す。

    重複投稿を防ぐため、リクエスト送信後の失敗（タイムアウトなど）では再試行しない。
    送信前の接続失敗のみ1回だけ再試行する。
    """
    body: dict = {"text": text}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}
    if media_ids:
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
        raise XAuthError(f"{r.status_code}: {r.text[:300]}")
    if r.status_code == 429:
        raise XRateLimitError(r.text[:300])
    if r.status_code >= 400:
        raise XPostError(f"{r.status_code}: {r.text[:300]}")
    return r.json()["data"]["id"]


def delete(tweet_id: str) -> None:
    r = requests.delete(f"{API}/tweets/{tweet_id}", auth=_auth(), timeout=30)
    if r.status_code in (401, 403):
        raise XAuthError(f"{r.status_code}: {r.text[:300]}")
    if r.status_code >= 400:
        raise XPostError(f"{r.status_code}: {r.text[:300]}")
