"""共通設定・パス・時刻ユーティリティ。"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"
DATA_DIR = Path(os.environ.get("AIEDIT_DATA_DIR", ROOT / "data"))

JST = ZoneInfo("Asia/Tokyo")
NY = ZoneInfo("America/New_York")

log = logging.getLogger("aiedit")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@lru_cache(maxsize=1)
def settings() -> dict:
    with open(CONFIG_DIR / "settings.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # GitHubリポジトリ変数による上書き
    if os.environ.get("APPROVAL_MODE"):
        cfg["mode"] = "approval" if os.environ["APPROVAL_MODE"].lower() in ("1", "true", "yes", "approval") else "auto"
    if os.environ.get("GEMINI_MODEL"):
        cfg["llm"]["model"] = os.environ["GEMINI_MODEL"]
    if os.environ.get("X_PREMIUM"):
        cfg["account"]["premium"] = os.environ["X_PREMIUM"].lower() in ("1", "true", "yes")
    return cfg


def read_list(name: str) -> list[str]:
    """コメント行と空行を除いたリストファイルを読む。"""
    lines = (CONFIG_DIR / name).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def now_utc() -> datetime:
    override = os.environ.get("AIEDIT_NOW")  # テスト用: ISO8601
    if override:
        return datetime.fromisoformat(override).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def now_jst() -> datetime:
    return now_utc().astimezone(JST)


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        raise RuntimeError(f"環境変数 {name} が設定されていません")
    return value


WEEKDAYS_JA = "月火水木金土日"


def fmt_jst(dt: datetime) -> str:
    """例: 10/26(月) 07:30"""
    d = dt.astimezone(JST)
    return f"{d.month}/{d.day}({WEEKDAYS_JA[d.weekday()]}) {d:%H:%M}"
