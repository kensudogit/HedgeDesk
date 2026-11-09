"""desk.market_snapshot の既定動作（キャッシュ優先）を検査する。"""
import pandas as pd

from hedgedesk import config as C
from hedgedesk import desk
from hedgedesk.strategies import Context


def _mini_ctx() -> Context:
    idx = pd.date_range("2026-11-01", periods=3, freq="D")
    df = pd.DataFrame(
        {
            "close": [100.0, 101.0, 102.0],
            "sma200": [95.0, 95.5, 96.0],
            "volume": [1_000_000.0, 1_100_000.0, 1_200_000.0],
            "vol_avg20": [900_000.0, 900_000.0, 900_000.0],
            "score": [7.0, 7.0, 7.0],
        },
        index=idx,
    )
    market = {s: df.copy() for s in C.INDEX_ETFS[:1]}
    vix = pd.Series([20.0, 21.0, 22.0], index=idx)
    return Context(market=market, vix=vix, earnings={})


def test_market_snapshot_defaults_to_cache(monkeypatch):
    """既定では build_context(refresh=False) を呼ぶ（朝のブリーフで全取得しない）。"""
    seen: list[bool] = []

    def fake_build(refresh=False, now=None):
        seen.append(refresh)
        return _mini_ctx(), {"source": "test-cache"}

    monkeypatch.setattr("hedgedesk.research.build_context", fake_build)
    out = desk.market_snapshot()
    assert seen == [False]
    assert out["source"] == "test-cache"
    assert "SPY" in out["rows"]


def test_market_snapshot_honors_refresh_flag(monkeypatch):
    seen: list[bool] = []

    def fake_build(refresh=False, now=None):
        seen.append(refresh)
        return _mini_ctx(), {"source": "test"}

    monkeypatch.setattr("hedgedesk.research.build_context", fake_build)
    desk.market_snapshot(refresh=True)
    assert seen == [True]
