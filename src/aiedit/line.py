"""LINE Messaging APIで管理者に承認依頼・異常通知を送る。

無料プランは月200通まで（Push送信のみカウント）。1回の投稿枠につき1通にまとめて送る。
"""
from __future__ import annotations

import requests

from .common import env, log

PUSH_URL = "https://api.line.me/v2/bot/message/push"


def push(messages: list[dict]) -> bool:
    token = env("LINE_CHANNEL_ACCESS_TOKEN", required=False)
    to = env("LINE_ADMIN_USER_ID", required=False)
    if not token or not to:
        log.warning("LINEの設定がないため通知をスキップしました")
        return False
    r = requests.post(
        PUSH_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"to": to, "messages": messages[:5]},
        timeout=20,
    )
    if r.status_code >= 400:
        log.error("LINE送信失敗: %s %s", r.status_code, r.text[:300])
        return False
    return True


def notify(text: str) -> bool:
    return push([{"type": "text", "text": text[:4900]}])


def _preview(text: str, limit: int = 900) -> str:
    return text if len(text) <= limit else text[:limit] + "…（続きはXで）"


def _bubble(draft: dict) -> dict:
    sc = draft["scores"]
    flags = []
    if draft.get("needs_human"):
        flags.append("⚠ 未検証ニュースを含む")
    sources = " / ".join(sorted({s["name"] for s in draft["sources"]}))
    meta = f"{draft['total']}点（新{sc['novelty']} 関{sc['interest']} 数{sc['evidence']} 実{sc['utility']} 独{sc['insight']}）・{draft['style']}"

    def button(label: str, action: str, style: str) -> dict:
        return {
            "type": "button",
            "style": style,
            "height": "sm",
            "action": {"type": "postback", "label": label, "data": f"a={action}&id={draft['id']}",
                       "displayText": f"{label}: {draft['id']}"},
        }

    return {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": draft["slot_theme"], "size": "xs", "color": "#888888", "wrap": True},
                {"type": "text", "text": _preview(draft["text"]), "size": "sm", "wrap": True},
                {"type": "separator"},
                {"type": "text", "text": meta, "size": "xxs", "color": "#888888", "wrap": True},
                {"type": "text", "text": f"出典: {sources}", "size": "xxs", "color": "#888888", "wrap": True},
                *([{"type": "text", "text": " ".join(flags), "size": "xs", "color": "#D9534F", "wrap": True}] if flags else []),
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                button("承認して投稿", "approve", "primary"),
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "contents": [
                        button("却下:質が低い", "reject_quality", "secondary"),
                        button("却下:不正確", "reject_inaccurate", "secondary"),
                    ],
                },
            ],
        },
    }


def send_approval_request(drafts: list[dict], expiry_hours: int, note: str | None = None) -> bool:
    if not drafts:
        return False
    head = [{"type": "text", "text": note[:4900]}] if note else []
    return push(head + [{
        "type": "flex",
        "altText": f"投稿の承認依頼が{len(drafts)}件あります",
        "contents": {"type": "carousel", "contents": [_bubble(d) for d in drafts[:10]]},
    }, {
        "type": "text",
        "text": f"承認は{expiry_hours}時間以内にお願いします。1枠につき承認できるのは1件までです。「停止」と送ると自動運用を止められます。",
    }])


def send_posted_notice(draft: dict, tweet_id: str) -> bool:
    """全自動モードで投稿したことを知らせる。問題があればワンタップで削除できる。"""
    return push([{
        "type": "flex",
        "altText": "自動で投稿しました",
        "contents": {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {"type": "text", "text": f"✅ 自動で投稿しました（{draft['total']}点）", "size": "xs", "color": "#888888"},
                    {"type": "text", "text": _preview(draft["text"]), "size": "sm", "wrap": True},
                ],
            },
            "footer": {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "contents": [
                    {"type": "button", "style": "secondary", "height": "sm",
                     "action": {"type": "uri", "label": "Xで見る", "uri": f"https://x.com/i/status/{tweet_id}"}},
                    {"type": "button", "style": "secondary", "height": "sm",
                     "action": {"type": "postback", "label": "削除する", "data": f"a=delete&id={draft['id']}",
                                "displayText": f"削除: {draft['id']}"}},
                ],
            },
        },
    }])
