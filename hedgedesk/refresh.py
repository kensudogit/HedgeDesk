"""サービス画面の「今すぐ更新」。画面は依頼を置くだけで、調査は jobs コンテナが行う。

依頼は requests/ の1ファイルで表す。
- research.req     依頼を受けた（待機中）
- research.running jobs が実行中
画面のコンテナが書き込めるのはこのフォルダだけ。発注やキルスイッチには触れない。
"""
#
# 【位置付け】ui.Handler.do_POST が request() を呼び、scheduler._run_manual_refresh が take() / finish() /
#   clear_stale() を呼ぶ。ui と jobs は別コンテナなので、共有ボリューム上のファイルだけで受け渡す。
# 【状態遷移】(なし) → request() → research.req → take() → research.running → finish() → (なし)
# 【主な依存先】config.REQUESTS（テストでは monkeypatch で差し替える）。
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from . import config as C

# 前回の調査からこの時間が経つまでは新しい依頼を受けない（連打で重い調査が続かないように）。
MIN_INTERVAL = timedelta(minutes=10)


def _dir():
    """依頼ファイルを置くディレクトリ。C.REQUESTS を毎回読むのは、テストの差し替えを反映させるため。"""
    return C.REQUESTS


def _pending():
    """待機中の依頼ファイルのパス。"""
    return _dir() / "research.req"


def _running():
    """実行中の目印ファイルのパス。"""
    return _dir() / "research.running"


def _read(path) -> dict | None:
    """JSON ファイルを読む。存在しない・読めない・壊れている場合は None（＝その状態ではない）とみなす。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def state() -> dict:
    """依頼の状態を返す（画面の「今すぐ更新」ボタンの表示に使う）。

    Returns:
        pending（待機中か）、running（実行中か）、requested_utc（依頼時刻）、started_utc（開始時刻）。
        実行中の情報があればそちらを優先して時刻を返す。
    """
    pending, running = _read(_pending()), _read(_running())
    return {
        "pending": pending is not None,
        "running": running is not None,
        "requested_utc": (running or pending or {}).get("requested_utc"),
        "started_utc": (running or {}).get("started_utc"),
    }


def request(last_research_utc: str | None, now: datetime | None = None) -> tuple[int, dict]:
    """依頼を受け付ける。(HTTPステータス, 応答) を返す。"""
    # 引数: last_research_utc は直近の調査の時刻（ISO 形式、タイムゾーン付き）。None なら間隔の確認をしない。
    # 戻り値: 202（受付）、409（すでに待機中か実行中）、429（前回から10分以内。retry_after_seconds 付き）。
    # 副作用: 受け付けたときだけ requests/research.req を作る。
    now = now or datetime.now(timezone.utc)
    s = state()
    if s["pending"] or s["running"]:
        return 409, {"status": "already_requested", **s}
    if last_research_utc and now - datetime.fromisoformat(last_research_utc) < MIN_INTERVAL:
        wait = MIN_INTERVAL - (now - datetime.fromisoformat(last_research_utc))
        return 429, {"status": "too_soon", "retry_after_seconds": int(wait.total_seconds()) + 1}
    _dir().mkdir(parents=True, exist_ok=True)
    tmp = _dir() / f"research.req.{os.getpid()}.tmp"
    # 一時ファイルに書いてから os.replace で置き換える。jobs 側が書きかけの JSON を読むことを防ぐため（置き換えは原子的）。
    tmp.write_text(json.dumps({"requested_utc": now.isoformat(timespec="seconds")}), encoding="utf-8")
    os.replace(tmp, _pending())
    return 202, {"status": "requested", **state()}


def take(now: datetime | None = None) -> dict | None:
    """jobs が依頼を引き取る。依頼がなければ None。"""
    # 引数: now は開始時刻として記録する時刻。
    # 戻り値: 依頼の内容に started_utc を加えた dict。
    # 副作用: research.running を書いてから research.req を消す。この順なので、途中で止まっても
    #   「待機中も実行中もない」状態にはならず、画面から二重に依頼されることがない。
    now = now or datetime.now(timezone.utc)
    req = _read(_pending())
    if req is None:
        return None
    req["started_utc"] = now.isoformat(timespec="seconds")
    _running().write_text(json.dumps(req), encoding="utf-8")
    _pending().unlink(missing_ok=True)
    return req


def finish() -> None:
    """実行中の目印を消す（調査の成功・失敗にかかわらず呼ぶ）。"""
    _running().unlink(missing_ok=True)


def clear_stale(now: datetime | None = None, limit: timedelta = timedelta(hours=1)) -> None:
    """jobs が途中で止まって残った「実行中」を片付ける。"""
    # 引数: limit を超えて実行中のままなら、プロセスが落ちたとみなして目印を消す（調査は通常数分で終わる）。
    # 残したままだと、画面のボタンが永久に押せなくなるため。
    now = now or datetime.now(timezone.utc)
    run = _read(_running())
    if run and now - datetime.fromisoformat(run["started_utc"]) > limit:
        finish()
