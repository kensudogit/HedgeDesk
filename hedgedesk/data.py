"""市場データの取得と品質確認。出所は Yahoo Finance（yfinance、非公式経路）。

注意:
- Yahooの Close は分割調整済みで配当は未調整。OHLCは Adj Close に合わせて配当も調整する。
- 未確定の足（当日の取引中の足）は必ず落とす。ここを誤ると先読みになる。
"""
from __future__ import annotations

import hashlib
import os
import warnings
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .config import CACHE, DATA_START

NY = ZoneInfo("America/New_York")
SOURCE = "Yahoo Finance via yfinance"
VIX_SYMBOL = "^VIX"
STABLE_TOLERANCE = 1e-5     # 許容する相対差。実測の揺れは1e-6程度で、配当の調整はこれより十分大きい
STABLE_RECENT_BARS = 5      # 直近の足は未確定や訂正があり得るので、常に新しい値を使う


def _adjust(raw: pd.DataFrame) -> pd.DataFrame:
    """配当込みに調整した OHLC を作る。売買代金は分割の影響を受けない値を残す。"""
    factor = raw["Adj Close"] / raw["Close"]
    df = pd.DataFrame(index=raw.index)
    df["open"] = raw["Open"] * factor
    df["high"] = raw["High"] * factor
    df["low"] = raw["Low"] * factor
    df["close"] = raw["Adj Close"]
    df["volume"] = raw["Volume"]
    df["dollar_volume"] = raw["Close"] * raw["Volume"]
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


def drop_incomplete(df: pd.DataFrame, symbol: str, now: datetime | None = None) -> pd.DataFrame:
    """確定していない最新足を除く。暗号資産はUTC日付、株はNY時間16:15が境目。"""
    if df.empty:
        return df
    now = now or datetime.now(timezone.utc)
    last = df.index[-1].date()
    if symbol.endswith("-USD"):
        return df[df.index.date < now.astimezone(timezone.utc).date()]
    ny = now.astimezone(NY)
    if last > ny.date() or (last == ny.date() and ny.time() < time(16, 15)):
        return df[df.index.date < ny.date()]
    return df


def quality_issues(df: pd.DataFrame) -> list[str]:
    """データの不備を文字列で返す。空なら問題なし。"""
    if df.empty:
        return ["no data"]
    out = []
    core = df[["open", "high", "low", "close"]]
    if core.isna().any(axis=None):
        out.append(f"NaN rows={int(core.isna().any(axis=1).sum())}")
    if (core.dropna() <= 0).any(axis=None):
        out.append("non-positive prices")
    tol = 1e-6 * df["close"]
    if ((df["high"] + tol < df[["open", "close"]].max(axis=1)) | (df["low"] - tol > df[["open", "close"]].min(axis=1))).any():
        out.append("OHLC inconsistent")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        out.append("bad index")
    return out


def _cache_path(symbol: str, stamp: str):
    return CACHE / f"{symbol.replace('^', '_')}_{stamp}.pkl"


def _save(df: pd.DataFrame, path) -> None:
    """書きかけのファイルを別のコンテナが読まないよう、一時ファイル経由で置き換える。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    df.to_pickle(tmp)
    os.replace(tmp, path)


def _latest_path(symbol: str):
    return CACHE / f"latest_{symbol.replace('^', '_')}.pkl"


def stabilize(old: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """取得ごとの微小な揺れを吸収する。(データ, 過去の値が本当に変わったか) を返す。

    Yahooの調整後価格は、同じ日でも取得ごとに百万分の1程度揺れ、境界付近の判定を変えてしまう。
    過去の足が許容差内で同じなら前回の値を使い続け、直近の足だけ新しい値にする。
    配当、分割、訂正で本当に変わったときは、新しい値にすべて置き換える。
    """
    cut = old.index[-STABLE_RECENT_BARS] if len(old) > STABLE_RECENT_BARS else old.index[0]
    past = old.index[old.index < cut]
    if len(past) == 0 or not past.isin(new.index).all():
        return new, True
    cols = ["open", "high", "low", "close", "volume"]
    a, b = old.loc[past, cols], new.loc[past, cols]
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = ((a - b).abs() / a.abs().where(a != 0, 1.0)).to_numpy()
    if np.nan_to_num(rel).max() > STABLE_TOLERANCE:
        return new, True
    return pd.concat([old.loc[past], new[new.index >= cut]]), False


def load(symbols: list[str], refresh: bool = False, now: datetime | None = None) -> tuple[dict[str, pd.DataFrame], dict]:
    """銘柄ごとのDataFrameと、取得記録（出所、期間、欠損、不備）を返す。"""
    import yfinance as yf

    now = now or datetime.now(timezone.utc)
    CACHE.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d")
    out: dict[str, pd.DataFrame] = {}
    log: dict = {"source": SOURCE, "as_of_utc": now.isoformat(timespec="seconds"), "symbols": {}}
    missing = []
    for s in symbols:
        path = _cache_path(s, stamp)
        if path.exists() and not refresh:
            out[s] = pd.read_pickle(path)
        else:
            missing.append(s)
    if missing:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw = yf.download(missing, start=DATA_START, auto_adjust=False, progress=False, threads=False, group_by="ticker")
        for s in missing:
            try:
                sub = raw[s].dropna(subset=["Close"]) if isinstance(raw.columns, pd.MultiIndex) else raw.dropna(subset=["Close"])
                if s == "^VIX":
                    sub = sub.assign(Volume=0.0)
                df = _adjust(sub)
            except KeyError:
                df = pd.DataFrame()
            latest = _latest_path(s)
            if len(df) and latest.exists():
                df, changed = stabilize(pd.read_pickle(latest), df)
                log.setdefault("history_changed", {})[s] = changed
            if len(df):
                _save(df, latest)
            _save(df, _cache_path(s, stamp))
            out[s] = df
    for s in symbols:
        out[s] = drop_incomplete(out[s], s, now)
        df = out[s]
        log["symbols"][s] = {
            "first": str(df.index[0].date()) if len(df) else None,
            "last": str(df.index[-1].date()) if len(df) else None,
            "bars": len(df),
            "issues": quality_issues(df),
        }
    if VIX_SYMBOL in out:
        check_vix(out[VIX_SYMBOL])
    log["fingerprint"] = fingerprint(out)
    return out, log


def check_vix(df: pd.DataFrame) -> None:
    """VIXの列に別の銘柄が混ざっていないか確かめる。おかしければ調査を止める。"""
    if df.empty:
        return
    median = float(df["close"].median())
    if not 8 <= median <= 45:
        raise ValueError(f"VIXの中央値が{median:.1f}で不自然。データを確認するまで検証しない")


def fingerprint(frames: dict[str, pd.DataFrame]) -> str:
    """読み込んだ全データの指紋。同じ指紋なら同じ入力で検証したと確認できる。"""
    h = hashlib.sha256()
    for s in sorted(frames):
        df = frames[s]
        h.update(s.encode())
        if len(df):
            h.update(df.index.values.tobytes())
            h.update(df[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float).tobytes())
    return h.hexdigest()[:16]


def earnings_dates(symbols: list[str], refresh: bool = False) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """決算日を取得する。列: reaction(最初に反応する営業日), surprise(%)。取れない銘柄は失敗一覧に入れる。"""
    import yfinance as yf

    CACHE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out, failed = {}, []
    for s in symbols:
        path = CACHE / f"earn_{s}_{stamp}.pkl"
        if path.exists() and not refresh:
            out[s] = pd.read_pickle(path)
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                raw = yf.Ticker(s).get_earnings_dates(limit=100)
            if raw is None or raw.empty:
                raise ValueError("empty")
            idx = raw.index.tz_convert(NY)
            # 寄付き前（0時より後、9時台前半まで）の発表だけ当日に反応とする。
            # 発表時刻が不明（0時）や取引時間中は、サプライズを終値判定に使うと先読みになるので翌営業日に送る。
            minutes = idx.hour * 60 + idx.minute
            before_open = (minutes > 0) & (minutes < 9 * 60 + 30)
            after_close = ~before_open
            base = pd.DatetimeIndex(idx.tz_localize(None).normalize())
            reaction = base + pd.to_timedelta(after_close.astype(int), unit="D")
            reaction = pd.DatetimeIndex([d + pd.offsets.BDay(0) for d in reaction])
            df = pd.DataFrame({"reaction": reaction, "surprise": raw["Surprise(%)"].to_numpy()}).sort_values("reaction")
            df = df.drop_duplicates("reaction").reset_index(drop=True)
            df.to_pickle(path)
            out[s] = df
        except Exception:
            failed.append(s)
    return out, failed
