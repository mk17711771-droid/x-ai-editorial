"""コマンドライン入口。GitHub Actionsから呼ばれる。

  python -m aiedit.cli generate [--slot auto|0730|...] [--force]
  python -m aiedit.cli decide --id <下書きID> --action approve|reject_quality|reject_inaccurate
  python -m aiedit.cli resume
  python -m aiedit.cli notify --message "..."
  python -m aiedit.cli research                 # 市場の定点観測と、自分の投稿の反応の計測
  python -m aiedit.cli learn [--weekly-report]  # 成果を分析して運用ノートを更新
  python -m aiedit.cli health                  # 見回り（投稿漏れ・停止・失敗の検出と、直せるものの対応）
  python -m aiedit.cli note-check              # 公開されたnote記事の確認（間違いがあればLINE）
  python -m aiedit.cli sources [--daily] [--weekly]  # 外部リサーチ（ニュース・note・X）
  python -m aiedit.cli improve                 # 改善会議（日曜は週次、月の最初の日曜は月次）
  python -m aiedit.cli preview --kind stock|hl   # 材料収集だけ試す（投稿しない）
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback

from . import health, improve, learn, line, note, note_check, pipeline, research, sources, store
from .common import JST, now_utc, setup_logging


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    ap = argparse.ArgumentParser(prog="aiedit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--slot", default="auto")
    g.add_argument("--force", action="store_true")
    d = sub.add_parser("decide")
    d.add_argument("--id", required=True)
    d.add_argument("--action", required=True, choices=["approve", "reject_quality", "reject_inaccurate", "delete"])
    sub.add_parser("resume")
    n = sub.add_parser("notify")
    n.add_argument("--message", required=True)
    sub.add_parser("research")
    lp = sub.add_parser("learn")
    lp.add_argument("--weekly-report", action="store_true")
    np_ = sub.add_parser("note-profile")
    np_.add_argument("--urlname", default="charisma_hl")
    na = sub.add_parser("note-audit")
    na.add_argument("--urlname", default="charisma_hl")
    nart = sub.add_parser("note-article")
    nart.add_argument("--theme", default=None)
    sub.add_parser("note-check")
    sub.add_parser("health")
    sub.add_parser("metrics-now")
    sub.add_parser("news-watch")
    xc = sub.add_parser("x-check")
    xc.add_argument("--users", default="")
    xc.add_argument("--query", default="")
    sp = sub.add_parser("sources")
    sp.add_argument("--daily", action="store_true")
    sp.add_argument("--weekly", action="store_true")
    sp.add_argument("--news-only", action="store_true")
    ip = sub.add_parser("improve")
    ip.add_argument("--kind", choices=["auto", "weekly", "monthly"], default="auto")
    p = sub.add_parser("preview")
    p.add_argument("--kind", choices=["stock", "hl"], default="hl")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "generate":
            drafts = pipeline.generate(args.slot, args.force)
            print(f"下書き {len(drafts)} 件")
        elif args.cmd == "decide":
            print(pipeline.publish(args.id, args.action))
        elif args.cmd == "resume":
            store.resume()
            line.notify("自動運用を再開しました。")
        elif args.cmd == "notify":
            line.notify(args.message)
        elif args.cmd == "research":
            research.watch()
            print(f"反応を計測: {research.measure()}件")
        elif args.cmd == "learn":
            weekly = args.weekly_report or now_utc().astimezone(JST).weekday() == 6
            learn.run(send_weekly_report=weekly)
        elif args.cmd == "note-profile":
            print(json.dumps(note.fetch_profile(args.urlname), ensure_ascii=False, indent=2))
        elif args.cmd == "note-audit":
            print(f"{len(note.audit_notes(args.urlname))}件を確認")
        elif args.cmd == "note-article":
            from . import note_article
            meta = note_article.write(args.theme)
            print(json.dumps(meta, ensure_ascii=False, indent=2) if meta else "記事を作れませんでした")
        elif args.cmd == "x-check":
            # ニュースの裏取り用: アカウントの公開情報と、検索結果を data/x_check.json に残す
            from . import xapi
            out = {"at": now_utc().isoformat(), "users": [], "posts": []}
            if args.users:
                out["users"] = xapi.users([u.strip().lstrip("@") for u in args.users.split(",") if u.strip()])
            if args.query:
                tweets, us = xapi.search_recent(args.query, 10)
                out["posts"] = [{"author": us.get(t.get("author_id"), {}).get("username"), "created_at": t.get("created_at"),
                                 "text": t.get("text"), "metrics": t.get("public_metrics")} for t in tweets]
            store.write_json("x_check.json", out)
            print(json.dumps(out, ensure_ascii=False, indent=2))
        elif args.cmd == "news-watch":
            from . import news_watch
            import os
            payload = {}
            event_path = os.environ.get("GITHUB_EVENT_PATH")
            if event_path and os.path.exists(event_path):
                payload = json.load(open(event_path, encoding="utf-8")).get("client_payload") or {}
            print(json.dumps(news_watch.run(payload), ensure_ascii=False, indent=2))
        elif args.cmd == "metrics-now":
            print(json.dumps(research.snapshot(), ensure_ascii=False, indent=2))
        elif args.cmd == "health":
            print(json.dumps(health.run(), ensure_ascii=False, indent=2))
        elif args.cmd == "note-check":
            res = note_check.run()
            print(json.dumps(res, ensure_ascii=False, indent=2) if res else "新しく公開された記事はありません")
            print("リマインド:", note_check.remind_unpublished())
        elif args.cmd == "sources":
            print(sources.run(daily=args.daily, weekly=args.weekly, news_only=args.news_only))
        elif args.cmd == "improve":
            today = now_utc().astimezone(JST)
            if args.kind == "auto" and today.weekday() != 6:
                print("改善会議は日曜に行います")
            else:
                monthly = args.kind == "monthly" or (args.kind == "auto" and today.day <= 7)
                rec = improve.run(monthly=monthly)
                print(json.dumps({k: rec.get(k) for k in ("kind", "decisions", "actions")}, ensure_ascii=False, indent=2))
        elif args.cmd == "preview":
            for f in pipeline.collect(args.kind):
                print(json.dumps({"id": f["id"], "category": f["category"], "summary": f["summary"]}, ensure_ascii=False))
    except pipeline.Halted as e:
        print(f"停止中のため処理しません: {e}")
        if args.cmd == "decide":
            line.notify(f"自動運用が停止中のため投稿しませんでした。\n{e}")
    except Exception as e:
        # 原因を data/last_error.json に残し（コミットされる）、LINEにも要点を送る
        store.write_json("last_error.json", {
            "at": now_utc().isoformat(),
            "command": args.cmd,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-4000:],
        })
        if args.cmd in ("generate", "decide", "note-article", "news-watch"):
            # リサーチ・学習の失敗は次の回で取り戻せるので、LINEの通数を使わない
            line.notify(f"【異常】{args.cmd} でエラーが発生しました\n{type(e).__name__}: {str(e)[:600]}")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
