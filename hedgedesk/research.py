"""Strategist: 全仮説をウォークフォワードとホールドアウトで検証し、採点して順位を出す。

検証の流れ:
  1. 全パラメータを 2018〜2024 で計算（試行として全部数える）
  2. ウォークフォワード: 直前3年で最良のパラメータを選び、次の6か月に適用
  3. ホールドアウト: 2018〜2024で選んだパラメータを 2025年以降に1組だけ適用
  4. ストレス: 1本遅れ＋スリッページ2倍、2020年、2022年、モンテカルロ
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config as C
from . import data as D
from . import journal
from . import metrics as M
from .backtest import Result, costs_for, simulate
from .strategies import BLOCKED_HYPOTHESES, STRATEGIES, Context, Strategy, prepare, trial_id

REGISTRY = C.STATE / "trial_registry.json"
LATEST = C.STATE / "latest_research.json"
CHAMPION = C.STATE / "champion.json"


def build_context(refresh: bool = False, now: datetime | None = None) -> tuple[Context, dict]:
    symbols = sorted(set(C.ETFS + C.STOCKS + C.CRYPTO + [C.VIX]))
    raw, log = D.load(symbols, refresh=refresh, now=now)
    earnings, failed = D.earnings_dates(C.STOCKS, refresh=refresh)
    log["earnings_failed"] = failed
    vix = raw[C.VIX]["close"] if len(raw.get(C.VIX, [])) else None
    return Context(prepare(raw), vix, earnings), log


def _windows() -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """(学習開始, 検証開始, 検証終了) の一覧。"""
    out = []
    test_start = pd.Timestamp(C.IS_START) + pd.DateOffset(years=C.TRAIN_YEARS)
    end = pd.Timestamp(C.IS_END)
    while test_start <= end:
        test_end = min(test_start + pd.DateOffset(months=C.TEST_MONTHS) - pd.Timedelta(days=1), end)
        out.append((test_start - pd.DateOffset(years=C.TRAIN_YEARS), test_start, test_end))
        test_start = test_start + pd.DateOffset(months=C.TEST_MONTHS)
    return out


def _stitch(pieces: list[Result]) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    rets = [p.returns for p in pieces if not p.equity.empty]
    if not rets:
        empty = pd.Series(dtype=float)
        return empty, empty, pd.DataFrame(columns=["pnl", "r"])
    r = pd.concat(rets)
    eq = C.CAPITAL * (1 + r).cumprod()
    frames = [p.trades for p in pieces if len(p.trades)]
    tr = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=pieces[0].trades.columns)
    return r, eq, tr


def _best(trials: list[dict], start: pd.Timestamp, end: pd.Timestamp, periods: int, min_trades: int = 10) -> dict | None:
    """指定期間でシャープが最大のパラメータを選ぶ。取引が少ないものは対象外。"""
    best, best_s = None, -math.inf
    for t in trials:
        res: Result = t["result"]
        tr = res.trades
        n = int(((tr["entry_date"] >= start) & (tr["entry_date"] <= end)).sum()) if len(tr) else 0
        if n < min_trades:
            continue
        r = res.returns.loc[start:end]
        s = M.sharpe(r, periods)
        if s > best_s:
            best, best_s = t, s
    return best


def evaluate(st: Strategy, ctx: Context) -> dict:
    """1つの戦略を検証する。ここでは DSR 以外をすべて計算する。"""
    periods = C.ANNUALIZATION[st.asset_class]
    costs = costs_for(st.asset_class)
    fractional = st.asset_class == "crypto"
    market = ctx.market
    run = lambda sig, start, end, lag=0, cost=costs: simulate(market, sig, start, end, cost, lag=lag, fractional=fractional)

    trials = []
    for p in st.params():
        sig = st.signals(ctx, p)
        if not sig:
            continue
        res = run(sig, C.IS_START, C.IS_END)
        trials.append({"params": p, "signals": sig, "result": res, "id": trial_id(st.id, p), "ann_sharpe": M.sharpe(res.returns, periods)})

    base = {
        "id": st.id, "name": st.name, "asset_class": st.asset_class, "thesis": st.thesis, "other_side": st.other_side,
        "breaks_when": st.breaks_when, "periods": periods,
        "trials": [{"id": t["id"], "params": t["params"], "ann_sharpe": round(t["ann_sharpe"], 4), "is_trades": len(t["result"].trades)} for t in trials],
    }
    if not trials:
        return {**base, "status": "NO_DATA", "failures": ["シグナルを計算できるデータがない"]}

    wf_rows, oos_pieces, stress_pieces = [], [], []
    for train_start, test_start, test_end in _windows():
        chosen = _best(trials, train_start, test_start - pd.Timedelta(days=1), periods)
        row = {"test_start": str(test_start.date()), "test_end": str(test_end.date()), "params": chosen["params"] if chosen else None}
        if chosen is None:
            row.update(sharpe=None, trades=0, net_pnl=0.0, note="学習期間の取引が10回未満なので見送り")
        else:
            res = run(chosen["signals"], str(test_start.date()), str(test_end.date()))
            oos_pieces.append(res)
            stress_pieces.append(run(chosen["signals"], str(test_start.date()), str(test_end.date()), lag=1, cost=costs.doubled_slip()))
            row.update(sharpe=round(M.sharpe(res.returns, periods), 3), trades=len(res.trades), net_pnl=round(float(res.trades["pnl"].sum()), 2) if len(res.trades) else 0.0)
        wf_rows.append(row)

    final = _best(trials, pd.Timestamp(C.IS_START), pd.Timestamp(C.IS_END), periods)
    if final is None:
        return {**base, "status": "TOO_FEW_TRADES", "failures": ["2018〜2024で取引が10回に届くパラメータがない"], "walk_forward": wf_rows}

    in_sample = final["result"]
    holdout = run(final["signals"], C.HOLDOUT_START, None)
    holdout_stress = run(final["signals"], C.HOLDOUT_START, None, lag=1, cost=costs.doubled_slip())
    oos_r, oos_eq, oos_tr = _stitch(oos_pieces + [holdout])
    st_r, st_eq, st_tr = _stitch(stress_pieces + [holdout_stress])
    y2020 = run(final["signals"], "2020-01-01", "2020-12-31")
    y2022 = run(final["signals"], "2022-01-01", "2022-12-31")
    kills = sum(len(p.kills) for p in oos_pieces + [holdout])

    return {
        **base,
        "status": "TESTED",
        "final_params": final["params"],
        "rules": {
            "entry": st.entry_rule.format(**final["params"]),
            "exit": st.exit_rule.format(**final["params"]),
            "stop": st.stop_rule.format(**final["params"]),
            "sizing": f"数量 = 資金×{C.RISK_PER_TRADE:.1%} ÷ (実約定価格 − ストップ)。1銘柄の時価は資金の{C.MAX_POSITION_NOTIONAL:.0%}まで、合計リスクは{C.MAX_OPEN_RISK:.1%}まで。",
        },
        "in_sample": M.summarize(in_sample.returns, in_sample.equity, in_sample.trades, periods),
        "out_of_sample": M.summarize(oos_r, oos_eq, oos_tr, periods),
        "holdout": M.summarize(holdout.returns, holdout.equity, holdout.trades, periods),
        "walk_forward": wf_rows,
        "stress_late_double_slip": M.summarize(st_r, st_eq, st_tr, periods),
        "year_2020": M.summarize(y2020.returns, y2020.equity, y2020.trades, periods),
        "year_2022": M.summarize(y2022.returns, y2022.equity, y2022.trades, periods),
        "monte_carlo": M.monte_carlo_drawdown(oos_tr["r"].to_numpy(dtype=float) if len(oos_tr) else np.array([])),
        "kill_switch_fires_oos": kills,
        "_oos_returns": oos_r,
    }


def apply_gates(res: dict) -> dict:
    """スコアと失格条件を入れる。第8節のとおりで、結果を見て変えない。"""
    if res.get("status") != "TESTED":
        return res
    oos, st = res["out_of_sample"], res["stress_late_double_slip"]
    exp = oos["expectancy_r"] if oos["expectancy_r"] is not None else 0.0
    card = M.score_card(oos["sharpe"], res["deflated_sharpe"], oos["max_drawdown"], oos["trades"], exp)
    failures = []
    if oos["net_pnl"] <= 0:
        failures.append("OOSのコスト後損益が0以下")
    if oos["trades"] < C.MIN_OOS_TRADES:
        failures.append(f"OOSトレードが{oos['trades']}回で{C.MIN_OOS_TRADES}回未満")
    if st["net_pnl"] <= 0:
        failures.append("1本遅れ＋スリッページ2倍で損益が0以下")
    if res["deflated_sharpe"] < C.MIN_DSR:
        failures.append(f"デフレートシャープが{res['deflated_sharpe']:.2f}で{C.MIN_DSR}未満")
    for y in ("year_2020", "year_2022"):
        if res[y]["max_drawdown"] > C.MAX_STRESS_DD:
            failures.append(f"{y[-4:]}年のドローダウンが{res[y]['max_drawdown']:.1%}")
    if res["holdout"]["net_pnl"] <= 0:
        failures.append("ホールドアウトの損益が0以下")
    if card["total"] < C.SCORE_KEEP:
        failures.append(f"スコア{card['total']}で{C.SCORE_KEEP}未満")
    if oos["sharpe"] > C.SUSPICIOUS_SHARPE:
        failures.append(f"OOSシャープ{oos['sharpe']}は異常に高い。不具合を再検査するまで失格")
    res["score"] = card
    res["failures"] = failures
    res["passed"] = not failures
    res["final_check"] = {
        "out_of_sample": oos["net_pnl"] > 0,
        "after_costs": exp > 0,
        "late_double_slip": st["net_pnl"] > 0,
        "deflated_sharpe": res["deflated_sharpe"] >= C.MIN_DSR,
        "survived_2020": res["year_2020"]["max_drawdown"] <= C.MAX_STRESS_DD,
        "survived_2022": res["year_2022"]["max_drawdown"] <= C.MAX_STRESS_DD,
    }
    caveats = [res["breaks_when"]]
    if res["asset_class"] == "stock":
        caveats.append("銘柄は今日時点の大型株で、生存バイアスがあり、成績は実力より良く見えている可能性が高い。")
        caveats.append("過去の決算日は実績の日付を使っている。本番では予定日が変わる可能性がある。")
    if res["asset_class"] == "crypto":
        caveats.append("検証価格はYahooで、発注先のAlpacaとは価格も手数料後の約定も異なる。")
    if res["year_2020"]["trades"] < 5 or res["year_2022"]["trades"] < 5:
        caveats.append("2020年か2022年の取引数が少なく、危機時の頑健性はほぼ未検証。")
    res["why_this_could_fail"] = caveats
    return res


def update_registry(evaluated: list[dict]) -> dict:
    """試行は一度数えたら減らさない。夜ごとに増えた分だけ DSR が厳しくなる。"""
    reg = json.loads(REGISTRY.read_text(encoding="utf-8")) if REGISTRY.exists() else {}
    today = datetime.now(timezone.utc).date().isoformat()
    for res in evaluated:
        for t in res.get("trials", []):
            rec = reg.setdefault(t["id"], {"strategy": res["id"], "params": t["params"], "first_seen": today})
            rec["ann_sharpe"] = t["ann_sharpe"]
            rec["periods"] = res["periods"]
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    return reg


def run_research(refresh: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    ctx, data_log = build_context(refresh=refresh, now=now)
    evaluated = [evaluate(st, ctx) for st in STRATEGIES]
    reg = update_registry(evaluated)
    n_trials = len(reg)
    trial_srs = [v["ann_sharpe"] / math.sqrt(v["periods"]) for v in reg.values()]
    for res in evaluated:
        if res.get("status") == "TESTED":
            res["deflated_sharpe"] = round(M.deflated_sharpe(res.pop("_oos_returns"), trial_srs, n_trials), 4)
            apply_gates(res)
    tested = [r for r in evaluated if r.get("status") == "TESTED"]
    tested.sort(key=lambda r: (r["passed"], r["score"]["total"], r["out_of_sample"]["sharpe"]), reverse=True)
    passed = [r for r in tested if r["passed"]]
    for r in evaluated:
        journal.log(
            "research", "Strategist", TICKER=r["asset_class"], IDEA=r["id"],
            EVIDENCE={"score": r.get("score", {}).get("total"), "oos": r.get("out_of_sample"), "dsr": r.get("deflated_sharpe")},
            NEXT_STEP="passed" if r.get("passed") else "rejected: " + "; ".join(r.get("failures", [])),
        )
    report = {
        "as_of_utc": now.isoformat(timespec="seconds"),
        "data": {"source": data_log["source"], "fingerprint": data_log["fingerprint"], "earnings_failed": data_log["earnings_failed"], "issues": {k: v["issues"] for k, v in data_log["symbols"].items() if v["issues"]}, "coverage": data_log["symbols"]},
        "n_trials_total": n_trials,
        "blocked_hypotheses": BLOCKED_HYPOTHESES,
        "ranked": tested,
        "untested": [r for r in evaluated if r.get("status") != "TESTED"],
        "passed_ids": [r["id"] for r in passed],
    }
    C.STATE.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    C.REPORTS.mkdir(parents=True, exist_ok=True)
    (C.REPORTS / f"research_{now.strftime('%Y%m%d')}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    _update_champion(passed, now)
    return report


def _update_champion(passed: list[dict], now: datetime) -> None:
    """基準を通った戦略があるときだけチャンピオンにする。なければ外す（取引しない）。"""
    old = json.loads(CHAMPION.read_text(encoding="utf-8")) if CHAMPION.exists() else None
    if not passed:
        if old:
            CHAMPION.unlink()
            journal.log("champion", "Strategist", action="removed", was=old["id"], reason="基準を通る戦略なし")
        return
    top = passed[0]
    same = old and old["id"] == top["id"] and old["params"] == top["final_params"]
    if same:
        return
    champ = {"id": top["id"], "asset_class": top["asset_class"], "params": top["final_params"], "selected_utc": now.isoformat(timespec="seconds"), "launched": False, "score": top["score"]["total"]}
    CHAMPION.write_text(json.dumps(champ, ensure_ascii=False, indent=1), encoding="utf-8")
    journal.log("champion", "Strategist", action="selected", **{k: champ[k] for k in ("id", "params", "score")})
