"""コマンドライン入口。GitHub Actionsから呼ばれる。

  python -m aiedit.cli generate [--slot auto|0730|...] [--force]
  python -m aiedit.cli decide --id <下書きID> --action approve|reject_quality|reject_inaccurate
  python -m aiedit.cli resume
  python -m aiedit.cli notify --message "..."
  python -m aiedit.cli preview --kind stock|hl   # 材料収集だけ試す（投稿しない）
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback

from . import line, pipeline, store
from .common import now_utc, setup_logging


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
        line.notify(f"【異常】{args.cmd} でエラーが発生しました\n{type(e).__name__}: {str(e)[:600]}")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
