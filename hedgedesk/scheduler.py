"""jobsコンテナ: 夜間リサーチ、モーニングブリーフ、週次レビューを時刻どおりに動かす。

- 時刻はJSTで判定する。PCがスリープしていた場合は、起きたときに遅れて1回だけ実行する。
- 調査 → ブリーフ → 週次の順に動かすので、遅れたときもブリーフは新しい調査結果を使う。
- 長い調査で監視が止まらないよう、5分ごとの処理（tick）とは別のプロセスで動かす。
"""
#
# 【位置付け】jobs コンテナで `python -m hedgedesk jobs` → run_loop が1分ごとに run_due を呼ぶ。
#   画面の「今すぐ更新」の依頼（refresh モジュール）もここで処理する。
# 【主な依存先】research.run_research、brief.write_brief / write_weekly（いずれも関数内で import）、refresh、journal、config。
# 【実行の判定】各ジョブについて「直近で予定時刻を過ぎた日（キー）」を計算し、state/jobs.json の記録と違えば実行する。
#   これにより、時刻ちょうどに起きていなくても、次に起きたときに1回だけ実行される。
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

# 予定時刻（JST）。調査は米国の前日の確定足がそろった後の21時、ブリーフは朝7:15、週次レビューは日曜10時。
# SUNDAY は datetime.weekday() の日曜（月曜=0 … 日曜=6）。
JST = ZoneInfo("Asia/Tokyo")
RESEARCH_AT = time(21, 0)
BRIEF_AT = time(7, 15)
WEEKLY_AT = time(10, 0)
SUNDAY = 6


def _state_path():
    """ジョブの最終実行キーを記録するファイル。C.STATE を毎回読むのは、テストの差し替えを反映させるため。"""
    return C.STATE / "jobs.json"


def _daily_key(jst: datetime, at: time) -> str:
    """直近で予定時刻を過ぎた日。この値が変わったら実行する。"""
    # 引数: jst は JST の現在時刻、at は予定時刻。
    # 戻り値: 今日の予定時刻を過ぎていれば今日、まだなら昨日の日付（ISO 文字列）。
    day: date = jst.date() if jst.time() >= at else jst.date() - timedelta(days=1)
    return day.isoformat()


def _weekly_key(jst: datetime) -> str:
    """直近で「日曜 WEEKLY_AT」を過ぎた日曜の日付を返す。この値が変わったら週次レビューを実行する。

    日曜でもまだ予定時刻前なら、1週間前の日曜を返す。月曜に起きた場合も前日の日曜がキーになるので、遅れて1回実行される。
    """
    days_since = (jst.weekday() - SUNDAY) % 7
    sunday = jst.date() - timedelta(days=days_since)
    if days_since == 0 and jst.time() < WEEKLY_AT:
        sunday -= timedelta(days=7)
    return sunday.isoformat()


def keys(now: datetime) -> dict[str, str]:
    """research / brief / weekly の各ジョブの現在のキーを返す。now は UTC でもよい（JST に変換して判定する）。"""
    jst = now.astimezone(JST)
    return {"research": _daily_key(jst, RESEARCH_AT), "brief": _daily_key(jst, BRIEF_AT), "weekly": _weekly_key(jst)}


def _run_research() -> str:
    """夜間リサーチを実行し、ジャーナルに残す短い結果文を返す。"""
    from .research import run_research

    rep = run_research(refresh=True)
    return f"合格{len(rep['passed_ids'])}本、全{rep['n_trials_total']}試行"


def _run_brief() -> str:
    """モーニングブリーフを書き、保存先のパスを返す。"""
    from .brief import write_brief

    return write_brief()


def _run_weekly() -> str:
    """週次メモを書き、保存先のパスを返す。"""
    from .brief import write_weekly

    return write_weekly()


# ジョブ名 → 実行関数。テストでは run_due に記録用の関数を渡して差し替える。
DEFAULT_RUNNERS: dict[str, Callable[[], str]] = {"research": _run_research, "brief": _run_brief, "weekly": _run_weekly}


def run_due(now: datetime | None = None, runners: dict[str, Callable[[], str]] | None = None) -> dict:
    """予定を過ぎたジョブを実行する。初回は基準を記録するだけで、次の予定時刻から動かす。"""
    # 引数: now は判定時刻、runners はジョブ名 → 実行関数（省略時は DEFAULT_RUNNERS）。
    # 戻り値: 実行したジョブの結果（エラーなら "error"）。初回は {"seeded": キー}。「今すぐ更新」を処理したら manual_research も入る。
    # 副作用: state/jobs.json を更新し、ジャーナルに job_done / job_error を記録する。
    # 初回に何も実行しないのは、コンテナを初めて起動した瞬間に、過ぎた予定の重い調査をまとめて走らせないため。
    # 順番は research → brief → weekly 固定。スリープ明けにまとめて実行されても、ブリーフは新しい調査結果を使える。
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
    # 引数: now は処理時刻、runners の "research" を使う。
    # 戻り値: 依頼がなければ空の dict、処理したら {"manual_research": 結果 または "error"}。
    # 例外は記録して握りつぶし、finally で必ず実行中の目印を消す（画面のボタンが押せないままにならないように）。
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
    """jobs コンテナの常駐ループ。interval_seconds ごとに run_due を呼ぶ。

    Args:
        interval_seconds: 確認の間隔（秒）。予定時刻の判定はキー方式なので、1分おきの確認で十分。
        max_iterations: テスト用。None なら無限に回る。
        sleep: 待ち関数。テストで待たずに回すために差し替える。

    1回の失敗でループを止めないよう、例外は標準エラーに出して次の回へ進む。最後の回の後は待たない。
    """
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
