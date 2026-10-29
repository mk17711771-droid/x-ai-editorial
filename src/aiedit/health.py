"""見回り: 投稿漏れ・停止・処理の失敗を見つけ、直せるものはその場で直し、結果を data/health.json に残す。

ここで直せない問題（原因の調査やコードの修正が必要なもの）は、定期点検（Claude）が health.json を読んで対応し、
再発防止策を data/improve/incidents.md に記録する。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import requests

from . import store
from .common import env, log, now_jst, now_utc, settings

GH_API = "https://api.github.com"


def _slot_applies(slot: dict, day: datetime) -> bool:
    if slot["days"] == "manual":
        return False
    weekend = day.weekday() >= 5
    return not (slot["days"] == "weekday" and weekend or slot["days"] == "weekend" and not weekend)


def missed_slots(now: datetime | None = None) -> list[dict]:
    """今日の枠のうち、投稿時間帯が過ぎたのに投稿されていないもの。"""
    now = now or now_jst()
    cfg = settings()
    posted = {p.get("slot_key") for p in store.posts()}
    generated = set(store.state().get("generated_slots", []))
    out = []
    from .pipeline import slot_base
    for s in cfg["slots"]:
        base = slot_base(s, now)
        if not _slot_applies(s, base):
            continue
        # 投稿時刻は枠の時刻の最大約30分後までばらつくので、それを過ぎてから判定する
        if now < base + timedelta(minutes=40):
            continue
        key = f"{base:%Y%m%d}-{s['id']}"
        if key in posted:
            continue
        out.append({"slot": s["id"], "key": key, "generated": key in generated,
                    "within_grace": now <= base + timedelta(minutes=cfg["slot_grace_minutes"] - 15)})
    return out


def _gh(path: str, method: str = "GET", **kw) -> requests.Response:
    token = env("GITHUB_TOKEN", required=False)
    repo = env("GITHUB_REPOSITORY", required=False)
    if not token or not repo:
        raise RuntimeError("GitHubのトークンがありません")
    return requests.request(method, f"{GH_API}/repos/{repo}{path}", timeout=30,
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, **kw)


def failed_runs(hours: int = 6) -> list[dict]:
    try:
        r = _gh("/actions/runs", params={"status": "failure", "per_page": 20})
        r.raise_for_status()
    except (requests.RequestException, RuntimeError) as e:
        log.warning("ワークフローの失敗一覧を取得できません: %s", e)
        return []
    since = now_utc() - timedelta(hours=hours)
    return [{"name": w["name"], "event": w["event"], "at": w["created_at"], "id": w["id"]}
            for w in r.json().get("workflow_runs", [])
            if datetime.fromisoformat(w["created_at"].replace("Z", "+00:00")) >= since]


def worker_checks() -> list[dict]:
    """Cloudflare Workerの見張りが実際に動いているか（設定ミスで一度も動かない、を早く見つける）。"""
    out: list[dict] = []
    base = settings().get("worker_url", "").rstrip("/")
    try:
        r = requests.get(f"{base}/watch-debug", timeout=60)
        dbg = r.json()
        broken = {k: v.get("error", "") for k, v in dbg.items() if not v.get("ok")}
        if broken:
            out.append({"type": "worker_watch_broken", "parts": broken})
    except (requests.RequestException, ValueError) as e:
        out.append({"type": "worker_unreachable", "error": str(e)[:200]})
    # 速報の見張りは、関連ニュースが出るたびに起動する。12時間まったく起動していなければ止まっている疑い
    try:
        r = _gh("/actions/workflows/news-watch.yml/runs", params={"event": "repository_dispatch", "per_page": 1})
        runs = r.json().get("workflow_runs", []) if r.ok else []
        last = runs[0]["created_at"] if runs else None
        if not last or now_utc() - datetime.fromisoformat(last.replace("Z", "+00:00")) > timedelta(hours=12):
            out.append({"type": "news_watch_silent", "last": last})
    except (requests.RequestException, RuntimeError, ValueError) as e:
        log.warning("速報の見張りの起動を確認できません: %s", e)
    return out


def retry_slot() -> bool:
    """投稿漏れの枠がまだ間に合うなら、投稿をもう一度起動する（枠の判定は起動先で行う）。"""
    try:
        r = _gh("/dispatches", method="POST", json={"event_type": "generate", "client_payload": {"by": "health"}})
        return r.status_code < 300
    except (requests.RequestException, RuntimeError) as e:
        log.warning("投稿の再起動に失敗: %s", e)
        return False


def run() -> dict:
    now = now_jst()
    issues: list[dict] = []
    actions: list[str] = []

    for m in missed_slots(now):
        issues.append({"type": "missed_slot", **m})
        # 生成済みなのに投稿がない枠は、原稿が全部弾かれたなど。再起動しても同じなので点検に回す
        if m["within_grace"] and not m["generated"] and retry_slot():
            actions.append(f"枠{m['slot']}の投稿を再起動")

    if issues:
        # 投稿漏れがあるとき、Cloudflareからの起動が来ていたかも確かめる（来ていなければWorker側の問題）
        try:
            r = _gh("/actions/workflows/generate.yml/runs", params={"event": "repository_dispatch", "per_page": 1})
            runs = r.json().get("workflow_runs", []) if r.ok else []
            last = runs[0]["created_at"] if runs else None
            if not last or now_utc() - datetime.fromisoformat(last.replace("Z", "+00:00")) > timedelta(hours=14):
                issues.append({"type": "cloudflare_not_dispatching", "last_dispatch": last})
        except (requests.RequestException, RuntimeError, ValueError) as e:
            log.warning("Cloudflareからの起動を確認できません: %s", e)

    issues += worker_checks()

    st = store.state()
    if st.get("halted"):
        issues.append({"type": "halted", "reason": st.get("halt_reason", "")})

    err = store.read_json("last_error.json")
    if err and now_utc() - datetime.fromisoformat(err["at"]) < timedelta(hours=6):
        issues.append({"type": "error", "command": err["command"], "error": err["error"][:300], "at": err["at"]})

    for w in failed_runs():
        issues.append({"type": "workflow_failed", **w})

    rejected = [r for r in store.read_jsonl("rejected_variants.jsonl")
                if now_utc() - datetime.fromisoformat(r["at"]) < timedelta(hours=12)]
    if len(rejected) >= 6:
        issues.append({"type": "many_rejections", "n": len(rejected),
                       "reasons": sorted({e for r in rejected for e in r["errors"]})[:8]})

    rec = {"at": now_utc().isoformat(), "at_jst": now.strftime("%m/%d %H:%M"), "ok": not issues,
           "issues": issues, "actions": actions}
    store.write_json("health.json", rec)
    if issues:
        store.append_jsonl("health_log.jsonl", rec)
    log.info("見回り: 問題 %d件 / 対応 %s", len(issues), actions)
    return rec

