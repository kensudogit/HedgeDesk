"""Windowsのタスクスケジューラから5分ごとに呼ばれる入口。画面は出さない。"""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from hedgedesk.desk import tick

    tick()
except Exception:
    (ROOT / "state").mkdir(exist_ok=True)
    with open(ROOT / "state" / "tick_error.log", "a", encoding="utf-8") as f:
        f.write(traceback.format_exc() + "\n")
