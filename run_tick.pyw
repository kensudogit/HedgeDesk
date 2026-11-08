"""Windowsのタスクスケジューラから5分ごとに呼ばれる入口。画面は出さない。"""
#
# 【役割】Docker を使わずに Windows で運用する場合の tick の入口（Docker の tick コンテナと同じ desk.tick を1回呼ぶ）。
# 【なぜ .pyw か】pythonw.exe で実行され、5分ごとにコンソール画面が開かないようにするため。
#   その代わり標準出力・標準エラーが見えないので、例外は state/tick_error.log に書き出す。
# 【主な依存先】hedgedesk.desk.tick。tick のロックにより、コンテナと同時に動いても二重実行はされない。
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# タスクスケジューラは作業ディレクトリを保証しないので、このファイルの場所を import の探索先に加えて hedgedesk を読めるようにする。

try:
    from hedgedesk.desk import tick

    tick()
except Exception:
    # import の失敗も含めて記録する（画面がないので、ここに残さないと失敗に気づけない）。
    (ROOT / "state").mkdir(exist_ok=True)
    with open(ROOT / "state" / "tick_error.log", "a", encoding="utf-8") as f:
        f.write(traceback.format_exc() + "\n")
