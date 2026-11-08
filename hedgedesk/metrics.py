"""成績指標、デフレートシャープ、モンテカルロ、スコア。配点は DESK_RULES.md 第8節と同じ。"""
#
# 【役割】バックテスト結果（日次リターン、資産曲線、トレード一覧）から評価指標を計算する純粋関数群。
# 【位置付け】research.evaluate / apply_gates / run_research が呼ぶ。状態もファイルも持たない。
# 【主な依存先】numpy、pandas、scipy.stats（正規分布と歪度・尖度）、config（MC の設定と1トレードのリスク）。
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats

from . import config as C

# オイラー＝マスケローニ定数。期待最大シャープ（極値分布の近似）の式に使う。
EULER = 0.5772156649015329


def sharpe(returns: pd.Series, periods: int) -> float:
    """年率換算のシャープレシオ（無リスク金利は0とみなす）。

    Args:
        returns: 期間ごとの単純リターン。
        periods: 1年あたりの期間数（株は252、暗号資産は365）。√periods 倍して年率にする。

    Returns:
        シャープレシオ。データが2点未満、または標準偏差が0（全く動かない）なら0.0。
    """
    if len(returns) < 2 or returns.std(ddof=1) == 0:
        return 0.0
    return float(returns.mean() / returns.std(ddof=1) * math.sqrt(periods))


def max_drawdown(equity: pd.Series) -> float:
    """資産曲線の最大ドローダウンを割合（0.25 = 25%）で返す。空なら0.0。

    各時点の「それまでの最高値からの下落率」の最大値。
    """
    if equity.empty:
        return 0.0
    return float((1 - equity / equity.cummax()).max())


def expected_max_sharpe(trial_sharpes: list[float], n_trials: int) -> float:
    """N回試したときに、運だけで出る最大シャープの期待値（1期間あたり）。"""
    # 引数:
    #   trial_sharpes: 各試行の1期間あたりシャープ（年率ではない）。ばらつき（標準偏差）の推定に使う。
    #   n_trials: 累計の試行回数（trial_registry.json の件数）。
    # 戻り値: 期待最大シャープ。試行が2回未満なら0.0（多重検定の割引なし）。
    # 式（Bailey & Lopez de Prado）:
    #   E[max SR] ≈ σ_SR × ((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)))   γ はオイラー定数、Φ⁻¹ は正規分布の分位点
    #   真のシャープがすべて0でも、N 回試せば最大値はこの程度になる、という基準値。
    if n_trials < 2 or len(trial_sharpes) < 2:
        return 0.0
    sd = float(np.std(trial_sharpes, ddof=1))
    return sd * ((1 - EULER) * stats.norm.ppf(1 - 1 / n_trials) + EULER * stats.norm.ppf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(returns: pd.Series, trial_sharpes_per_period: list[float], n_trials: int) -> float:
    """Bailey & Lopez de Prado のDSR。試行回数を割り引いた上で、真のシャープが正である確率。"""
    # 引数:
    #   returns: 評価対象の OOS 日次リターン。
    #   trial_sharpes_per_period: 全試行の1期間あたりシャープ（expected_max_sharpe に渡す）。
    #   n_trials: 累計の試行回数。試行が増えるほど基準 sr0 が上がり、DSR は下がる。
    # 戻り値: 0〜1 の確率。データが30点未満、標準偏差0、分母が0以下のときは 0.0（不合格側に倒す）。
    # 式: DSR = Φ( (SR − SR0) × √(T−1) / √(1 − 歪度·SR + (尖度−1)/4 · SR²) )
    #   分母はリターンが正規分布でないこと（歪み・裾の厚さ）を考慮した SR の標準誤差の補正項。
    r = returns.dropna()
    t = len(r)
    if t < 30 or r.std(ddof=1) == 0:
        return 0.0
    sr = float(r.mean() / r.std(ddof=1))
    sr0 = expected_max_sharpe(trial_sharpes_per_period, n_trials)
    skew = float(stats.skew(r, bias=False))
    kurt = float(stats.kurtosis(r, fisher=False, bias=False))
    # fisher=False で「正規分布なら3」の素の尖度を使う（式の (kurt−1)/4 はこの定義が前提）。
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:
        return 0.0
    return float(stats.norm.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom)))


def monte_carlo_drawdown(r_multiples: np.ndarray, runs: int = C.MC_RUNS, seed: int = C.MC_SEED, risk: float = C.RISK_PER_TRADE) -> dict:
    """トレードのRを復元抽出して並べ替え、各回の最大ドローダウンを測る。"""
    # 引数:
    #   r_multiples: 各トレードの損益をリスク額で割った値（R）。−1 はちょうどストップで負けた状態。
    #   runs: 試行回数。seed: 乱数シード（同じ入力なら同じ結果になるよう固定）。
    #   risk: 1トレードで資金に対して取るリスクの割合。資産は毎回 (1 + risk × R) 倍になるとみなす。
    # 戻り値: runs と、最大ドローダウンの中央値・95パーセンタイル・最悪値。トレードがなければ値は None。
    # 目的: 実際のトレード順は1通りしかないので、順番の運で起こり得たドローダウンの幅を見積もる。
    r = np.asarray(r_multiples, dtype=float)
    if r.size == 0:
        return {"runs": 0, "median": None, "p95": None, "worst": None}
    rng = np.random.default_rng(seed)
    dds = np.empty(runs)
    for k in range(runs):
        eq = np.cumprod(1 + risk * rng.choice(r, size=r.size, replace=True))
        eq = np.concatenate(([1.0], eq))
            # 先頭に開始資金 1.0 を入れる。最初のトレードからの負けもドローダウンに数えるため。
        dds[k] = float((1 - eq / np.maximum.accumulate(eq)).max())
    return {"runs": runs, "median": float(np.median(dds)), "p95": float(np.percentile(dds, 95)), "worst": float(dds.max())}


def _band(value: float, edges: list[float]) -> int:
    """edges は 2点、3点、4点、5点の下限。"""
    # 引数: value は評価値、edges は昇順の境界値4つ。
    # 戻り値: 1〜5 の点数。各境界以上なら1点ずつ加点するので、境界ちょうどの値は高い点の側に入る。
    score = 1
    for e in edges:
        if value >= e:
            score += 1
    return score


def score_card(oos_sharpe: float, dsr: float, max_dd: float, trades: int, expectancy_r: float) -> dict:
    """第8節の配点。境界の値は高い点の側に入れる。"""
    # 引数: OOS シャープ、DSR、OOS 最大ドローダウン（割合）、OOS トレード数、コスト後の平均R。
    # 戻り値: 5項目それぞれの点数（1〜5）と合計 total（5〜25）の dict。
    # max_drawdown と edge_after_costs は「小さいほど良い」「0以下は最低点」など単純な昇順でないため、条件式で直接書いている。
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
    """1つの検証結果を、レポートやブリーフに載せる要約指標の dict にする。

    Args:
        returns: 日次リターン。equity: 資産曲線。trades: pnl と r 列を持つトレード一覧。
        periods: 年率換算の期間数。

    Returns:
        sharpe、total_return、max_drawdown、trades、win_rate、expectancy_r（平均R）、net_pnl。
        トレードが0件のときは勝率と平均Rを None（0 と区別するため）、損益を0.0にする。
        JSON に保存して比較しやすいよう、すべて丸めた値にしている。
    """
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
