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
    # 混雑（429/503）は一時的なことが多い。モデルを順に試し、全滅したら待ってからもう一巡する
    models = [cfg["model"]] + [m for m in cfg.get("fallback_models", []) if m != cfg["model"]]
    missing: set[str] = set()
    last_err: Exception | None = None
    for round_no, wait in enumerate((0, 30, 60, 120, 240)):  # 11/5 混雑が4分以上続き夜の長文が落ちたので、待つ回数とモデルを増やした
        if wait:
            log.info("Geminiが混雑しているため%d秒待って再試行します（%d巡目）", wait, round_no + 1)
            time.sleep(wait)
        for model in models:
            if model in missing:
                continue
            try:
                r = requests.post(API.format(model=model), params={"key": env("GEMINI_API_KEY")}, json=body, timeout=120)
                if r.status_code == 404:
                    missing.add(model)
                    continue
                if r.status_code in (429, 500, 503):
                    raise requests.HTTPError(f"{r.status_code}: {r.text[:300]}")
                if r.status_code >= 400:
                    # キーの誤りなど、再試行しても直らないエラー
                    raise RuntimeError(f"Gemini APIエラー {r.status_code}（モデル: {model}）: {r.text[:500]}")
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as e:
                last_err = e
                log.warning("Gemini（%s）呼び出し失敗: %s", model, str(e)[:200])
        if len(missing) == len(models):
            raise RuntimeError(f"利用できるGeminiモデルがありません: {', '.join(models)}")
    raise RuntimeError(f"Gemini APIの呼び出しに失敗しました: {last_err}")
