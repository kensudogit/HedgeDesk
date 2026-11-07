import numpy as np
import pandas as pd
import pytest

from hedgedesk import config as C, journal, risk, trader


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """テストが本物のジャーナルや状態ファイルに触れないようにする。"""
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "journal.jsonl")
    monkeypatch.setattr(risk, "KILL_FILE", tmp_path / "kill.json")
    monkeypatch.setattr(trader, "POSITIONS", tmp_path / "positions.json")
    monkeypatch.setattr(trader, "FAILS", tmp_path / "fails.json")
    monkeypatch.setattr(trader, "ENV_FILE", tmp_path / "no.env")
    monkeypatch.setattr(C, "STATE", tmp_path)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("APCA_API_BASE_URL", raising=False)


def make_bars(rows, start="2024-01-01"):
    """(open, high, low, close) の並びから日足を作る。"""
    idx = pd.bdate_range(start, periods=len(rows))
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    df["volume"] = 1_000_000.0
    df["dollar_volume"] = df["close"] * df["volume"]
    return df


def make_signals(df, entries=(), exits=(), limit=None, stop=None, max_hold=10):
    sig = pd.DataFrame(index=df.index)
    sig["entry"] = 0.0
    sig["exit"] = 0.0
    sig["limit"] = limit
    sig["stop"] = stop
    sig["rank"] = 0.0
    for i in entries:
        sig.iloc[i, sig.columns.get_loc("entry")] = 1.0
    for i in exits:
        sig.iloc[i, sig.columns.get_loc("exit")] = 1.0
    sig.attrs["max_hold"] = max_hold
    return sig


def random_walk(seed, n=900, start_price=100.0, drift=0.0, vol=0.012):
    rng = np.random.default_rng(seed)
    close = start_price * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    opn = np.concatenate(([start_price], close[:-1])) * np.exp(rng.normal(0, vol / 3, n))
    high = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    idx = pd.bdate_range("2018-01-01", periods=n)
    volume = rng.lognormal(15, 0.5, n)
    return pd.DataFrame({"open": opn, "high": high, "low": low, "close": close, "volume": volume, "dollar_volume": close * volume}, index=idx)
