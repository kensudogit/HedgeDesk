"""指標と Chart Analyst のスコア。すべて当日の終値までのデータだけで計算する。

スコアは backtest と本番で同じ関数を使う。本番だけのフィルターを作らないため。
"""
#
# 【役割】移動平均・ATR・RSI などのテクニカル指標と、Chart Analyst の10点スコアを計算する。
# 【位置付け】strategies.prepare が全銘柄に enrich を適用し、各戦略と Chart Analyst がその列を使う。
#   pine/hedge_desk_watch.pine は setup_score と同じ定義を TradingView 上で再現している。
# 【主な依存先】pandas のみ。
# 【先読み防止】rolling / ewm / shift(正の値) はすべて「その日以前」のデータしか参照しない。
#   shift(-1) のような未来を参照する操作は、このモジュールでは使わない。
from __future__ import annotations

import pandas as pd


def rsi(close: pd.Series, n: int) -> pd.Series:
    """Wilder の RSI（相対力指数）を計算する。

    Args:
        close: 終値の系列（日付昇順）。
        n: 期間。Wilder の平滑化は alpha=1/n の指数移動平均と同じなので ewm で計算する。

    Returns:
        0〜100 の RSI 系列。最初の n 本は min_periods により NaN。
        下落幅の平均が0（n 本すべて上昇）のときは、0除算を避けるため dn を NaN にしており、結果も NaN になる。
    """
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.where(dn > 0))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """ATR（真の値幅の平均）を計算する。ストップ幅や指値の上乗せ幅の単位として使う。

    Args:
        df: high / low / close 列を持つ日足。
        n: 平均の期間（既定14）。RSI と同じく Wilder の平滑化（alpha=1/n）を使う。

    Returns:
        ATR の系列。最初の n 本は NaN。
    """
    # 真の値幅 = max(当日高値−安値, |高値−前日終値|, |安値−前日終値|)。窓開けの値幅も含めるため前日終値を使う。
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def setup_score(df: pd.DataFrame) -> pd.Series:
    """トレンド4点、モメンタム3点、出来高3点の計10点。7点以上だけが通る。"""
    # 引数: df は close / volume 列を持つ日足（日付昇順）。
    # 戻り値: 0〜10 の整数スコアの系列。200日線が計算できない最初の約200本は NaN（判定不能として扱う）。
    # 配点の内訳:
    #   トレンド: 終値>200日線 で2点、50日線>200日線 で1点、終値>50日線 で1点
    #   モメンタム: 終値が63営業日前（約3か月）より上、21営業日前（約1か月）より上、RSI(14) が 40〜75
    #               （RSI の上限75は、過熱しすぎた足を避けるため）
    #   出来高: 当日出来高が20日平均以上、20日平均の1.5倍以上、20日間で上昇日の出来高合計 > 下落日の合計
    c, v = df["close"], df["volume"]
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    avg_vol = v.rolling(20).mean()
    trend = 2 * (c > sma200).astype(int) + (sma50 > sma200).astype(int) + (c > sma50).astype(int)
    r14 = rsi(c, 14)
    momentum = (c > c.shift(63)).astype(int) + (c > c.shift(21)).astype(int) + r14.between(40, 75).astype(int)
    up = c.diff() > 0
    # 前日比が0（変わらず）や最初の足（diff が NaN）は上昇日に含めず、下落側に数える。
    up_vol = v.where(up, 0.0).rolling(20).sum()
    dn_vol = v.where(~up, 0.0).rolling(20).sum()
    volume = (v >= avg_vol).astype(int) + (v >= 1.5 * avg_vol).astype(int) + (up_vol > dn_vol).astype(int)
    return (trend + momentum + volume).where(sma200.notna())


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """シグナルが共通で使う列をまとめて追加する。"""
    # 引数: df は open / high / low / close / volume / dollar_volume 列を持つ日足。
    # 戻り値: 元の列に sma5〜sma200、atr、rsi2、vol_avg20、dv_avg20、score を加えた新しい DataFrame。
    # 副作用: なし（入力 df は copy してから列を追加するので変更しない）。
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
