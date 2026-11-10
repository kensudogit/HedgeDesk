"""データ読み込みの再発防止テスト。

2026-11-07 の不具合: 価格の保存先が1つのファイルに上書きされ、VIXの中身がXOMの株価になっていた。
"""
#
# 【対象】hedgedesk/data.py（キャッシュ、取得ごとの揺れの吸収 stabilize、VIX の検査、指紋）と research の中止条件・再現性。
# 【方針】yfinance は sys.modules に偽物を差し込み、ネットワークに出ない。銘柄ごとに価格水準を変え（LEVELS）、
#   取り違えがあればすぐ分かるようにしている。
import sys
import types
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from conftest import random_walk
from hedgedesk import data as D
from hedgedesk.research import evaluate
from hedgedesk.strategies import STRATEGIES

# NOW: キャッシュの日付スタンプ（20261107）を決める基準時刻。LEVELS: 銘柄ごとの一定の価格（取り違えの検出用）。
NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)
LEVELS = {"AAA": 100.0, "ZZZ": 400.0, "^VIX": 20.0}


def _raw(symbols):
    """yfinance.download の戻り値を模した、(銘柄, 項目) の MultiIndex 列を持つ30営業日分のデータを作る。"""
    idx = pd.bdate_range("2024-01-01", periods=30)
    frames = {}
    for s in symbols:
        p = LEVELS[s]
        frames[s] = pd.DataFrame({"Open": p, "High": p + 1, "Low": p - 1, "Close": p, "Adj Close": p, "Volume": 1000.0}, index=idx)
    return pd.concat(frames, axis=1)


@pytest.fixture
def fake_yahoo(tmp_path, monkeypatch):
    """data.CACHE を一時ディレクトリに向け、yfinance を偽物に差し替える。

    Returns:
        download が呼ばれるたびに、要求された銘柄のリストを追加していくリスト（取得回数と対象の確認用）。
    """
    monkeypatch.setattr(D, "CACHE", tmp_path / "cache")
    calls = []

    def download(symbols, **kw):
        calls.append(list(symbols))
        return _raw(symbols)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    return calls


def test_each_symbol_is_cached_in_its_own_file(fake_yahoo):
    """銘柄ごとに別のファイルに保存され、中身がその銘柄の価格になっている（VIX 取り違えの再発防止）。"""
    D.load(["AAA", "ZZZ", "^VIX"], now=NOW)
    for s, level in LEVELS.items():
        assert pd.read_pickle(D._cache_path(s, "20261107"))["close"].iloc[-1] == level


def test_second_load_uses_cache_and_returns_identical_data(fake_yahoo):
    """同じ日の2回目はキャッシュを使い（取得は1回だけ）、同じ指紋のデータを返す。"""
    a, log_a = D.load(["AAA", "ZZZ", "^VIX"], now=NOW)
    b, log_b = D.load(["AAA", "ZZZ", "^VIX"], now=NOW)
    assert len(fake_yahoo) == 1
    assert log_a["fingerprint"] == log_b["fingerprint"]
    assert a["^VIX"]["close"].iloc[-1] == 20.0 and b["^VIX"]["close"].iloc[-1] == 20.0


def test_partial_cache_does_not_overwrite_other_symbols(fake_yahoo):
    """キャッシュのある銘柄は取り直さず、ない銘柄だけを取得しても、既存のキャッシュは上書きされない。"""
    D.load(["^VIX"], now=NOW)
    D.load(["AAA", "ZZZ", "^VIX"], now=NOW)
    assert fake_yahoo[-1] == ["AAA", "ZZZ"]
    assert D.load(["^VIX"], now=NOW)[0]["^VIX"]["close"].iloc[-1] == 20.0


def test_vix_check_rejects_a_stock_price():
    """VIX らしい水準は通し、株価のような水準（中央値150）は ValueError で止める。"""
    good = pd.DataFrame({"close": [15.0, 20.0, 30.0]})
    D.check_vix(good)
    with pytest.raises(ValueError):
        D.check_vix(pd.DataFrame({"close": [120.0, 150.0, 165.0]}))


def test_fingerprint_detects_a_changed_price():
    """価格が1か所でも変われば指紋が変わり、同じデータなら同じ指紋になる。"""
    df = random_walk(1)[["open", "high", "low", "close", "volume"]]
    other = df.copy()
    other.iloc[5, 3] += 0.01
    assert D.fingerprint({"X": df}) == D.fingerprint({"X": df.copy()})
    assert D.fingerprint({"X": df}) != D.fingerprint({"X": other})


def _prices(n=100):
    """stabilize のテスト用の日足（data._adjust 後と同じ列）。"""
    return random_walk(5, n=n)[["open", "high", "low", "close", "volume", "dollar_volume"]]


def test_tiny_jitter_keeps_previous_history():
    """百万分の1未満の揺れなら、過去の足は前回の値を使い続け、直近5本だけ新しい値になる。"""
    old = _prices()
    new = old.copy()
    new[["open", "high", "low", "close"]] *= 1 + 8e-7
    out, changed = D.stabilize(old, new)
    assert not changed
    assert out.iloc[:-5]["close"].equals(old.iloc[:-5]["close"])
    assert out.iloc[-5:]["close"].equals(new.iloc[-5:]["close"])
    assert D.fingerprint({"X": out.iloc[:-5]}) == D.fingerprint({"X": old.iloc[:-5]})


def test_dividend_adjustment_replaces_history():
    """配当の調整（0.5%）のような本当の変化なら、過去も含めて新しい値にすべて置き換える。"""
    old = _prices()
    new = old.copy()
    new[["open", "high", "low", "close"]] *= 0.995
    out, changed = D.stabilize(old, new)
    assert changed and out["close"].equals(new["close"])


def test_correction_of_one_old_bar_is_accepted():
    """過去の1本だけの訂正（1%）も本当の変化として受け入れる。"""
    old = _prices()
    new = old.copy()
    new.iloc[10, new.columns.get_loc("close")] *= 1.01
    out, changed = D.stabilize(old, new)
    assert changed and out["close"].iloc[10] == new["close"].iloc[10]


def test_new_bars_are_appended_and_recent_bars_refreshed():
    """新しい足は追加され、直近の足の訂正は「過去の変化」とみなさずに反映される。"""
    full = _prices(105)
    old, new = full.iloc[:100], full.copy()
    new.iloc[97, new.columns.get_loc("close")] *= 1.02  # 直近の足の訂正
    out, changed = D.stabilize(old, new)
    assert not changed and len(out) == 105
    assert out["close"].iloc[97] == new["close"].iloc[97]


def test_missing_history_replaces_snapshot():
    """今回の取得で過去の足が欠けていたら（取得範囲の変化）、比較せず新しいデータをそのまま使う。"""
    old = _prices()
    out, changed = D.stabilize(old, old.iloc[30:])
    assert changed and len(out) == 70


def test_refresh_with_jitter_gives_same_fingerprint(fake_yahoo, monkeypatch):
    """取り直したデータに微小な揺れがあっても、過去の値が変わらず、history_changed も False になる。"""
    a, log_a = D.load(["AAA", "^VIX"], now=NOW)
    import yfinance

    def jittered(symbols, **kw):
        raw = _raw(symbols)
        raw.loc[:, (slice(None), "Adj Close")] *= 1 + 8e-7
        return raw

    monkeypatch.setattr(yfinance, "download", jittered)
    b, log_b = D.load(["AAA", "^VIX"], refresh=True, now=NOW)
    assert log_b["history_changed"] == {"AAA": False, "^VIX": False}
    assert a["AAA"].iloc[:-5]["close"].equals(b["AAA"].iloc[:-5]["close"])


def test_html_parser_for_earnings_is_installed():
    """lxml がないと決算日の取得が全銘柄で失敗する。コンテナで実際に起きた。"""
    import lxml.etree  # noqa: F401


def test_research_stops_when_earnings_are_mostly_missing():
    """決算データの取得失敗が少数なら続行し、ほぼ全銘柄なら RuntimeError で調査を中止する。"""
    from hedgedesk import config as C
    from hedgedesk.research import check_earnings_coverage

    check_earnings_coverage([])
    check_earnings_coverage(C.STOCKS[:2])
    with pytest.raises(RuntimeError):
        check_earnings_coverage(C.STOCKS)


def test_evaluation_is_deterministic():
    """同じデータで2回検証すると、結果（OOS リターンを含む）が完全に一致する（乱数や順序に依存しない）。"""
    from test_signals_and_metrics import synthetic_context

    st = next(s for s in STRATEGIES if s.id == "S6_VOL")
    ctx = synthetic_context()
    a, b = evaluate(st, ctx), evaluate(st, ctx)
    ra, rb = a.pop("_oos_returns", None), b.pop("_oos_returns", None)
    assert a == b
    if ra is not None:
        assert np.array_equal(ra.to_numpy(), rb.to_numpy())


# ---------------------------------------------------------------------------
# 直近の足の補充（top_up）
# ---------------------------------------------------------------------------

def test_expected_last_session_for_stocks_and_crypto():
    """株は NY 16:15 を過ぎた平日ならその日、前なら前の平日。暗号資産は UTC の前日。"""
    tue_jst_9am = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)      # NY 月曜 19:00
    assert str(D.expected_last_session("SPY", tue_jst_9am)) == "2026-11-09"
    mon_ny_10am = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)       # NY 月曜 10:00 → 金曜
    assert str(D.expected_last_session("SPY", mon_ny_10am)) == "2026-11-06"
    sat = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)
    assert str(D.expected_last_session("SPY", sat)) == "2026-11-06"
    assert str(D.expected_last_session("BTC-USD", tue_jst_9am)) == "2026-11-09"


def _bars(end, level, n=30):
    idx = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame({"Open": level, "High": level + 1, "Low": level - 1, "Close": level, "Adj Close": level, "Volume": 1000.0}, index=idx)


def test_top_up_appends_missing_session_and_throttles(tmp_path, monkeypatch):
    """欠けている銘柄だけ直近を取得して継ぎ足し、1時間以内は取り直さない。新しい銘柄はそのまま。"""
    monkeypatch.setattr(D, "CACHE", tmp_path / "cache")
    D.CACHE.mkdir()
    now = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
    old = D._adjust(_bars("2026-11-06", 100.0, 300))
    D._save(old, D._latest_path("AAA"))
    D._save(old, D._cache_path("AAA", "20261110"))
    D._save(D._adjust(_bars("2026-11-09", 400.0)), D._latest_path("ZZZ"))
    calls = []

    def download(symbols, **kw):
        calls.append((list(symbols), kw.get("period")))
        return pd.concat({s: _bars("2026-11-09", 101.0 if s == "AAA" else 9.0, 20) for s in symbols}, axis=1)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    out = D.top_up(["AAA", "ZZZ"], now)
    assert calls == [(["AAA"], D.RECENT_PERIOD)]
    assert out == {"refreshed": ["AAA"], "still_behind": [], "skipped": []}
    for path in (D._latest_path("AAA"), D._cache_path("AAA", "20261110")):
        df = pd.read_pickle(path)
        assert str(df.index[-1].date()) == "2026-11-09" and df["close"].iloc[-1] == 101.0
        assert len(df) > 300 and df.index.is_unique and df["close"].iloc[0] == 100.0   # 古い履歴は残る
    assert pd.read_pickle(D._latest_path("ZZZ"))["close"].iloc[-1] == 400.0
    assert D.top_up(["AAA"], now)["refreshed"] == [] and len(calls) == 1


def test_top_up_records_holiday_as_still_behind(tmp_path, monkeypatch):
    """取得しても足が増えない（休場・取得遅れ）なら still_behind に残し、1時間は取り直さない。取得の失敗でも例外を出さない。"""
    monkeypatch.setattr(D, "CACHE", tmp_path / "cache")
    D.CACHE.mkdir()
    now = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
    D._save(D._adjust(_bars("2026-11-06", 100.0)), D._latest_path("AAA"))

    def boom(symbols, **kw):
        raise OSError("down")

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=boom))
    assert D.top_up(["AAA"], now) == {"refreshed": [], "still_behind": ["AAA"], "skipped": []}
    assert D.top_up(["AAA"], now)["skipped"] == ["AAA"]


def test_bar_saved_mid_session_is_refreshed(tmp_path, monkeypatch):
    """取引中に保存された足（例: 12:00 UTC に保存した暗号資産の当日足）は、日付が揃っていても取り直す。"""
    import os

    monkeypatch.setattr(D, "CACHE", tmp_path / "cache")
    D.CACHE.mkdir()
    now = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)
    idx = pd.date_range(end="2026-11-09", periods=30, freq="D")
    partial = D._adjust(pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.0, "Adj Close": 1.0, "Volume": 1.0}, index=idx))
    D._save(partial, D._latest_path("BTC-USD"))
    noon = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc).timestamp()
    os.utime(D._latest_path("BTC-USD"), (noon, noon))
    assert D.is_behind(partial, "BTC-USD", now, datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)) is True
    assert D.is_behind(partial, "BTC-USD", now, datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)) is False

    def download(symbols, **kw):
        return pd.concat({"BTC-USD": pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Adj Close": 1.5, "Volume": 1.0},
                                                   index=pd.date_range(end="2026-11-10", periods=10, freq="D"))}, axis=1)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    assert D.top_up(["BTC-USD"], now)["refreshed"] == ["BTC-USD"]
    df = D.drop_incomplete(pd.read_pickle(D._latest_path("BTC-USD")), "BTC-USD", now)
    assert str(df.index[-1].date()) == "2026-11-09" and df["close"].iloc[-1] == 1.5   # 途中の値が確定値に置き換わる
