"""ポートフォリオ単位のシミュレータ。Desk Rules と同じ条件で売買を再現する。

時間の流れ（1日の中の順序）:
  1. 寄付き: 前日の手仕舞い予約を実行。窓開けでストップを割っていれば始値で約定。
  2. 取引中: ストップ判定 → 前日に出した指値の判定（有効期限は1日）。
     指値は「安値が指値を下回った」ときだけ約定する。触れただけでは約定としない。
     同じ足で指値とストップの両方に届いたら、損失側（約定後にストップ）とみなす。
  3. 大引け: 評価、キルスイッチ判定、翌日の注文作成。

シグナルは当日の終値までの情報で作り、最短でも翌日の足で約定する。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import config as C


@dataclass
class Costs:
    slip_bps: float = C.SLIP_BPS
    entry_fee_bps: float = C.EQUITY_FEE_BPS
    exit_fee_bps: float = C.EQUITY_FEE_BPS

    def doubled_slip(self) -> "Costs":
        return Costs(self.slip_bps * 2, self.entry_fee_bps, self.exit_fee_bps)


def costs_for(asset_class: str) -> Costs:
    if asset_class == "crypto":
        # 指値の新規はメイカー、ストップや期限切れの手仕舞いはテイカーとみなす
        return Costs(C.SLIP_BPS, C.CRYPTO_MAKER_BPS, C.CRYPTO_TAKER_BPS)
    return Costs()


@dataclass
class Result:
    equity: pd.Series
    trades: pd.DataFrame
    kills: list = field(default_factory=list)

    @property
    def returns(self) -> pd.Series:
        # 初日は開始資金との比較で出す（期間をつないでも1日分を失わないように）
        if self.equity.empty:
            return pd.Series(dtype=float)
        r = self.equity.pct_change()
        r.iloc[0] = self.equity.iloc[0] / C.CAPITAL - 1
        return r


TRADE_COLUMNS = ["symbol", "signal_date", "entry_date", "entry", "stop", "qty", "exit_date", "exit", "reason", "pnl", "risk", "r", "bars"]


def _matrix(signals: dict[str, pd.DataFrame], col: str, index: pd.DatetimeIndex, symbols: list[str], fill) -> np.ndarray:
    return np.column_stack([signals[s][col].reindex(index).to_numpy(dtype=float, na_value=np.nan) if col in signals[s] else np.full(len(index), np.nan) for s in symbols]) if fill is None else np.column_stack(
        [signals[s][col].reindex(index).fillna(fill).to_numpy(dtype=float) for s in symbols]
    )


def simulate(
    market: dict[str, pd.DataFrame],
    signals: dict[str, pd.DataFrame],
    start: str,
    end: str | None,
    costs: Costs,
    lag: int = 0,
    capital: float = C.CAPITAL,
    fractional: bool = False,
    apply_kill: bool = True,
) -> Result:
    """signals は銘柄ごとに entry, exit, limit, stop, rank の列を持つ。lag は遅延する足の本数。"""
    symbols = sorted(signals)
    index = pd.DatetimeIndex(sorted(set().union(*[market[s].index for s in symbols])))
    index = index[index <= pd.Timestamp(end)] if end else index
    if len(index) == 0 or not symbols:
        return Result(pd.Series(dtype=float), pd.DataFrame(columns=TRADE_COLUMNS))
    i0 = int(index.searchsorted(pd.Timestamp(start)))
    if i0 >= len(index):
        return Result(pd.Series(dtype=float), pd.DataFrame(columns=TRADE_COLUMNS))

    def price(col):
        return np.column_stack([market[s][col].reindex(index).to_numpy(dtype=float) for s in symbols])

    O, H, L, CL = price("open"), price("high"), price("low"), price("close")
    entry = _matrix(signals, "entry", index, symbols, 0.0) > 0
    exit_ = _matrix(signals, "exit", index, symbols, 0.0) > 0
    limit = _matrix(signals, "limit", index, symbols, None)
    stop = _matrix(signals, "stop", index, symbols, None)
    rank = _matrix(signals, "rank", index, symbols, 0.0)
    if lag:
        # 1本遅れ: 注文が遅れて出る。古い指値のままだと押し目だけを拾って逆に有利になるので、
        # 指値の恩恵をなくし、遅れた足の始値で約定させる。ストップの価格は元のまま。
        for arr, blank in ((entry, False), (exit_, False), (limit, np.nan), (stop, np.nan), (rank, 0.0)):
            arr[lag:] = arr[:-lag].copy()
            arr[:lag] = blank
        limit = np.where(np.isnan(limit), np.nan, np.inf)

    slip = costs.slip_bps / 1e4
    fee_in = costs.entry_fee_bps / 1e4
    fee_out = costs.exit_fee_bps / 1e4
    cash = capital
    equity_prev = capital
    positions: dict[int, dict] = {}
    pending: list[dict] = []
    trades: list[dict] = []
    kills: list[dict] = []
    losses_in_row = 0
    halt_until = -1
    curve = []

    def close_pos(j: int, i: int, px: float, reason: str):
        nonlocal cash, losses_in_row
        p = positions.pop(j)
        fill = px * (1 - slip)
        cash += p["qty"] * fill * (1 - fee_out)
        pnl = p["qty"] * fill * (1 - fee_out) - p["cost"]
        losses_in_row = losses_in_row + 1 if pnl < 0 else 0
        trades.append({
            "symbol": symbols[j], "signal_date": p["signal_date"], "entry_date": p["entry_date"], "entry": p["entry"],
            "stop": p["stop"], "qty": p["qty"], "exit_date": index[i], "exit": fill, "reason": reason, "pnl": pnl,
            "risk": p["risk"], "r": pnl / p["risk"], "bars": p["bars"],
        })

    for i in range(i0, len(index)):
        day_start_equity = equity_prev
        # 1. 保有ポジションの処理
        for j in list(positions):
            p = positions[j]
            o, l = O[i, j], L[i, j]
            if np.isnan(o):
                continue  # この銘柄は今日の足がない
            if p["exit_next"]:
                close_pos(j, i, o, p["exit_reason"])
            elif o <= p["stop"]:
                close_pos(j, i, o, "stop_gap")
            elif l <= p["stop"]:
                close_pos(j, i, p["stop"], "stop")

        # 2. 前日に出した指値の判定
        for od in sorted(pending, key=lambda x: (-x["rank"], x["j"])):
            j = od["j"]
            o, l = O[i, j], L[i, j]
            if j in positions or np.isnan(o) or not (l < od["limit"]):
                continue
            fill = min(o, od["limit"]) * (1 + slip)
            risk_per_unit = fill - od["stop"]
            if risk_per_unit <= 0:
                continue
            open_risk = sum(p["risk"] for p in positions.values())
            budget = min(C.RISK_PER_TRADE * equity_prev, C.MAX_OPEN_RISK * equity_prev - open_risk)
            if budget <= 0:
                continue
            qty = min(budget / risk_per_unit, C.MAX_POSITION_NOTIONAL * equity_prev / fill, cash / (fill * (1 + fee_in)))
            qty = qty if fractional else float(np.floor(qty))
            if qty <= 0:
                continue
            cost = qty * fill * (1 + fee_in)
            cash -= cost
            positions[j] = {
                "qty": qty, "entry": fill, "stop": od["stop"], "cost": cost, "risk": qty * risk_per_unit,
                "signal_date": od["signal_date"], "entry_date": index[i], "bars": 0, "exit_next": False, "exit_reason": "",
                "max_hold": od["max_hold"],
            }
            if l <= od["stop"]:  # 同じ足でストップにも届いたら損失側で処理
                close_pos(j, i, min(od["stop"], fill), "stop_same_bar")
        pending = []

        # 3. 大引けの評価
        mark = cash
        for j, p in positions.items():
            c = CL[i, j]
            if np.isnan(c):
                c = p.get("last_close", p["entry"])
            p["last_close"] = c
            mark += p["qty"] * c
        equity = mark
        curve.append(equity)
        equity_prev = equity

        # キルスイッチ（再開は5営業日後と仮定）
        if apply_kill and i >= halt_until:
            why = None
            if equity <= day_start_equity * (1 - C.KILL_DAILY_LOSS):
                why = "daily_loss"
            elif losses_in_row >= C.KILL_CONSECUTIVE_LOSSES:
                why = "consecutive_losses"
            if why:
                kills.append({"date": index[i], "reason": why})
                halt_until = i + C.BACKTEST_KILL_PAUSE_BARS
                losses_in_row = 0

        # 手仕舞い予約（最低保有2本、最大保有は戦略ごと）
        for j, p in positions.items():
            if np.isnan(CL[i, j]):
                continue
            p["bars"] += 1
            if p["bars"] >= p["max_hold"]:
                p["exit_next"], p["exit_reason"] = True, "time"
            elif exit_[i, j] and p["bars"] >= C.MIN_HOLD_BARS:
                p["exit_next"], p["exit_reason"] = True, "signal"

        # 翌日の注文作成
        if i >= halt_until:
            for j in np.flatnonzero(entry[i]):
                if j in positions or np.isnan(limit[i, j]) or np.isnan(stop[i, j]):
                    continue
                pending.append({
                    "j": int(j), "limit": limit[i, j], "stop": stop[i, j], "rank": rank[i, j], "signal_date": index[i],
                    "max_hold": signals[symbols[j]].attrs.get("max_hold", C.MAX_HOLD_BARS),
                })

    # 検証期間の終わりに残った建玉は終値で閉じる
    last = len(index) - 1
    for j in list(positions):
        close_pos(j, last, positions[j]["last_close"], "window_end")
    curve[-1] = cash
    eq = pd.Series(curve, index=index[i0:], name="equity")
    return Result(eq, pd.DataFrame(trades, columns=TRADE_COLUMNS), kills)
