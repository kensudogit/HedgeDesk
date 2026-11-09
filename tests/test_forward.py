"""フォワード記録（forward.py）のテスト: 登録の固定、記録開始日より前を使わないこと、整合の判定。"""
import json
from datetime import datetime, timezone

import pandas as pd
from test_signals_and_metrics import synthetic_context

from hedgedesk import config as C
from hedgedesk import forward
from hedgedesk.strategies import STRATEGIES, trial_id

ST = next(s for s in STRATEGIES if s.id == "S3_MR_ETF")


def research_result(params, mean=0.2, std=1.0, passed=False):
    """research の検証結果のうち、forward が使う項目だけ。"""
    return {"id": ST.id, "status": "TESTED", "final_params": params, "passed": passed, "oos_r": {"n": 100, "mean": mean, "std": std}}


def test_enroll_freezes_params_and_start_date():
    """一度登録した試行は、後の調査で開始日もパラメータも変わらない。パラメータが変われば別の試行として登録する。"""
    p0, p1 = ST.params()[0], ST.params()[1]
    forward.enroll([research_result(p0)], datetime(2026, 11, 2, 12, tzinfo=timezone.utc))
    reg = forward.enroll([research_result(p0), research_result(p1)], datetime(2026, 11, 9, 12, tzinfo=timezone.utc))
    assert reg[trial_id(ST.id, p0)]["start"] == "2026-11-02"
    assert reg[trial_id(ST.id, p1)]["start"] == "2026-11-09"
    assert json.loads((C.STATE / "forward_registry.json").read_text(encoding="utf-8")) == reg


def test_forward_uses_only_bars_from_start():
    """記録開始日より前の足ではトレードしない。全トレードの約定日が開始日以降になる。"""
    ctx = synthetic_context()
    start = ctx.market["SPY"].index[600]
    p = ST.params()[0]
    out = forward.update([research_result(p)], ctx, start.tz_localize("UTC").to_pydatetime())
    row = out["rows"][0]
    assert row["start"] == str(start.date()) and row["bars"] == len(ctx.market["SPY"].loc[start:])
    res_trades = forward.simulate(ctx.market, ST.signals(ctx, p), row["start"], None, forward.costs_for("etf")).trades
    assert len(res_trades) == row["trades"]
    assert (pd.to_datetime(res_trades["entry_date"]) > start).all()


def test_consistency_needs_enough_trades_and_uses_90pct_range():
    """件数が少ない間は判定しない。10件以上で、平均R がバックテストの90%範囲に入るかを見る。"""
    few = forward.consistency([1.0] * 5, 0.1, 1.0)
    assert few["consistent"] is None and few["range90"] is not None
    ok = forward.consistency([0.5, -0.3] * 5, 0.1, 1.0)   # 平均0.1、範囲は 0.1 ± 1.645/√10
    assert ok["consistent"] is True and ok["range90"] == [round(0.1 - 1.645 / 10 ** 0.5, 4), round(0.1 + 1.645 / 10 ** 0.5, 4)]
    bad = forward.consistency([-1.0] * 10, 0.1, 1.0)
    assert bad["consistent"] is False
    assert forward.consistency([], 0.1, 1.0)["mean_r"] is None
    assert forward.consistency([0.2] * 12, None, None)["consistent"] is None


def test_old_params_move_to_history_and_signals_are_view_only():
    """現在のパラメータの登録だけを表の行にし、古い登録は history に残す。今日のシグナルは一覧だけで発注しない。"""
    ctx = synthetic_context()
    now = ctx.market["SPY"].index[700].tz_localize("UTC").to_pydatetime()
    p0, p1 = ST.params()[0], ST.params()[1]
    forward.enroll([research_result(p0)], now)
    out = forward.update([research_result(p1)], ctx, now)
    assert [r["trial"] for r in out["rows"]] == [trial_id(ST.id, p1)]
    assert [h["trial"] for h in out["history"]] == [trial_id(ST.id, p0)]
    for s in out["rows"][0]["signals_today"]:
        assert set(s) == {"symbol", "date", "limit", "stop"} and s["stop"] < s["limit"]
    assert not (C.STATE / "positions.json").exists()
