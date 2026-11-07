"""コマンド入口: python -m hedgedesk <research|brief-data|tick|loop|weekly|scout|status|ui|reset-kill>"""
import json
import sys

from . import config as C


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.stdout.reconfigure(encoding="utf-8")
    if cmd == "research":
        from .research import run_research

        rep = run_research(refresh="--refresh" in sys.argv)
        print(json.dumps({
            "trials": rep["n_trials_total"], "passed": rep["passed_ids"],
            "ranked": [{"id": r["id"], "score": r["score"]["total"], "oos": r["out_of_sample"], "dsr": r["deflated_sharpe"], "failures": r["failures"]} for r in rep["ranked"]],
            "untested": [{"id": r["id"], "failures": r["failures"]} for r in rep["untested"]],
        }, ensure_ascii=False, indent=1, default=str))
    elif cmd == "brief-data":
        from .desk import brief_data

        brief_data()
        print(C.STATE / "brief_data.json")
    elif cmd == "tick":
        from .desk import tick

        print(json.dumps(tick(), ensure_ascii=False, default=str))
    elif cmd == "loop":
        from .desk import run_loop

        run_loop()
    elif cmd == "weekly":
        from .desk import weekly_review

        print(json.dumps(weekly_review(), ensure_ascii=False, indent=1, default=str))
    elif cmd == "scout":
        from .scout import collect

        print(json.dumps(collect(), ensure_ascii=False))
    elif cmd == "ui":
        from .ui import HOST, PORT, serve

        print(f"http://{HOST}:{PORT}", flush=True)
        serve()
    elif cmd == "reset-kill":
        from . import journal
        from .risk import KILL_FILE

        if KILL_FILE.exists():
            KILL_FILE.unlink()
        journal.log("kill_reset", "Owner", note="オーナーが解除")
        print("kill switch reset")
    elif cmd == "status":
        from . import analyst, risk, trader

        print(json.dumps({
            "champion": analyst.load_champion(), "kill_switch": risk.kill_status(),
            "alpaca_keys": trader.AlpacaPaper().available, "open_book": trader._load_positions(),
        }, ensure_ascii=False, indent=1))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
