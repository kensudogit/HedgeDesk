"""jobsコンテナ: 夜間リサーチ、モーニングブリーフ、週次レビューを時刻どおりに動かす。

- 時刻はJSTで判定する。PCがスリープしていた場合は、起きたときに遅れて1回だけ実行する。
- 調査 → ブリーフ → 週次の順に動かすので、遅れたときもブリーフは新しい調査結果を使う。
- 長い調査で監視が止まらないよう、5分ごとの処理（tick）とは別のプロセスで動かす。
"""
from __future__ import annotations

import json
import sys
import time as _time
import traceback
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from . import config as C
from . import journal, refresh

JST = ZoneInfo("Asia/Tokyo")
RESEARCH_AT = time(21, 0)
BRIEF_AT = time(7, 15)
WEEKLY_AT = time(10, 0)
SUNDAY = 6


def _state_path():
    return C.STATE / "jobs.json"


def _daily_key(jst: datetime, at: time) -> str:
    """直近で予定時刻を過ぎた日。この値が変わったら実行する。"""
    day: date = jst.date() if jst.time() >= at else jst.date() - timedelta(days=1)
    return day.isoformat()


def _weekly_key(jst: datetime) -> str:
    days_since = (jst.weekday() - SUNDAY) % 7
    sunday = jst.date() - timedelta(days=days_since)
    if days_since == 0 and jst.time() < WEEKLY_AT:
        sunday -= timedelta(days=7)
    return sunday.isoformat()


def keys(now: datetime) -> dict[str, str]:
    jst = now.astimezone(JST)
    return {"research": _daily_key(jst, RESEARCH_AT), "brief": _daily_key(jst, BRIEF_AT), "weekly": _weekly_key(jst)}


def _run_research() -> str:
    from .research import run_research

    rep = run_research(refresh=True)
    return f"合格{len(rep['passed_ids'])}本、全{rep['n_trials_total']}試行"


def _run_brief() -> str:
    from .brief import write_brief

    return write_brief()


def _run_weekly() -> str:
    from .brief import write_weekly

    return write_weekly()


DEFAULT_RUNNERS: dict[str, Callable[[], str]] = {"research": _run_research, "brief": _run_brief, "weekly": _run_weekly}


def run_due(now: datetime | None = None, runners: dict[str, Callable[[], str]] | None = None) -> dict:
    """予定を過ぎたジョブを実行する。初回は基準を記録するだけで、次の予定時刻から動かす。"""
    now = now or datetime.now(timezone.utc)
    runners = runners or DEFAULT_RUNNERS
    ran = _run_manual_refresh(now, runners)
    current = keys(now)
    path = _state_path()
    C.STATE.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(current), encoding="utf-8")
        return {**ran, "seeded": current}
    state = json.loads(path.read_text(encoding="utf-8"))
    for name in ("research", "brief", "weekly"):
        if state.get(name) == current[name]:
            continue
        # 先に記録する。失敗しても次の予定まで繰り返さず、エラーはブリーフで報告する。
        state[name] = current[name]
        path.write_text(json.dumps(state), encoding="utf-8")
        try:
            ran[name] = runners[name]()
            journal.log("job_done", "Chief of Staff", job=name, result=ran[name])
        except Exception:
            ran[name] = "error"
            journal.log("job_error", "Chief of Staff", job=name, error=traceback.format_exc()[-800:])
    return ran


def _run_manual_refresh(now: datetime, runners: dict[str, Callable[[], str]]) -> dict:
    """サービス画面の「今すぐ更新」で置かれた依頼を処理する。定時の予定には影響しない。"""
    refresh.clear_stale(now)
    req = refresh.take(now)
    if req is None:
        return {}
    try:
        result = runners["research"]()
        journal.log("job_done", "Chief of Staff", job="manual_research", result=result, requested_utc=req["requested_utc"])
        return {"manual_research": result}
    except Exception:
        journal.log("job_error", "Chief of Staff", job="manual_research", error=traceback.format_exc()[-800:])
        return {"manual_research": "error"}
    finally:
        refresh.finish()


def run_loop(interval_seconds: int = 60, max_iterations: int | None = None, sleep=_time.sleep) -> None:
    n = 0
    while max_iterations is None or n < max_iterations:
        try:
            ran = run_due()
            if ran:
                print(json.dumps(ran, ensure_ascii=False, default=str), flush=True)
        except Exception:
            print(traceback.format_exc(), file=sys.stderr, flush=True)
        n += 1
        if max_iterations is None or n < max_iterations:
            sleep(interval_seconds)
