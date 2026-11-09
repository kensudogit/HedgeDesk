"""先読みの検査と、統計指標の検査。"""
#
# 【対象】hedgedesk/strategies.py（先読みがないこと、共通の関門、決算跨ぎ）、data.py（未確定足・品質）、
#   research._windows、metrics.py（DSR、期待最大シャープ、モンテカルロ、配点）、events.py（FOMC の解析、ローンチの関門）。
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from conftest import random_walk
from hedgedesk import config as C, data as D, events, metrics as M
from hedgedesk.research import _windows
from hedgedesk.strategies import STRATEGIES, Context, earnings_block, prepare, trial_id


def synthetic_context(end=None):
    """全銘柄の合成データ（わずかに上昇するランダムウォーク）・VIX・決算日で Context を作る。

    Args:
        end: 指定すると、その日より後のデータを消す（先読みの検査で「未来を消した」データを作るため）。

    Returns:
        strategies の Context。売買代金は全日5億ドルにして、流動性の関門で落ちないようにしている。
    """
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
    # 全戦略について、700本目までに切ったデータと全データで、700本目までのシグナルが完全に一致することを確かめる。
    # 一致しなければ、どこかで未来のデータを参照している（先読み）。
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
    """どの戦略でも、新規シグナルはスコア7以上で、ストップは指値より下にある。"""
    ctx = synthetic_context()
    for s, sig in st.signals(ctx, st.params()[0]).items():
        hits = sig[sig["entry"] > 0]
        assert (hits["score"] >= C.MIN_SETUP_SCORE).all()
        assert (hits["stop"] < hits["limit"]).all()


def test_earnings_block_before_report_and_unknown_dates():
    """決算前の11本は新規禁止、遠い日は許可、反応日当日は許可、最後の決算以降（次が不明）は禁止、データなしは全日禁止。"""
    idx = pd.bdate_range("2024-01-01", periods=60)
    reactions = pd.Series([idx[5], idx[40]])
    b = earnings_block(idx, reactions, horizon=11)
    assert b.iloc[29:40].all()     # 決算前の11本
    assert not b.iloc[20]          # 決算まで遠い日
    assert not b.iloc[40]          # 決算当日の反応後は決算ドリフトで入れる
    assert b.iloc[41:].all()       # 次の決算日が不明
    assert earnings_block(idx, None).all()


def test_drop_incomplete_bars():
    """株は NY 16:15 前の当日足を落とし、暗号資産は UTC の当日足を落とす。"""
    idx = pd.to_datetime(["2026-11-05", "2026-11-06"])
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    during = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)  # NY 13:00
    after = datetime(2026, 11, 6, 22, 0, tzinfo=timezone.utc)   # NY 17:00
    assert len(D.drop_incomplete(df, "SPY", during)) == 1
    assert len(D.drop_incomplete(df, "SPY", after)) == 2
    assert len(D.drop_incomplete(df, "BTC-USD", after)) == 1   # UTCの当日足は未確定
    assert len(D.drop_incomplete(df, "BTC-USD", datetime(2026, 11, 7, 0, 10, tzinfo=timezone.utc))) == 2


def test_quality_issues_detect_bad_data():
    """高値が始値より低いような矛盾と、空のデータを不備として検出する。"""
    df = pd.DataFrame({"open": [1.0], "high": [0.5], "low": [0.4], "close": [0.45]}, index=pd.to_datetime(["2024-01-01"]))
    assert D.quality_issues(df)
    assert D.quality_issues(pd.DataFrame()) == ["no data"]


def test_walk_forward_windows_never_overlap_train_and_test():
    """ウォークフォワードは8区間（2021〜2024年の半年ごと）で、学習と検証が重ならず、ホールドアウトに入らない。"""
    w = _windows()
    assert len(w) == 8
    for train, test, end in w:
        assert train < test <= end
        assert test - train >= pd.Timedelta(days=365 * 3)
        assert end <= pd.Timestamp(C.IS_END) < pd.Timestamp(C.HOLDOUT_START)


def test_dsr_falls_as_trials_grow():
    """同じリターンでも、試行回数が多いほどデフレートシャープは下がる。"""
    rng = np.random.default_rng(0)
    r = pd.Series(rng.normal(0.001, 0.01, 750))
    trial_srs = list(rng.normal(0, 0.03, 500))
    few = M.deflated_sharpe(r, trial_srs, 10)
    many = M.deflated_sharpe(r, trial_srs, 500)
    assert 0 <= many < few <= 1


def test_expected_max_sharpe_known_shape():
    """試行1回なら期待最大シャープは0、試行が増えるほど大きくなる。"""
    assert M.expected_max_sharpe([0.1, -0.1], 1) == 0.0
    a = M.expected_max_sharpe([0.1, -0.1, 0.05, -0.05], 10)
    b = M.expected_max_sharpe([0.1, -0.1, 0.05, -0.05], 1000)
    assert 0 < a < b


def test_monte_carlo_reproducible():
    """同じシードなら同じ結果。中央値 ≦ 95パーセンタイル ≦ 最悪値。トレードがなければ None。"""
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
    """配点の境界の値（1.2、0.95、0.10 など）が高い点の側に入ることを含め、合計点を確かめる。"""
    assert M.score_card(*args)["total"] == expected


def test_trial_id_stable_and_unique():
    """試行 ID はパラメータの並び順に依存せず、値が違えば別の ID になる。"""
    assert trial_id("A", {"x": 1, "y": 2}) == trial_id("A", {"y": 2, "x": 1})
    assert trial_id("A", {"x": 1}) != trial_id("A", {"x": 2})


def test_all_grids_stay_within_swing_and_register_trials():
    """全戦略の試行数が50を超え、どのパラメータも最大保有本数（10本）以内である。"""
    total = sum(len(s.params()) for s in STRATEGIES)
    assert total > 50
    for st in STRATEGIES:
        for p in st.params():
            assert p.get("max_hold", p.get("hold", 10)) <= C.MAX_HOLD_BARS


def test_no_banned_symbol_in_universe():
    """銘柄ユニバースに禁止銘柄（レバレッジ型・インバース型）が含まれていない。"""
    assert not (set(C.ETFS + C.STOCKS + C.CRYPTO) & C.BANNED)


def test_parse_fomc():
    """FRB のページ形式から各回の最終日を取り出す（"Apr/May" は後ろの月、"15-16*" の注記は無視）。"""
    html = """<h4>2026 FOMC Meetings</h4>
    <div class="fomc-meeting__month"><strong>January</strong></div><div class="fomc-meeting__date">27-28</div>
    <div class="fomc-meeting__month"><strong>Apr/May</strong></div><div class="fomc-meeting__date">28-29</div>
    <div class="fomc-meeting__month"><strong>September</strong></div><div class="fomc-meeting__date">15-16*</div>
    <h4>2027 FOMC Meetings</h4>
    <div class="fomc-meeting__month"><strong>March</strong></div><div class="fomc-meeting__date">16-17</div>"""
    days = [d.isoformat() for d in events.parse_fomc(html)]
    assert days == ["2026-01-28", "2026-05-29", "2026-09-16", "2027-03-17"]


def test_launch_blocked_when_dates_unknown(monkeypatch):
    """FOMC の取得に失敗し、CPI の手動登録もなければ、両方を理由にローンチを止める。"""
    def boom():
        raise OSError
    monkeypatch.setattr(events, "fetch_fomc", boom)
    monkeypatch.setattr(events, "MANUAL", C.STATE / "none.json")
    ok, reasons = events.launch_gate(datetime(2026, 11, 7, tzinfo=timezone.utc))
    assert not ok
    assert any("CPI" in r for r in reasons) and any("FOMC" in r for r in reasons)


def test_new_hypotheses_match_their_rules():
    """S7（押し目）の新規は安値の連続切り下げ後だけ、S8（値幅縮小）の新規は直近で最小の値幅の日だけに出る。"""
    ctx = synthetic_context()
    by_id = {s.id: s for s in STRATEGIES}
    st = by_id["S7_PULLBACK_ETF"]
    p = st.params()[0]
    hits = 0
    for s, sig in st.signals(ctx, p).items():
        low = ctx.market[s]["low"]
        for d in sig.index[sig["entry"] > 0]:
            i = low.index.get_loc(d)
            assert all(low.iloc[i - k] < low.iloc[i - k - 1] for k in range(p["n_down"]))
            hits += 1
    st = by_id["S8_NR_ETF"]
    p = st.params()[0]
    for s, sig in st.signals(ctx, p).items():
        df = ctx.market[s]
        rng = df["high"] - df["low"]
        for d in sig.index[sig["entry"] > 0]:
            i = rng.index.get_loc(d)
            assert rng.iloc[i] <= rng.iloc[i - p["nr"] + 1:i + 1].min()
            hits += 1
    assert hits > 0
