"""サービス画面の「今すぐ更新」。画面は依頼を置くだけで、調査は jobs コンテナが行う。

依頼は requests/ の1ファイルで表す。
- research.req     依頼を受けた（待機中）
- research.running jobs が実行中
画面のコンテナが書き込めるのはこのフォルダだけ。発注やキルスイッチには触れない。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from . import config as C

MIN_INTERVAL = timedelta(minutes=10)


def _dir():
    return C.REQUESTS


def _pending():
    return _dir() / "research.req"


def _running():
    return _dir() / "research.running"


def _read(path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def state() -> dict:
    pending, running = _read(_pending()), _read(_running())
    return {
        "pending": pending is not None,
        "running": running is not None,
        "requested_utc": (running or pending or {}).get("requested_utc"),
        "started_utc": (running or {}).get("started_utc"),
    }


def request(last_research_utc: str | None, now: datetime | None = None) -> tuple[int, dict]:
    """依頼を受け付ける。(HTTPステータス, 応答) を返す。"""
    now = now or datetime.now(timezone.utc)
    s = state()
    if s["pending"] or s["running"]:
        return 409, {"status": "already_requested", **s}
    if last_research_utc and now - datetime.fromisoformat(last_research_utc) < MIN_INTERVAL:
        wait = MIN_INTERVAL - (now - datetime.fromisoformat(last_research_utc))
        return 429, {"status": "too_soon", "retry_after_seconds": int(wait.total_seconds()) + 1}
    _dir().mkdir(parents=True, exist_ok=True)
    tmp = _dir() / f"research.req.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({"requested_utc": now.isoformat(timespec="seconds")}), encoding="utf-8")
    os.replace(tmp, _pending())
    return 202, {"status": "requested", **state()}


def take(now: datetime | None = None) -> dict | None:
    """jobs が依頼を引き取る。依頼がなければ None。"""
    now = now or datetime.now(timezone.utc)
    req = _read(_pending())
    if req is None:
        return None
    req["started_utc"] = now.isoformat(timespec="seconds")
    _running().write_text(json.dumps(req), encoding="utf-8")
    _pending().unlink(missing_ok=True)
    return req


def finish() -> None:
    _running().unlink(missing_ok=True)


def clear_stale(now: datetime | None = None, limit: timedelta = timedelta(hours=1)) -> None:
    """jobs が途中で止まって残った「実行中」を片付ける。"""
    now = now or datetime.now(timezone.utc)
    run = _read(_running())
    if run and now - datetime.fromisoformat(run["started_utc"]) > limit:
        finish()
