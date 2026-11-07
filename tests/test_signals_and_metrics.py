"""先読みの検査と、統計指標の検査。"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from conftest import random_walk
from hedgedesk import config as C, data as D, events, metrics as M
from hedgedesk.research import _windows
from hedgedesk.strategies import STRATEGIES, Context, earnings_block, prepare, trial_id


def synthetic_context(end=None):
    raw = {}
    for k, s in enumerate(sorted(set(C.ETFS + C.STOCKS + C.CRYPTO))):
        raw[s] = random_walk(k + 1, drift=0.0006)
        raw[s]["dollar_volume"] = 5e8
    vix = pd.Series(20 + np.cumsum(np.random.default_rng(99).normal(0, 1, 900)).clip(-10, 30), index=raw["SPY"].index)
    earnings = {}
    for s in C.STOCKS:
        days = raw[s].index[30::63]
        earnings[s] = pd.DataFrame({"reaction": days, "surprise": 8.0})
    if end is not None:
        raw = {s: df.loc[:end] for s, df in raw.items()}
        vix = vix.loc[:end]
    return Context(prepare(raw), vix, earnings)


@pytest.mark.parametrize("st", STRATEGIES, ids=lambda s: s.id)
def test_signals_do_not_change_when_future_is_removed(st):
    """ある日のシグナルは、その日より後のデータを消しても変わらない。"""
    full = synthetic_context()
    cut_date = full.market["SPY"].index[700]
    cut = synthetic_context(end=cut_date)
    p = st.params()[0]
    a, b = st.signals(full, p), st.signals(cut, p)
    assert a
    for s in a:
        x, y = a[s].loc[:cut_date], b[s]
        for col in ("entry", "exit", "limit", "stop"):
            pd.testing.assert_series_equal(x[col], y[col], check_names=False, check_freq=False)


@pytest.mark.parametrize("st", STRATEGIES, ids=lambda s: s.id)
def test_entries_require_score_and_stop_below_limit(st):
    ctx = synthetic_context()
    for s, sig in st.signals(ctx, st.params()[0]).items():
        hits = sig[sig["entry"] > 0]
        assert (hits["score"] >= C.MIN_SETUP_SCORE).all()
        assert (hits["stop"] < hits["limit"]).all()


def test_earnings_block_before_report_and_unknown_dates():
    idx = pd.bdate_range("2024-01-01", periods=60)
    reactions = pd.Series([idx[5], idx[40]])
    b = earnings_block(idx, reactions, horizon=11)
    assert b.iloc[29:40].all()     # 決算前の11本
    assert not b.iloc[20]          # 決算まで遠い日
    assert not b.iloc[40]          # 決算当日の反応後は決算ドリフトで入れる
    assert b.iloc[41:].all()       # 次の決算日が不明
    assert earnings_block(idx, None).all()


def test_drop_incomplete_bars():
    idx = pd.to_datetime(["2026-11-05", "2026-11-06"])
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    during = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)  # NY 13:00
    after = datetime(2026, 11, 6, 22, 0, tzinfo=timezone.utc)   # NY 17:00
    assert len(D.drop_incomplete(df, "SPY", during)) == 1
    assert len(D.drop_incomplete(df, "SPY", after)) == 2
    assert len(D.drop_incomplete(df, "BTC-USD", after)) == 1   # UTCの当日足は未確定
    assert len(D.drop_incomplete(df, "BTC-USD", datetime(2026, 11, 7, 0, 10, tzinfo=timezone.utc))) == 2


def test_quality_issues_detect_bad_data():
    df = pd.DataFrame({"open": [1.0], "high": [0.5], "low": [0.4], "close": [0.45]}, index=pd.to_datetime(["2024-01-01"]))
    assert D.quality_issues(df)
    assert D.quality_issues(pd.DataFrame()) == ["no data"]


def test_walk_forward_windows_never_overlap_train_and_test():
    w = _windows()
    assert len(w) == 8
    for train, test, end in w:
        assert train < test <= end
        assert test - train >= pd.Timedelta(days=365 * 3)
        assert end <= pd.Timestamp(C.IS_END) < pd.Timestamp(C.HOLDOUT_START)


def test_dsr_falls_as_trials_grow():
    rng = np.random.default_rng(0)
    r = pd.Series(rng.normal(0.001, 0.01, 750))
    trial_srs = list(rng.normal(0, 0.03, 500))
    few = M.deflated_sharpe(r, trial_srs, 10)
    many = M.deflated_sharpe(r, trial_srs, 500)
    assert 0 <= many < few <= 1


def test_expected_max_sharpe_known_shape():
    assert M.expected_max_sharpe([0.1, -0.1], 1) == 0.0
    a = M.expected_max_sharpe([0.1, -0.1, 0.05, -0.05], 10)
    b = M.expected_max_sharpe([0.1, -0.1, 0.05, -0.05], 1000)
    assert 0 < a < b


def test_monte_carlo_reproducible():
    r = np.array([1.0, -1.0, 0.5, -0.5, 2.0, -1.0] * 10)
    a = M.monte_carlo_drawdown(r)
    assert a == M.monte_carlo_drawdown(r)
    assert a["runs"] == 1000 and 0 < a["median"] <= a["p95"] <= a["worst"]
    assert M.monte_carlo_drawdown(np.array([]))["worst"] is None


@pytest.mark.parametrize("args,expected", [
    ((1.2, 0.95, 0.09, 200, 0.2), 25),
    ((0.1, 0.1, 0.40, 10, -0.1), 5),
    ((0.6, 0.80, 0.15, 60, 0.05), 3 + 3 + 4 + 3 + 3),
    ((0.9, 0.90, 0.25, 100, 0.10), 4 + 4 + 3 + 4 + 4),
])
def test_score_card_bands(args, expected):
    assert M.score_card(*args)["total"] == expected


def test_trial_id_stable_and_unique():
    assert trial_id("A", {"x": 1, "y": 2}) == trial_id("A", {"y": 2, "x": 1})
    assert trial_id("A", {"x": 1}) != trial_id("A", {"x": 2})


def test_all_grids_stay_within_swing_and_register_trials():
    total = sum(len(s.params()) for s in STRATEGIES)
    assert total > 50
    for st in STRATEGIES:
        for p in st.params():
            assert p.get("max_hold", p.get("hold", 10)) <= C.MAX_HOLD_BARS


def test_no_banned_symbol_in_universe():
    assert not (set(C.ETFS + C.STOCKS + C.CRYPTO) & C.BANNED)


def test_parse_fomc():
    html = """<h4>2026 FOMC Meetings</h4>
    <div class="fomc-meeting__month"><strong>January</strong></div><div class="fomc-meeting__date">27-28</div>
    <div class="fomc-meeting__month"><strong>Apr/May</strong></div><div class="fomc-meeting__date">28-29</div>
    <div class="fomc-meeting__month"><strong>September</strong></div><div class="fomc-meeting__date">15-16*</div>
    <h4>2027 FOMC Meetings</h4>
    <div class="fomc-meeting__month"><strong>March</strong></div><div class="fomc-meeting__date">16-17</div>"""
    days = [d.isoformat() for d in events.parse_fomc(html)]
    assert days == ["2026-01-28", "2026-05-29", "2026-09-16", "2027-03-17"]


def test_launch_blocked_when_dates_unknown(monkeypatch):
    def boom():
        raise OSError
    monkeypatch.setattr(events, "fetch_fomc", boom)
    monkeypatch.setattr(events, "MANUAL", C.STATE / "none.json")
    ok, reasons = events.launch_gate(datetime(2026, 11, 7, tzinfo=timezone.utc))
    assert not ok
    assert any("CPI" in r for r in reasons) and any("FOMC" in r for r in reasons)
