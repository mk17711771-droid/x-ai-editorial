"""Gemini API（無料枠）呼び出し。JSONで応答させる。"""
from __future__ import annotations

import json
import re
import time

import requests

from .common import PROMPTS_DIR, env, log, settings

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _loads(text: str) -> dict:
    """JSONの読み込み。軽量モデルが末尾のカンマ・コメント・```囲みを付けることがある（11/17 朝の枠が落ちた）ので直してから読む。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    t = re.sub(r"(?m)^\s*//[^\n]*$", "", t)
    t = re.sub(r",\s*([}\]])", r"\1", t)
    return json.loads(t)


def _body_for(model: str, body: dict) -> dict:
    """Gemma（Geminiとは別の無料枠）は systemInstruction と JSONモード に対応しないので、指示を本文の先頭に入れる。
    11/23〜26 深夜に Gemini の全モデルが429/503で4晩続けて枠を落としたため、最後の予備として使う。"""
    if not model.startswith("gemma"):
        return body
    system = body["systemInstruction"]["parts"][0]["text"]
    user = body["contents"][0]["parts"][0]["text"]
    return {
        "contents": [{"role": "user", "parts": [{"text": f"{system}\n\n---\n\n{user}\n\n出力はJSONオブジェクトだけにしてください（前置き・```は不要）。"}]}],
        "generationConfig": {"temperature": body["generationConfig"].get("temperature", 0.7)},
    }


def generate_json(system: str, user: str, fast: bool = False, light: bool = False) -> dict:
    """fast=True: 待ち時間の短い呼び出し（10分おきに走る速報の見張り用。失敗しても次の回で拾い直す）。
    11/16 GitHub Actionsの無料枠を使い切った。待ち時間も課金されるため、速報の見張りは長く待たない。"""
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
    if fast or light:
        # 回数の多い・軽い呼び出し（速報の見張り10分おき・note校正・トレンドのまとめ）は軽量モデルから使う。
        # 主モデルの1日の無料枠を投稿の原稿に残す（11/23〜25 夜には主モデルが429で使えず、深夜の枠が3晩続けて落ちた）
        lite = [m for m in models if "lite" in m]
        models = lite + [m for m in models if m not in lite]
    missing: set[str] = set()
    last_err: Exception | None = None
    waits = (0, 20) if fast else (0, 30, 60, 120, 240)  # 11/5 混雑が4分以上続き夜の長文が落ちたので、待つ回数とモデルを増やした
    for round_no, wait in enumerate(waits):
        if wait:
            log.info("Geminiが混雑しているため%d秒待って再試行します（%d巡目）", wait, round_no + 1)
            time.sleep(wait)
        for model in models:
            if model in missing:
                continue
            try:
                r = requests.post(API.format(model=model), params={"key": env("GEMINI_API_KEY")}, json=_body_for(model, body), timeout=45)  # 120秒待ちが積み重なりActionsの時間を使っていた（11/16）
                if r.status_code == 404 or (r.status_code == 400 and model.startswith("gemma")):
                    missing.add(model)
                    continue
                if r.status_code in (429, 500, 503):
                    raise requests.HTTPError(f"{r.status_code}: {r.text[:300]}")
                if r.status_code >= 400:
                    # キーの誤りなど、再試行しても直らないエラー
                    raise RuntimeError(f"Gemini APIエラー {r.status_code}（モデル: {model}）: {r.text[:500]}")
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return _loads(text)
            except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as e:
                last_err = e
                log.warning("Gemini（%s）呼び出し失敗: %s", model, str(e)[:200])
                if isinstance(e, json.JSONDecodeError):
                    log.warning("壊れたJSONの先頭: %s", e.doc[:200].replace("\n", " "))
        if len(missing) == len(models):
            raise RuntimeError(f"利用できるGeminiモデルがありません: {', '.join(models)}")
    raise RuntimeError(f"Gemini APIの呼び出しに失敗しました: {last_err}")


def check_models() -> None:
    """予備も含めた各モデルが実際に使えるかを1回ずつ確かめる（tools の llm-check。無料枠）。"""
    cfg = settings()["llm"]
    body = {"systemInstruction": {"parts": [{"text": "JSONで答える"}]},
            "contents": [{"role": "user", "parts": [{"text": '{"ok": true} だけを返して'}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    names = []
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": env("GEMINI_API_KEY"), "pageSize": 200}, timeout=30)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
        log.info("llm-check 使えるモデル一覧: %s", ", ".join(names))
    except Exception as e:  # noqa: BLE001
        log.info("llm-check 一覧の取得に失敗: %s", str(e)[:160])
    extra = [n for n in names if ("flash" in n or n.startswith("gemma")) and "image" not in n and "tts" not in n and "live" not in n]
    for m in dict.fromkeys([cfg["model"]] + cfg.get("fallback_models", []) + extra):
        try:
            r = requests.post(API.format(model=m), params={"key": env("GEMINI_API_KEY")}, json=_body_for(m, body), timeout=45)
            out = r.json()["candidates"][0]["content"]["parts"][0]["text"][:80] if r.status_code == 200 else r.text[:160]
        except Exception as e:  # noqa: BLE001 確認用なので何があっても次のモデルへ
            out = str(e)[:160]
            r = None
        log.info("llm-check %s: %s %s", m, r.status_code if r is not None else "-", out.replace("\n", " "))
