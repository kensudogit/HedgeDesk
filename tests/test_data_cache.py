"""データ読み込みの再発防止テスト。

2026-11-07 の不具合: 価格の保存先が1つのファイルに上書きされ、VIXの中身がXOMの株価になっていた。
"""
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

NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)
LEVELS = {"AAA": 100.0, "ZZZ": 400.0, "^VIX": 20.0}


def _raw(symbols):
    idx = pd.bdate_range("2024-01-01", periods=30)
    frames = {}
    for s in symbols:
        p = LEVELS[s]
        frames[s] = pd.DataFrame({"Open": p, "High": p + 1, "Low": p - 1, "Close": p, "Adj Close": p, "Volume": 1000.0}, index=idx)
    return pd.concat(frames, axis=1)


@pytest.fixture
def fake_yahoo(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "CACHE", tmp_path / "cache")
    calls = []

    def download(symbols, **kw):
        calls.append(list(symbols))
        return _raw(symbols)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    return calls


def test_each_symbol_is_cached_in_its_own_file(fake_yahoo):
    D.load(["AAA", "ZZZ", "^VIX"], now=NOW)
    for s, level in LEVELS.items():
        assert pd.read_pickle(D._cache_path(s, "20261107"))["close"].iloc[-1] == level


def test_second_load_uses_cache_and_returns_identical_data(fake_yahoo):
    a, log_a = D.load(["AAA", "ZZZ", "^VIX"], now=NOW)
    b, log_b = D.load(["AAA", "ZZZ", "^VIX"], now=NOW)
    assert len(fake_yahoo) == 1
    assert log_a["fingerprint"] == log_b["fingerprint"]
    assert a["^VIX"]["close"].iloc[-1] == 20.0 and b["^VIX"]["close"].iloc[-1] == 20.0


def test_partial_cache_does_not_overwrite_other_symbols(fake_yahoo):
    D.load(["^VIX"], now=NOW)
    D.load(["AAA", "ZZZ", "^VIX"], now=NOW)
    assert fake_yahoo[-1] == ["AAA", "ZZZ"]
    assert D.load(["^VIX"], now=NOW)[0]["^VIX"]["close"].iloc[-1] == 20.0


def test_vix_check_rejects_a_stock_price():
    good = pd.DataFrame({"close": [15.0, 20.0, 30.0]})
    D.check_vix(good)
    with pytest.raises(ValueError):
        D.check_vix(pd.DataFrame({"close": [120.0, 150.0, 165.0]}))


def test_fingerprint_detects_a_changed_price():
    df = random_walk(1)[["open", "high", "low", "close", "volume"]]
    other = df.copy()
    other.iloc[5, 3] += 0.01
    assert D.fingerprint({"X": df}) == D.fingerprint({"X": df.copy()})
    assert D.fingerprint({"X": df}) != D.fingerprint({"X": other})


def _prices(n=100):
    return random_walk(5, n=n)[["open", "high", "low", "close", "volume", "dollar_volume"]]


def test_tiny_jitter_keeps_previous_history():
    old = _prices()
    new = old.copy()
    new[["open", "high", "low", "close"]] *= 1 + 8e-7
    out, changed = D.stabilize(old, new)
    assert not changed
    assert out.iloc[:-5]["close"].equals(old.iloc[:-5]["close"])
    assert out.iloc[-5:]["close"].equals(new.iloc[-5:]["close"])
    assert D.fingerprint({"X": out.iloc[:-5]}) == D.fingerprint({"X": old.iloc[:-5]})


def test_dividend_adjustment_replaces_history():
    old = _prices()
    new = old.copy()
    new[["open", "high", "low", "close"]] *= 0.995
    out, changed = D.stabilize(old, new)
    assert changed and out["close"].equals(new["close"])


def test_correction_of_one_old_bar_is_accepted():
    old = _prices()
    new = old.copy()
    new.iloc[10, new.columns.get_loc("close")] *= 1.01
    out, changed = D.stabilize(old, new)
    assert changed and out["close"].iloc[10] == new["close"].iloc[10]


def test_new_bars_are_appended_and_recent_bars_refreshed():
    full = _prices(105)
    old, new = full.iloc[:100], full.copy()
    new.iloc[97, new.columns.get_loc("close")] *= 1.02  # 直近の足の訂正
    out, changed = D.stabilize(old, new)
    assert not changed and len(out) == 105
    assert out["close"].iloc[97] == new["close"].iloc[97]


def test_missing_history_replaces_snapshot():
    old = _prices()
    out, changed = D.stabilize(old, old.iloc[30:])
    assert changed and len(out) == 70


def test_refresh_with_jitter_gives_same_fingerprint(fake_yahoo, monkeypatch):
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


def test_evaluation_is_deterministic():
    from test_signals_and_metrics import synthetic_context

    st = next(s for s in STRATEGIES if s.id == "S6_VOL")
    ctx = synthetic_context()
    a, b = evaluate(st, ctx), evaluate(st, ctx)
    ra, rb = a.pop("_oos_returns", None), b.pop("_oos_returns", None)
    assert a == b
    if ra is not None:
        assert np.array_equal(ra.to_numpy(), rb.to_numpy())
