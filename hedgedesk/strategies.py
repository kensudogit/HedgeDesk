"""事前登録する戦略仮説。パラメータの組み合わせはすべて試行数に数える。

各戦略は、当日の終値までの情報で entry / exit / limit / stop / rank を作る。
全戦略に共通で、Chart Analyst のスコア7点以上と流動性の条件を課す。
個別株には「決算跨ぎ禁止」（今後11営業日に決算がある、または日付不明なら見送り）を課す。
"""
from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from . import config as C
from .indicators import enrich


@dataclass
class Context:
    """シグナル計算に使うデータ一式。"""
    market: dict[str, pd.DataFrame]  # enrich 済み
    vix: pd.Series | None = None
    earnings: dict[str, pd.DataFrame] | None = None


def prepare(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    return {s: enrich(df) for s, df in raw.items() if len(df) > 0 and s != C.VIX}


def earnings_block(index: pd.DatetimeIndex, reactions: pd.Series | None, horizon: int = C.MAX_HOLD_BARS + 1) -> pd.Series:
    """今後 horizon 営業日に決算反応日があれば True。決算データがなければ全日 True。"""
    if reactions is None or len(reactions) == 0:
        return pd.Series(True, index=index)
    blocked = np.zeros(len(index), dtype=bool)
    for d in reactions:
        pos = index.searchsorted(d)  # d 以上で最初の営業日
        if pos >= len(index):
            continue
        blocked[max(0, pos - horizon):pos] = True  # 反応日の前 horizon 本は入らない
    # 決算データが始まる前の期間と、最後の決算反応日以降で次が未定の期間は不明として塞ぐ
    first, last = pd.Timestamp(reactions.min()), pd.Timestamp(reactions.max())
    blocked |= index < first
    blocked |= index > last
    return pd.Series(blocked, index=index)


def _finish(df: pd.DataFrame, entry: pd.Series, exit_: pd.Series, limit: pd.Series, stop_k: float, max_hold: int, rank: pd.Series | None = None) -> pd.DataFrame:
    gate = (df["score"] >= C.MIN_SETUP_SCORE) & (df["dv_avg20"] >= C.MIN_DOLLAR_VOLUME)
    out = pd.DataFrame(index=df.index)
    out["entry"] = (entry.fillna(False).astype(bool) & gate).astype(float)
    out["exit"] = exit_.fillna(False).astype(float)
    out["limit"] = limit
    out["stop"] = limit - stop_k * df["atr"]
    out["rank"] = rank if rank is not None else df["score"]
    out["score"] = df["score"]
    out.attrs["max_hold"] = int(max_hold)
    return out


def _with_earnings(ctx: Context, sym: str, sig: pd.DataFrame) -> pd.DataFrame:
    ed = (ctx.earnings or {}).get(sym)
    block = earnings_block(sig.index, ed["reaction"] if ed is not None else None)
    sig["entry"] = sig["entry"].where(~block, 0.0)
    return sig


def pead(ctx: Context, syms: list[str], p: dict) -> dict[str, pd.DataFrame]:
    """決算後ドリフト: 良い決算に市場が反応した日の終値で判定し、翌日に指値で入る。"""
    out = {}
    for s in syms:
        df = ctx.market[s]
        ed = (ctx.earnings or {}).get(s)
        if ed is None:
            continue
        hit = pd.Series(False, index=df.index)
        for _, row in ed.iterrows():
            pos = df.index.searchsorted(row["reaction"])
            if pos < len(df) and pd.notna(row["surprise"]) and row["surprise"] >= p["min_surprise"]:
                hit.iloc[pos] = True
        day_ret = df["close"] / df["close"].shift(1) - 1
        entry = hit & (day_ret >= p["min_react"]) & (df["volume"] >= p["vol_mult"] * df["vol_avg20"].shift(1)) & (df["close"] > df["sma50"])
        limit = df["close"] + 0.25 * df["atr"]
        sig = _finish(df, entry, df["close"] < df["sma20"], limit, p["stop_k"], p["max_hold"], rank=day_ret)
        out[s] = _with_earnings(ctx, s, sig)
    return out


def mean_reversion(ctx: Context, syms: list[str], p: dict) -> dict[str, pd.DataFrame]:
    """上昇基調の中での売られすぎの反発。"""
    out = {}
    for s in syms:
        df = ctx.market[s]
        entry = (df["close"] > df["sma200"]) & (df["rsi2"] < p["rsi_th"])
        out[s] = _finish(df, entry, df["close"] > df["sma5"], df["close"], p["stop_k"], p["max_hold"], rank=-df["rsi2"])
    return out


def breakout(ctx: Context, syms: list[str], p: dict) -> dict[str, pd.DataFrame]:
    """出来高を伴う高値ブレイク。"""
    out = {}
    for s in syms:
        df = ctx.market[s]
        prior_high = df["high"].shift(1).rolling(p["lookback"]).max()
        entry = (df["close"] > prior_high) & (df["volume"] >= p["vol_mult"] * df["vol_avg20"].shift(1)) & (df["close"] > df["sma200"])
        limit = df["close"] + 0.25 * df["atr"]
        exit_ = df["close"] < df[f"sma{p['exit_sma']}"]
        sig = _finish(df, entry, exit_, limit, p["stop_k"], p["max_hold"], rank=df["volume"] / df["vol_avg20"])
        out[s] = _with_earnings(ctx, s, sig) if s in C.STOCKS else sig
    return out


def sector_rotation(ctx: Context, syms: list[str], p: dict) -> dict[str, pd.DataFrame]:
    """週末に強いセクターの上位を買う。市場全体が上昇基調のときだけ。"""
    closes = pd.DataFrame({s: ctx.market[s]["close"] for s in syms})
    mom = closes / closes.shift(p["mom"]) - 1
    ranks = mom.rank(axis=1, ascending=False)
    spy = ctx.market["SPY"]
    market_up = (spy["close"] > spy["sma200"]).reindex(closes.index).fillna(False)
    # 週の最終営業日かどうかは、次の足を見ずに「金曜か」で決める（先読み防止）
    week_end = pd.Series(closes.index.dayofweek == 4, index=closes.index)
    out = {}
    for s in syms:
        df = ctx.market[s]
        entry = (ranks[s] <= p["top_n"]).reindex(df.index).fillna(False) & market_up.reindex(df.index).fillna(False) & week_end.reindex(df.index).fillna(False)
        limit = df["close"] + 0.25 * df["atr"]
        out[s] = _finish(df, entry, pd.Series(False, index=df.index), limit, p["stop_k"], p["hold"], rank=mom[s].reindex(df.index))
    return out


def vol_regime(ctx: Context, syms: list[str], p: dict) -> dict[str, pd.DataFrame]:
    """VIX急騰の後、VIXが10日平均を下回って落ち着き始めた日に買う。"""
    if ctx.vix is None:
        return {}
    vix = ctx.vix
    sma = vix.rolling(10).mean()
    was_high = (vix >= p["vix_th"]).rolling(10).max().fillna(0) > 0
    calm = (vix < sma) & (vix.shift(1) >= sma.shift(1)) & was_high
    out = {}
    for s in syms:
        df = ctx.market[s]
        entry = calm.reindex(df.index).fillna(False) & (df["close"] > df["sma20"])
        limit = df["close"] + 0.25 * df["atr"]
        out[s] = _finish(df, entry, pd.Series(False, index=df.index), limit, p["stop_k"], p["max_hold"])
    return out


@dataclass
class Strategy:
    id: str
    name: str
    asset_class: str
    symbols: list[str]
    fn: Callable
    grid: dict
    thesis: str
    other_side: str
    entry_rule: str
    exit_rule: str
    stop_rule: str
    breaks_when: str

    def params(self) -> list[dict]:
        keys = list(self.grid)
        return [dict(zip(keys, v)) for v in itertools.product(*self.grid.values())]

    def signals(self, ctx: Context, p: dict) -> dict[str, pd.DataFrame]:
        return self.fn(ctx, [s for s in self.symbols if s in ctx.market], p)


def trial_id(strategy_id: str, params: dict) -> str:
    body = json.dumps({"s": strategy_id, "p": params}, sort_keys=True)
    return strategy_id + ":" + hashlib.sha1(body.encode()).hexdigest()[:10]


_BREAK_ENTRY = "終値が前日までの{lookback}日高値を超え、出来高が20日平均の{vol_mult}倍以上、終値が200日線より上、スコア7以上。翌日、終値+0.25ATRの指値。"
STRATEGIES: list[Strategy] = [
    Strategy(
        "S1_PEAD", "決算後ドリフト（個別株）", "stock", C.STOCKS, pead,
        {"min_surprise": [0.0, 5.0], "min_react": [0.02, 0.04], "vol_mult": [1.5], "stop_k": [2.0, 3.0], "max_hold": [5, 10]},
        "良い決算への反応は1日で終わらず、数日かけて続く。",
        "決算情報の織り込みが遅い投資家、発表後に利益確定や逆張りをする参加者。",
        "決算反応日にEPSサプライズ{min_surprise}%以上、終値が{min_react}以上上昇、出来高が平均の{vol_mult}倍以上、終値が50日線より上、スコア7以上。翌日、終値+0.25ATRの指値。",
        "終値が20日線を下回った翌日の始値、または{max_hold}本で手仕舞い（最低2本保有）。",
        "指値-{stop_k}ATR。",
        "決算への反応が翌日に反転しやすい局面、市場全体の急落局面。",
    ),
    Strategy(
        "S3_MR_ETF", "売られすぎの反発（ETF）", "etf", C.ETFS, mean_reversion,
        {"rsi_th": [5.0, 10.0], "stop_k": [2.0, 3.0], "max_hold": [5, 10]},
        "上昇基調の中の短期的な投げ売りは行き過ぎやすく、数日で戻る。",
        "恐怖で投げる投資家、ボラティリティ上昇で機械的に売る参加者。",
        "終値が200日線より上、RSI(2)が{rsi_th}未満、スコア7以上。翌日、前日終値の指値。",
        "終値が5日線を上回った翌日の始値、または{max_hold}本で手仕舞い（最低2本保有）。",
        "指値-{stop_k}ATR。",
        "下落が連鎖する弱気相場の入り口。200日線を割るとシグナルは止まるが、割る途中で損失が出る。",
    ),
    Strategy(
        "S3_MR_CRYPTO", "売られすぎの反発（暗号資産）", "crypto", C.CRYPTO, mean_reversion,
        {"rsi_th": [5.0, 10.0], "stop_k": [2.0, 3.0], "max_hold": [5, 10]},
        "上昇基調の暗号資産での短期の投げ売りは反発しやすい。",
        "清算やパニックで売らされる参加者。",
        "終値が200日線より上、RSI(2)が{rsi_th}未満、スコア7以上。翌日、前日終値の指値。",
        "終値が5日線を上回った翌日の始値、または{max_hold}本で手仕舞い（最低2本保有）。",
        "指値-{stop_k}ATR。Alpacaではstop_limitのため、飛ぶと約定しないリスクあり。",
        "取引所の破綻や規制など、構造的な売り。手数料が高い局面。",
    ),
    Strategy(
        "S4_BO_ETF", "出来高ブレイク（ETF）", "etf", C.ETFS, breakout,
        {"lookback": [20, 55], "vol_mult": [1.5], "stop_k": [2.0, 3.0], "exit_sma": [10, 20], "max_hold": [10]},
        "資金流入を伴う高値更新は短期的に続きやすい。",
        "レンジ上限で売る逆張り勢、遅れて追随する買い手。",
        _BREAK_ENTRY,
        "終値が{exit_sma}日線を下回った翌日の始値、または{max_hold}本で手仕舞い（最低2本保有）。",
        "指値-{stop_k}ATR。",
        "レンジ相場でのだまし上げ。",
    ),
    Strategy(
        "S4_BO_STOCK", "出来高ブレイク（個別株）", "stock", C.STOCKS, breakout,
        {"lookback": [20, 55], "vol_mult": [1.5], "stop_k": [2.0, 3.0], "exit_sma": [10, 20], "max_hold": [10]},
        "資金流入を伴う高値更新は短期的に続きやすい。",
        "レンジ上限で売る逆張り勢、遅れて追随する買い手。",
        _BREAK_ENTRY,
        "終値が{exit_sma}日線を下回った翌日の始値、または{max_hold}本で手仕舞い（最低2本保有）。",
        "指値-{stop_k}ATR。",
        "レンジ相場でのだまし上げ。銘柄固有ニュースでの窓開け下落。",
    ),
    Strategy(
        "S4_BO_CRYPTO", "出来高ブレイク（暗号資産）", "crypto", C.CRYPTO, breakout,
        {"lookback": [20, 55], "vol_mult": [1.5], "stop_k": [2.0, 3.0], "exit_sma": [10, 20], "max_hold": [10]},
        "暗号資産のトレンドは短期的に持続しやすい。",
        "逆張りの売り手、遅れて飛び乗る買い手。",
        _BREAK_ENTRY,
        "終値が{exit_sma}日線を下回った翌日の始値、または{max_hold}本で手仕舞い（最低2本保有）。",
        "指値-{stop_k}ATR。Alpacaではstop_limitのため、飛ぶと約定しないリスクあり。",
        "もみ合いと急反転。手数料が往復30〜50bpsかかる点。",
    ),
    Strategy(
        "S5_ROTATION", "セクターローテーション", "etf", C.SECTOR_ETFS + ["SPY"], None,
        {"top_n": [2, 3], "mom": [63, 126], "hold": [5, 10], "stop_k": [2.0, 3.0]},
        "セクター間の資金移動はゆっくり進むので、強いセクターは続きやすい。",
        "リバランスの遅い機関投資家の資金、出遅れセクターを持ち続ける投資家。",
        "金曜の終値で{mom}日騰落率の上位{top_n}セクター、SPYが200日線より上、スコア7以上。翌営業日、終値+0.25ATRの指値。",
        "{hold}本で手仕舞い。",
        "指値-{stop_k}ATR。",
        "主導セクターが急に入れ替わる局面。",
    ),
    Strategy(
        "S6_VOL", "ボラティリティ局面の転換", "etf", ["SPY", "QQQ", "IWM"], vol_regime,
        {"vix_th": [20.0, 25.0, 30.0], "stop_k": [2.0, 3.0], "max_hold": [5, 10]},
        "恐怖の後にボラティリティが低下し始めると、ヘッジの解消とリスク資産の買い戻しが起きる。",
        "高値でヘッジを買った投資家、ボラティリティ低下で買い戻しを迫られる参加者。",
        "VIXが過去10日に{vix_th}以上をつけ、当日に10日平均を下抜け、終値が20日線より上、スコア7以上。翌日、終値+0.25ATRの指値。",
        "{max_hold}本で手仕舞い。",
        "指値-{stop_k}ATR。",
        "VIXが何度も再上昇する長期の弱気相場（2022年型）。",
    ),
]


def _rotation_fn(ctx: Context, syms: list[str], p: dict) -> dict[str, pd.DataFrame]:
    return sector_rotation(ctx, [s for s in syms if s != "SPY"], p)


for _s in STRATEGIES:
    if _s.id == "S5_ROTATION":
        _s.fn = _rotation_fn

# データがなく今夜はテストできない仮説。結果を作らず、理由を記録する。
BLOCKED_HYPOTHESES = [
    {"id": "S2_NEWS_MOM", "name": "ニュース後モメンタム", "reason": "銘柄別の過去ニュースのデータがない。Alpacaのキーが入ったら、ニュースAPIの提供範囲を確認して再検討する。"},
]
