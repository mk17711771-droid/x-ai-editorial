"""Gemini API（無料枠）呼び出し。JSONで応答させる。"""
from __future__ import annotations

import json
import time

import requests

from .common import PROMPTS_DIR, env, log, settings

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def generate_json(system: str, user: str) -> dict:
    cfg = settings()["llm"]
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": cfg.get("temperature", 0.7),
            "responseMimeType": "application/json",
        },
    }
    url = API.format(model=cfg["model"])
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(url, params={"key": env("GEMINI_API_KEY")}, json=body, timeout=120)
            if r.status_code in (429, 500, 503):
                raise requests.HTTPError(f"{r.status_code}: {r.text[:300]}")
            if r.status_code >= 400:
                # キーやモデル名の誤りなど、再試行しても直らないエラー
                raise RuntimeError(f"Gemini APIエラー {r.status_code}（モデル: {cfg['model']}）: {r.text[:500]}")
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as e:
            last_err = e
            log.warning("Gemini呼び出し失敗（%d回目）: %s", attempt + 1, e)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Gemini APIの呼び出しに失敗しました: {last_err}")
