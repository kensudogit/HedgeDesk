"""成績指標、デフレートシャープ、モンテカルロ、スコア。配点は DESK_RULES.md 第8節と同じ。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats

from . import config as C

EULER = 0.5772156649015329


def sharpe(returns: pd.Series, periods: int) -> float:
    if len(returns) < 2 or returns.std(ddof=1) == 0:
        return 0.0
    return float(returns.mean() / returns.std(ddof=1) * math.sqrt(periods))


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((1 - equity / equity.cummax()).max())


def expected_max_sharpe(trial_sharpes: list[float], n_trials: int) -> float:
    """N回試したときに、運だけで出る最大シャープの期待値（1期間あたり）。"""
    if n_trials < 2 or len(trial_sharpes) < 2:
        return 0.0
    sd = float(np.std(trial_sharpes, ddof=1))
    return sd * ((1 - EULER) * stats.norm.ppf(1 - 1 / n_trials) + EULER * stats.norm.ppf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(returns: pd.Series, trial_sharpes_per_period: list[float], n_trials: int) -> float:
    """Bailey & Lopez de Prado のDSR。試行回数を割り引いた上で、真のシャープが正である確率。"""
    r = returns.dropna()
    t = len(r)
    if t < 30 or r.std(ddof=1) == 0:
        return 0.0
    sr = float(r.mean() / r.std(ddof=1))
    sr0 = expected_max_sharpe(trial_sharpes_per_period, n_trials)
    skew = float(stats.skew(r, bias=False))
    kurt = float(stats.kurtosis(r, fisher=False, bias=False))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:
        return 0.0
    return float(stats.norm.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom)))


def monte_carlo_drawdown(r_multiples: np.ndarray, runs: int = C.MC_RUNS, seed: int = C.MC_SEED, risk: float = C.RISK_PER_TRADE) -> dict:
    """トレードのRを復元抽出して並べ替え、各回の最大ドローダウンを測る。"""
    r = np.asarray(r_multiples, dtype=float)
    if r.size == 0:
        return {"runs": 0, "median": None, "p95": None, "worst": None}
    rng = np.random.default_rng(seed)
    dds = np.empty(runs)
    for k in range(runs):
        eq = np.cumprod(1 + risk * rng.choice(r, size=r.size, replace=True))
        eq = np.concatenate(([1.0], eq))
        dds[k] = float((1 - eq / np.maximum.accumulate(eq)).max())
    return {"runs": runs, "median": float(np.median(dds)), "p95": float(np.percentile(dds, 95)), "worst": float(dds.max())}


def _band(value: float, edges: list[float]) -> int:
    """edges は 2点、3点、4点、5点の下限。"""
    score = 1
    for e in edges:
        if value >= e:
            score += 1
    return score


def score_card(oos_sharpe: float, dsr: float, max_dd: float, trades: int, expectancy_r: float) -> dict:
    """第8節の配点。境界の値は高い点の側に入れる。"""
    parts = {
        "oos_sharpe": _band(oos_sharpe, [0.3, 0.6, 0.9, 1.2]),
        "deflated_sharpe": _band(dsr, [0.50, 0.80, 0.90, 0.95]),
        "max_drawdown": 1 if max_dd > 0.35 else 2 if max_dd > 0.25 else 3 if max_dd > 0.15 else 4 if max_dd >= 0.10 else 5,
        "trade_count": _band(trades, [30, 60, 100, 200]),
        "edge_after_costs": 1 if expectancy_r <= 0 else 2 if expectancy_r < 0.05 else 3 if expectancy_r < 0.10 else 4 if expectancy_r < 0.20 else 5,
    }
    parts["total"] = sum(parts.values())
    return parts


def summarize(returns: pd.Series, equity: pd.Series, trades: pd.DataFrame, periods: int) -> dict:
    n = len(trades)
    return {
        "sharpe": round(sharpe(returns, periods), 3),
        "total_return": round(float((1 + returns).prod() - 1), 4) if len(returns) else 0.0,
        "max_drawdown": round(max_drawdown(equity), 4),
        "trades": n,
        "win_rate": round(float((trades["pnl"] > 0).mean()), 3) if n else None,
        "expectancy_r": round(float(trades["r"].mean()), 4) if n else None,
        "net_pnl": round(float(trades["pnl"].sum()), 2) if n else 0.0,
    }
