"""指標と Chart Analyst のスコア。すべて当日の終値までのデータだけで計算する。

スコアは backtest と本番で同じ関数を使う。本番だけのフィルターを作らないため。
"""
from __future__ import annotations

import pandas as pd


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.where(dn > 0))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def setup_score(df: pd.DataFrame) -> pd.Series:
    """トレンド4点、モメンタム3点、出来高3点の計10点。7点以上だけが通る。"""
    c, v = df["close"], df["volume"]
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    avg_vol = v.rolling(20).mean()
    trend = 2 * (c > sma200).astype(int) + (sma50 > sma200).astype(int) + (c > sma50).astype(int)
    r14 = rsi(c, 14)
    momentum = (c > c.shift(63)).astype(int) + (c > c.shift(21)).astype(int) + r14.between(40, 75).astype(int)
    up = c.diff() > 0
    up_vol = v.where(up, 0.0).rolling(20).sum()
    dn_vol = v.where(~up, 0.0).rolling(20).sum()
    volume = (v >= avg_vol).astype(int) + (v >= 1.5 * avg_vol).astype(int) + (up_vol > dn_vol).astype(int)
    return (trend + momentum + volume).where(sma200.notna())


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """シグナルが共通で使う列をまとめて追加する。"""
    out = df.copy()
    c = out["close"]
    for n in (5, 10, 20, 50, 200):
        out[f"sma{n}"] = c.rolling(n).mean()
    out["atr"] = atr(out)
    out["rsi2"] = rsi(c, 2)
    out["vol_avg20"] = out["volume"].rolling(20).mean()
    out["dv_avg20"] = out["dollar_volume"].rolling(20).mean()
    out["score"] = setup_score(out)
    return out
