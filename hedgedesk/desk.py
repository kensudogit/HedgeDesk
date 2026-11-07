"""Deskの運行: 5分ごとの tick、ブリーフ用データ、週次レビュー。

tick は判断にLLMを使わないので、常時稼働のサーバーでもそのまま動く。
時刻はニューヨーク時間とUTCで判定するので、夏時間の切り替えでずれない。
"""
from __future__ import annotations

import json
import os
import sys
import time as _time
import traceback
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from . import analyst, config as C, events, journal, risk, scout, trader

NY = ZoneInfo("America/New_York")
TICKS = C.STATE / "ticks.json"
APPROVED = C.STATE / "approved_plans.json"
LOCK_STALE_SECONDS = 1800


def _load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _save(path, value):
    C.STATE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _book_state(broker) -> tuple[float, float, set[str]]:
    """審査に使う資金、保有中のリスク、保有銘柄。"""
    book = trader._load_positions()
    held = {r["symbol"] for r in book.values()}
    open_risk = sum(max(0.0, r.get("entry_price", r["limit"]) - r["stop"]) * r.get("filled_qty", r["qty"]) for r in book.values())
    equity = float(broker.account()["equity"]) if broker.available else C.CAPITAL
    return equity, open_risk, held


def job_signals(asset_classes: set[str], now: datetime, submit_now: bool, broker=None) -> dict:
    """Chart Analyst → Risk Officer。暗号資産はそのまま発注、株は寄付き前まで保留。"""
    broker = broker or trader.AlpacaPaper()
    out = analyst.run(asset_classes, now)
    if out["status"] != "ok":
        return {k: v for k, v in out.items() if k != "ctx"}
    equity, open_risk, held = _book_state(broker)
    approved, vetoed = risk.review(out["plans"], equity, open_risk, held, out["champion"], now)
    plans_key = "crypto" if "crypto" in asset_classes else "equity"
    store = _load(APPROVED, {})
    store[plans_key] = approved
    _save(APPROVED, store)
    if approved and not out["champion"].get("launched"):
        champ = dict(out["champion"], launched=True, launched_utc=now.isoformat(timespec="seconds"))
        _save(analyst.CHAMPION, champ)
    sent = trader.submit(approved, broker) if submit_now else []
    _manage_exits(out, asset_classes, broker)
    return {"status": "ok", "approved": len(approved), "vetoed": len(vetoed), "sent": sent}


def _manage_exits(out: dict, asset_classes: set[str], broker) -> None:
    """保有本数の上限とシグナルで手仕舞い予約を判定する。ストップは別に常時有効。"""
    if not broker.available:
        return
    book = trader._load_positions()
    changed = False
    for pid, rec in book.items():
        if rec.get("state") != "open" or rec["asset_class"] not in asset_classes:
            continue
        held, signal = analyst.bars_held_and_exit(out["ctx"], rec, out["champion"])
        rec["bars_held"] = held
        changed = True
        if held >= rec["max_hold"]:
            rec["exit_due"] = "time"
        elif signal and held >= C.MIN_HOLD_BARS:
            rec["exit_due"] = "signal"
    if changed:
        trader._save_positions(book)


def job_exits(asset_classes: set[str], broker=None) -> int:
    broker = broker or trader.AlpacaPaper()
    n = 0
    if not broker.available:
        return n
    book = trader._load_positions()
    for pid, rec in book.items():
        if rec.get("state") == "open" and rec["asset_class"] in asset_classes and rec.get("exit_due") and not rec.get("exit_sent"):
            trader.exit_position(pid, rec["exit_due"], broker)
            rec["exit_sent"] = True
            n += 1
    trader._save_positions(book)
    return n


def job_preopen_recheck(now: datetime, broker=None) -> dict:
    """寄付き前の再審査: 保留中の計画をもう一度 Risk Officer に通す。"""
    broker = broker or trader.AlpacaPaper()
    store = _load(APPROVED, {})
    plans = store.get("equity", [])
    if not plans:
        return {"status": "nothing"}
    equity, open_risk, held = _book_state(broker)
    approved, vetoed = risk.review([{k: v for k, v in p.items() if k != "approval"} for p in plans], equity, open_risk, held, analyst.load_champion(), now)
    store["equity"] = approved
    _save(APPROVED, store)
    return {"status": "ok", "approved": len(approved), "vetoed": len(vetoed)}


def job_submit_equity(broker=None) -> dict:
    store = _load(APPROVED, {})
    plans = store.get("equity", [])
    sent = trader.submit(plans, broker) if plans else []
    store["equity"] = []
    _save(APPROVED, store)
    return {"sent": sent}


def _due(state: dict, name: str, key: str) -> bool:
    return state.get(name) != key


def _acquire_lock() -> bool:
    """同時に1つの tick だけが動くようにする。Windowsタスクとコンテナの二重起動も防ぐ。"""
    C.STATE.mkdir(parents=True, exist_ok=True)
    path = C.STATE / "tick.lock"
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if _time.time() - path.stat().st_mtime < LOCK_STALE_SECONDS:
                    return False
                path.unlink()  # 異常終了で残ったロックは片付けて、もう一度だけ試す
            except FileNotFoundError:
                pass
    return False


def tick(now: datetime | None = None, broker=None) -> dict:
    """5分ごとの処理の入口。別の tick が実行中なら何もしない。"""
    if not _acquire_lock():
        return {"status": "skipped_locked"}
    try:
        return _tick(now, broker)
    finally:
        (C.STATE / "tick.lock").unlink(missing_ok=True)


def run_loop(interval_seconds: int = 300, max_iterations: int | None = None, sleep=_time.sleep) -> None:
    """コンテナで常駐するときのループ。1回失敗しても止まらず、次の回に進む。"""
    n = 0
    while max_iterations is None or n < max_iterations:
        try:
            print(json.dumps(tick(), ensure_ascii=False, default=str), flush=True)
        except Exception:
            print(traceback.format_exc(), file=sys.stderr, flush=True)
        n += 1
        if max_iterations is None or n < max_iterations:
            sleep(interval_seconds)


def _tick(now: datetime | None = None, broker=None) -> dict:
    """各ジョブは1日1回（Scoutは30分ごと）だけ実行する。"""
    now = now or datetime.now(timezone.utc)
    ny = now.astimezone(NY)
    state = _load(TICKS, {})
    today_ny = ny.date().isoformat()
    today_utc = now.date().isoformat()
    weekday = ny.weekday() < 5
    ran = {}

    def run(name, key, fn):
        if not _due(state, name, key):
            return
        state[name] = key  # 失敗しても同じ日に繰り返し発注しないよう、先に記録する
        _save(TICKS, state)
        try:
            ran[name] = fn()
        except Exception as ex:
            ran[name] = f"error: {type(ex).__name__}"
            journal.log("job_error", "Chief of Staff", job=name, error=traceback.format_exc()[-800:])

    last_scout = state.get("scout_ts")
    if not last_scout or now - datetime.fromisoformat(last_scout) >= timedelta(minutes=30):
        state["scout_ts"] = now.isoformat(timespec="seconds")
        _save(TICKS, state)
        try:
            ran["scout"] = scout.collect(now)
        except Exception as ex:
            ran["scout"] = f"error: {type(ex).__name__}"

    ran["monitor"] = trader.monitor(broker, now)

    t = ny.time()
    if weekday and time(8, 45) <= t < time(9, 15):
        run("recheck", today_ny, lambda: job_preopen_recheck(now, broker))
    if weekday and time(9, 15) <= t < time(11, 0):
        run("submit_equity", today_ny, lambda: job_submit_equity(broker))
    if weekday and time(9, 20) <= t < time(11, 0):
        run("exits_equity", today_ny, lambda: job_exits({"etf", "stock"}, broker))
    if weekday and time(15, 50) <= t < time(16, 15):
        run("cancel_equity", today_ny, lambda: {"etf": trader.cancel_unfilled("etf", broker), "stock": trader.cancel_unfilled("stock", broker)})
    if weekday and t >= time(16, 15):
        run("signals_equity", today_ny, lambda: job_signals({"etf", "stock"}, now, False, broker))
    if now.time() >= time(0, 5):
        run("signals_crypto", today_utc, lambda: (trader.cancel_unfilled("crypto", broker, 23.0, now), job_signals({"crypto"}, now, True, broker), job_exits({"crypto"}, broker))[1])

    log_line = {"ts": now.isoformat(timespec="seconds"), "ran": ran}
    with open(C.STATE / "tick.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(log_line, ensure_ascii=False, default=str) + "\n")
    return log_line


def market_snapshot() -> dict:
    """ブリーフ用の市場データ。計算した値だけを返す。"""
    from .research import build_context

    ctx, log = build_context(refresh=True)
    rows = {}
    for s in C.INDEX_ETFS + C.SECTOR_ETFS + C.CRYPTO:
        df = ctx.market.get(s)
        if df is None or len(df) < 2:
            continue
        last = df.iloc[-1]
        rows[s] = {
            "date": str(df.index[-1].date()), "close": round(float(last["close"]), 2),
            "ret_1d": round(float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1), 4),
            "ret_21d": round(float(df["close"].iloc[-1] / df["close"].iloc[-22] - 1), 4) if len(df) > 22 else None,
            "above_sma200": bool(last["close"] > last["sma200"]),
            "volume_vs_avg20": round(float(last["volume"] / last["vol_avg20"]), 2) if last["vol_avg20"] else None,
            "score": None if last["score"] != last["score"] else int(last["score"]),
        }
    unusual = []
    for s in C.STOCKS + C.ETFS:
        df = ctx.market.get(s)
        if df is not None and len(df) > 21 and df["vol_avg20"].iloc[-1] and df["volume"].iloc[-1] / df["vol_avg20"].iloc[-1] >= 2:
            unusual.append({"symbol": s, "volume_vs_avg20": round(float(df["volume"].iloc[-1] / df["vol_avg20"].iloc[-1]), 2), "ret_1d": round(float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1), 4)})
    vix = round(float(ctx.vix.iloc[-1]), 2) if ctx.vix is not None and len(ctx.vix) else None
    return {"source": log["source"], "rows": rows, "vix": vix, "vix_date": str(ctx.vix.index[-1].date()) if vix else None, "unusual_volume": unusual}


def brief_data(now: datetime | None = None) -> dict:
    """Chief of Staff がブリーフを書くための材料を1つのJSONにまとめる。"""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = journal.read(since)
    gate_ok, gate_reasons = events.launch_gate(now)
    latest = _load(C.STATE / "latest_research.json", None)
    broker = trader.AlpacaPaper()
    account = None
    if broker.available:
        try:
            a = broker.account()
            account = {"equity": a["equity"], "last_equity": a["last_equity"]}
        except Exception as ex:
            account = {"error": type(ex).__name__}
    out = {
        "as_of_utc": now.isoformat(timespec="seconds"),
        "market": market_snapshot(),
        "events": events.upcoming(now, days=7),
        "launch_gate": {"ok": gate_ok, "reasons": gate_reasons},
        "research": latest,
        "champion": analyst.load_champion(),
        "vetoes_24h": [r for r in rows if r["kind"] == "veto"],
        "trades_24h": [r for r in rows if r["kind"] in ("order", "fill_opened", "fill_closed", "exit_order", "order_not_sent", "emergency_exit", "close_unknown")],
        "errors_24h": [r for r in rows if r["kind"] == "job_error"],
        "kill_switch": risk.kill_status(),
        "alpaca_connected": broker.available,
        "account": account,
        "open_book": trader._load_positions(),
        "scout_headlines_untrusted": scout.recent(60, now),
    }
    _save(C.STATE / "brief_data.json", out)
    return out


def weekly_review(now: datetime | None = None, seed: int = C.MC_SEED) -> dict:
    """ペーパーとバックテストを比較して、昇格の階段の位置を判定する。"""
    now = now or datetime.now(timezone.utc)
    champ = analyst.load_champion()
    latest = _load(C.STATE / "latest_research.json", None)
    closed = [r for r in journal.read() if r["kind"] == "fill_closed"]
    result = {"as_of_utc": now.isoformat(timespec="seconds"), "champion": champ, "paper_trades": len(closed), "rung": "no_champion"}
    if champ:
        closed = [r for r in closed if r.get("strategy") == champ["id"] and r.get("r") is not None]
        launched = champ.get("launched_utc")
        days = (now - datetime.fromisoformat(launched)).days if launched else 0
        result.update(paper_trades=len(closed), paper_days=days)
        top = next((r for r in (latest or {}).get("ranked", []) if r["id"] == champ["id"]), None)
        if not launched:
            result["rung"] = "not_launched"
        elif days < 14:
            result["rung"] = "paper_under_2_weeks"
        elif not closed or top is None:
            result["rung"] = "insufficient_data"
        else:
            result["rung"] = "paper_review"
        if closed and top:
            r = np.array([x["r"] for x in closed], dtype=float)
            result["paper_expectancy_r"] = round(float(r.mean()), 3)
            result["backtest_expectancy_r"] = top["out_of_sample"]["expectancy_r"]
            # バックテストの1トレード分布が手元にないので、期待値と標準偏差の推定はペーパー側で行う
            rng = np.random.default_rng(seed)
            boots = [rng.choice(r, size=r.size, replace=True).mean() for _ in range(C.MC_RUNS)]
            lo, hi = np.percentile(boots, [5, 95])
            result["paper_ci90"] = [round(float(lo), 3), round(float(hi), 3)]
            matches = lo <= top["out_of_sample"]["expectancy_r"] <= hi
            result["matches_backtest"] = bool(matches)
            if result["rung"] == "paper_review":
                result["rung"] = "eligible_for_owner_decision" if matches else "does_not_match_backtest"
    C.REPORTS.mkdir(parents=True, exist_ok=True)
    _save(C.REPORTS / f"weekly_{now.strftime('%Y%m%d')}.json", result)
    journal.log("weekly_review", "Chief of Staff", **{k: result.get(k) for k in ("rung", "paper_trades", "matches_backtest")})
    return result
