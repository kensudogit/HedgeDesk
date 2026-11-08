"""シミュレータが楽観的にならないことを確かめる。"""
#
# 【対象】hedgedesk/backtest.py の simulate。約定の条件、コスト、リスク上限、キルスイッチ、遅延の再現を検査する。
# 【方針】手で作った数本の日足で、約定するか・いくらで約定するかを1つずつ確かめる。
#   併せて乱数データで「コスト後に優位性が出ない」「資産と損益が一致する」など全体の整合も確かめる。
import numpy as np
import pandas as pd

from conftest import make_bars, make_signals, random_walk
from hedgedesk import config as C
from hedgedesk.backtest import Costs, simulate

# FREE: コストなし（約定価格を正確に確かめるため）。FLAT: 100 前後で動かない足 [始値, 高値, 安値, 終値]。
FREE = Costs(0, 0, 0)
FLAT = [100, 101, 99, 100]


def run(rows, entries, limit=100.0, stop=90.0, exits=(), costs=FREE, lag=0, max_hold=10):
    """1銘柄 "X" の日足 rows とシグナルで simulate を実行する短縮形。期間は日足の先頭から最後まで。"""
    df = make_bars(rows)
    sig = make_signals(df, entries, exits, limit, stop, max_hold)
    return simulate({"X": df}, {"X": sig}, "2024-01-01", None, costs, lag=lag)


def test_limit_only_touched_is_not_filled():
    """安値が指値にちょうど触れただけ（100 = 100）では約定しない。"""
    res = run([FLAT, [101, 102, 100, 101], FLAT], [0], limit=100.0)
    assert len(res.trades) == 0


def test_limit_fills_only_after_trading_through():
    """安値が指値を下回れば約定し、価格は指値（始値が指値より上なので）。"""
    res = run([FLAT, [101, 102, 99.5, 101], FLAT, FLAT], [0], limit=100.0)
    assert len(res.trades) == 1
    assert res.trades.iloc[0]["entry"] == 100.0


def test_order_expires_after_one_day():
    """指値の有効期限は1日。翌日に約定しなければ、その後に下がっても約定しない。"""
    res = run([FLAT, [105, 106, 104, 105], [100, 100, 80, 95], [95, 96, 94, 95]], [0], limit=100.0, stop=70.0)
    assert len(res.trades) == 0


def test_signal_cannot_trade_its_own_bar():
    """シグナルが出た足の中では約定しない（先読み防止）。"""
    res = run([[100, 100, 50, 100], [150, 151, 149, 150], [150, 151, 149, 150]], [0], limit=100.0, stop=50.0)
    assert len(res.trades) == 0


def test_gap_through_stop_fills_at_open_not_at_stop():
    """窓開けでストップより下から始まったら、ストップ価格ではなく（より悪い）始値で決済される。"""
    rows = [FLAT, FLAT, FLAT, [70, 72, 68, 70], [70, 71, 69, 70]]
    res = run(rows, [0], limit=100.0, stop=90.0)
    t = res.trades.iloc[0]
    assert t["reason"] == "stop_gap"
    assert t["exit"] == 70.0
    assert t["r"] < -1  # 窓開けではストップ幅より大きく負ける


def test_same_bar_limit_and_stop_is_a_loss():
    """約定した足でストップにも届いたら、損失として扱う（日足では順序が分からないので楽観しない）。"""
    rows = [FLAT, [100, 120, 85, 110], [110, 111, 109, 110]]
    res = run(rows, [0], limit=100.0, stop=90.0)
    t = res.trades.iloc[0]
    assert t["reason"] == "stop_same_bar"
    assert t["pnl"] < 0


def test_max_hold_exits_next_open():
    """最大保有本数に達したら時間切れで手仕舞う。"""
    rows = [FLAT] * 10
    res = run(rows, [0], limit=101.0, stop=90.0, max_hold=3)
    t = res.trades.iloc[0]
    assert t["reason"] == "time"
    assert t["bars"] == 3


def test_min_hold_delays_signal_exit():
    """手仕舞いシグナルが毎日出ていても、最低保有本数までは持ち続ける。"""
    rows = [FLAT] * 8
    res = run(rows, [0], limit=101.0, stop=90.0, exits=[1, 2, 3, 4, 5, 6, 7])
    assert res.trades.iloc[0]["bars"] >= C.MIN_HOLD_BARS


def test_every_trade_risk_within_limit_and_notional_cap():
    """乱数データで、全トレードのリスク額と時価がそれぞれの上限内に収まる。"""
    # リスク額の上限に3倍の余裕を持たせているのは、資産が増えると1トレードのリスク額も増えるため（開始資金基準の比較）。
    # 時価の上限の 1.01 倍は、スリッページで約定価格がわずかに上がる分の許容。
    df = random_walk(1)
    limit = df["close"]
    stop = df["close"] * 0.97
    sig = make_signals(df, list(range(250, 850, 7)), [], limit, stop)
    res = simulate({"X": df}, {"X": sig}, "2018-01-01", None, Costs())
    assert len(res.trades) > 20
    assert (res.trades["risk"] <= C.CAPITAL * 3 * C.RISK_PER_TRADE).all()
    assert ((res.trades["qty"] * res.trades["entry"]) <= res.equity.max() * C.MAX_POSITION_NOTIONAL * 1.01).all()


def test_open_risk_cap_limits_concurrent_positions():
    """同じ日に12銘柄でシグナルが出ても、合計リスクの上限で同時保有は5本までになる。"""
    market, signals = {}, {}
    for k in range(12):
        df = make_bars([[100, 101, 99, 100]] * 8)
        market[f"S{k}"] = df
        signals[f"S{k}"] = make_signals(df, [0], [], 101.0, 95.0)
    res = simulate(market, signals, "2024-01-01", None, FREE)
    assert len(res.trades) == 5  # 0.5% × 5 = 2.5% で頭打ち


def test_costs_reduce_results():
    """コストを入れると最終資産が減る（コストが計算に効いていることの確認）。"""
    df = random_walk(2)
    sig = make_signals(df, list(range(250, 850, 5)), [], df["close"] * 1.002, df["close"] * 0.97, max_hold=5)
    cheap = simulate({"X": df}, {"X": sig}, "2018-01-01", None, FREE, apply_kill=False)
    dear = simulate({"X": df}, {"X": sig}, "2018-01-01", None, Costs(10, 15, 25), apply_kill=False)
    assert dear.equity.iloc[-1] < cheap.equity.iloc[-1]


def test_random_entries_have_no_edge_after_costs():
    """乱数の売買で利益が出るなら、シミュレータに不具合がある。"""
    expect = []
    for seed in range(20):
        df = random_walk(seed + 100)
        rng = np.random.default_rng(seed)
        entries = sorted(rng.choice(np.arange(250, 880), size=60, replace=False).tolist())
        atr = df["close"] * 0.02
        sig = make_signals(df, entries, [], df["close"] + 0.25 * atr, df["close"] - 2 * atr, max_hold=5)
        res = simulate({"X": df}, {"X": sig}, "2018-01-01", None, Costs(), apply_kill=False)
        expect.append(res.trades["r"].mean())
    assert np.mean(expect) < 0.03


def test_kill_switch_pauses_new_orders():
    """3連敗でキルスイッチが作動し、その後しばらく新規が止まるので、作動なしよりトレード数が減る。"""
    # 毎回ストップにかかる下落が3回続く
    rows = []
    for _ in range(6):
        rows += [[100, 101, 99, 100], [100, 101, 80, 100]]
    rows += [[100, 101, 99, 100]] * 12
    df = make_bars(rows)
    sig = make_signals(df, list(range(0, 24, 2)), [], 100.0, 95.0)
    res = simulate({"X": df}, {"X": sig}, "2024-01-01", None, FREE)
    assert any(k["reason"] == "consecutive_losses" for k in res.kills)
    no_kill = simulate({"X": df}, {"X": sig}, "2024-01-01", None, FREE, apply_kill=False)
    assert len(res.trades) < len(no_kill.trades)


def test_equity_reconciles_with_trades():
    """最終資産は、開始資金と全トレード損益の合計に一致する。"""
    df = random_walk(7)
    sig = make_signals(df, list(range(250, 850, 9)), [], df["close"] * 1.002, df["close"] * 0.97, max_hold=6)
    res = simulate({"X": df}, {"X": sig}, "2018-01-01", None, Costs(5, 15, 25))
    assert abs(res.equity.iloc[-1] - (C.CAPITAL + res.trades["pnl"].sum())) < 1e-6


def test_lag_delays_order_by_one_bar():
    """1本遅れの設定では、注文が1日遅れて出る（通常では約定しない並びでも、遅れた日に約定する）。"""
    rows = [FLAT, [105, 106, 104, 105], [101, 102, 99.5, 100], FLAT]
    on_time = run(rows, [0], limit=100.0)
    late = run(rows, [0], limit=100.0, lag=1)
    assert len(on_time.trades) == 0
    assert len(late.trades) == 1
    assert isinstance(late.trades.iloc[0]["entry_date"], pd.Timestamp)


def test_late_entry_pays_the_open_and_gets_no_limit_discount():
    """遅れた注文は押し目を待たず、遅れた足の始値で買う。"""
    rows = [FLAT, [105, 106, 104, 105], [120, 125, 119, 121], [121, 122, 120, 121]]
    late = run(rows, [0], limit=100.0, stop=90.0, lag=1)
    assert late.trades.iloc[0]["entry"] == 120.0
    assert late.trades.iloc[0]["entry_date"] == pd.Timestamp(make_bars(rows).index[2])
