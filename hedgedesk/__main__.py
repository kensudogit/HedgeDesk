"""コマンド入口: python -m hedgedesk <research|forward|brief|brief-data|tick|loop|jobs|weekly|scout|news|status|check-alpaca|ui|reset-kill>"""
#
# 【役割】`python -m hedgedesk <サブコマンド>` の入口。引数を見て各モジュールの関数を呼び分ける。
# 【位置付け】Docker Compose の各サービス（ui / tick=loop / jobs）と手動操作の両方がここを通る。
# 【主な依存先】config。各サブコマンドのモジュールは、そのコマンドが選ばれたときだけ関数内で import する
#   （ui コンテナで pandas / yfinance を読み込まずに済ませ、起動を速く、依存を最小にするため）。
# 【注意】上のモジュール docstring は、不明なコマンドのときに使い方として print(__doc__) で表示される。
import json
import sys

from . import config as C


# 引数: なし（sys.argv を直接読む。第1引数がサブコマンド、"--refresh" や "--stdout" はフラグ）。
# 戻り値: プロセスの終了コード。成功は0、不明なサブコマンドは2。
# 副作用: サブコマンドに応じて、調査の実行、state/ や journal/ への書き込み、HTTP サーバーの起動などを行う。
# 例外: 各処理の例外はここでは捕まえず、そのまま呼び出し元（プロセス）に伝える。
def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    # Windows のコンソール（cp932）でも日本語の JSON を文字化けや UnicodeEncodeError なしで出すため。
    sys.stdout.reconfigure(encoding="utf-8")
    if cmd == "research":
        # 夜間リサーチを手動で実行し、要点だけを JSON で表示する（全文は state/latest_research.json）。
        from .research import run_research

        rep = run_research(refresh="--refresh" in sys.argv)
        print(json.dumps({
            "trials": rep["n_trials_total"], "passed": rep["passed_ids"],
            "ranked": [{"id": r["id"], "score": r["score"]["total"], "oos": r["out_of_sample"], "dsr": r["deflated_sharpe"], "failures": r["failures"]} for r in rep["ranked"]],
            "untested": [{"id": r["id"], "failures": r["failures"]} for r in rep["untested"]],
        }, ensure_ascii=False, indent=1, default=str))
    elif cmd == "forward":
        # フォワード記録だけを作り直す（最新の調査結果とキャッシュ済みのデータを使う。データは取り直さない）。
        from . import forward
        from .research import LATEST, build_context

        rep = json.loads(LATEST.read_text(encoding="utf-8"))
        ctx, _ = build_context(refresh=False)
        out = forward.update(rep.get("ranked", []), ctx)
        print(json.dumps([{k: r[k] for k in ("strategy", "start", "trades", "mean_r", "range90", "consistent", "signals_today")} for r in out["rows"]], ensure_ascii=False, indent=1))
    elif cmd == "brief-data":
        # ブリーフの材料だけを作って state/brief_data.json に保存し、そのパスを表示する。
        from .desk import brief_data

        brief_data()
        print(C.STATE / "brief_data.json")
    elif cmd == "tick":
        # 5分ごとの処理を1回だけ実行する（Windows タスクやデバッグ用）。
        from .desk import tick

        print(json.dumps(tick(), ensure_ascii=False, default=str))
    elif cmd == "loop":
        # tick コンテナ用: tick を5分間隔で繰り返す常駐ループ。
        from .desk import run_loop

        run_loop()
    elif cmd == "jobs":
        # jobs コンテナ用: 夜間リサーチ・ブリーフ・週次レビューを時刻どおりに動かす常駐ループ。
        from .scheduler import run_loop as jobs_loop

        jobs_loop()
    elif cmd == "brief":
        # --stdout なら保存せずに本文を表示し、なければ briefs/ に保存してパスを表示する。
        from .brief import render, write_brief
        from .desk import brief_data

        print(render(brief_data()) if "--stdout" in sys.argv else write_brief())
    elif cmd == "weekly":
        # 週次レビュー（ペーパーとバックテストの整合）を実行して結果を表示する。
        from .desk import weekly_review

        print(json.dumps(weekly_review(), ensure_ascii=False, indent=1, default=str))
    elif cmd == "scout":
        # ニュース見出しの収集を1回実行する。
        from .scout import collect

        print(json.dumps(collect(), ensure_ascii=False))
    elif cmd == "news":
        # ニュース分析を1回実行する。--no-ai なら AI を呼ばずに一覧（鮮度・価格反応）だけを作り直す。
        from .news import run as news_run

        print(json.dumps(news_run(use_ai="--no-ai" not in sys.argv), ensure_ascii=False, default=str))
    elif cmd == "ui":
        # 読み取り専用のサービス画面を起動する。serve() は戻らない（Ctrl+C かコンテナ停止まで動く）。
        from .ui import HOST, PORT, serve

        print(f"http://{HOST}:{PORT}", flush=True)
        serve()
    elif cmd == "reset-kill":
        # キルスイッチの解除。DESK_RULES 上、解除できるのはオーナーだけなので、画面や自動処理からは呼ばれない。
        # 解除の事実はジャーナルに残す（誰がいつ解除したかを後から確認できるように）。
        from . import journal
        from .risk import KILL_FILE

        if KILL_FILE.exists():
            KILL_FILE.unlink()
        journal.log("kill_reset", "Owner", note="オーナーが解除")
        print("kill switch reset")
    elif cmd == "check-alpaca":
        # ペーパー口座のキーを読み取りだけで確かめる（GET /v2/account）。発注はしない。キーの値と口座番号は表示しない。
        from . import trader

        broker = trader.AlpacaPaper()
        if not broker.available:
            print(json.dumps({"keys": False, "ok": False, "hint": ".env に APCA_API_KEY_ID と APCA_API_SECRET_KEY を書き、tick を作り直してください"}, ensure_ascii=False))
            return 1
        try:
            acct = broker.account()
        except Exception as ex:
            print(json.dumps({"keys": True, "ok": False, "error": type(ex).__name__}, ensure_ascii=False))
            return 1
        print(json.dumps({"keys": True, "ok": True, "endpoint": broker.BASE, "status": acct.get("status"),
                          "currency": acct.get("currency"), "equity": acct.get("equity"), "trading_blocked": acct.get("trading_blocked")}, ensure_ascii=False))
    elif cmd == "status":
        # 現在の状態の要約。alpaca_keys はキーの有無（真偽値）だけで、キーの値は表示しない。
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
