"""事前登録する戦略仮説。パラメータの組み合わせはすべて試行数に数える。

各戦略は、当日の終値までの情報で entry / exit / limit / stop / rank を作る。
全戦略に共通で、Chart Analyst のスコア7点以上と流動性の条件を課す。
個別株には「決算跨ぎ禁止」（今後11営業日に決算がある、または日付不明なら見送り）を課す。
"""
#
# 【役割】検証する戦略（仮説）とパラメータの格子を、結果を見る前に固定して登録する（事前登録）。
#   各戦略関数は enrich 済みの日足から、銘柄ごとのシグナル（entry / exit / limit / stop / rank / score）を作る。
# 【位置付け】research が全戦略・全パラメータを検証し、analyst がチャンピオンの戦略だけを本番の確定足で動かす。
#   バックテストと本番で同じ関数を使うので、本番だけの条件が紛れ込まない。
# 【主な依存先】indicators.enrich、config（スコア・流動性・保有本数の基準と銘柄ユニバース）、numpy、pandas。
# 【シグナルの約束】entry=1 の足の終値で判定し、翌日に limit の指値で入る。stop は limit − stop_k×ATR。
#   exit=1 は「翌日の寄付きで手仕舞う」予約。どの列も当日の終値までの情報だけで計算する。
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
    """取得した生の日足に共通の指標列を加える。空のデータと VIX（売買対象ではない）は除く。"""
    return {s: enrich(df) for s, df in raw.items() if len(df) > 0 and s != C.VIX}


def earnings_block(index: pd.DatetimeIndex, reactions: pd.Series | None, horizon: int = C.MAX_HOLD_BARS + 1) -> pd.Series:
    """今後 horizon 営業日に決算反応日があれば True。決算データがなければ全日 True。"""
    # 引数:
    #   index: 判定する日付（銘柄の日足のインデックス）。reactions: 決算反応日の一覧（None なら決算データなし）。
    #   horizon: 何本先までの決算を避けるか。既定は最大保有本数 + 1（保有中に決算を迎えないように1本の余裕を持たせる）。
    # 戻り値: 日付ごとの真偽値（True = 新規禁止）。
    # 反応日そのものは塞がない。決算後ドリフト戦略（pead）は反応日の終値で判定して入るため。
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
    """各戦略の条件に、全戦略共通の関門を加えてシグナルの DataFrame にまとめる。

    Args:
        df: enrich 済みの日足。entry / exit_: 戦略固有の新規・手仕舞いの条件（真偽値、NaN は False）。
        limit: 翌日の指値。stop_k: ストップ幅（ATR の何倍か）。max_hold: 最大保有本数（attrs に入れる）。
        rank: 同じ日に複数のシグナルが出たときの優先順位（大きいほど優先）。省略時はスコア。

    Returns:
        entry / exit / limit / stop / rank / score 列と attrs["max_hold"] を持つ DataFrame。
        entry は共通関門（スコア7以上、20日平均売買代金が基準以上）を満たす足だけ 1.0 になる。
    """
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
    """個別株のシグナルから、決算前の禁止期間（と決算日が不明な期間）の新規を消す。

    副作用: 受け取った sig の entry 列をその場で書き換え、同じオブジェクトを返す。
    """
    ed = (ctx.earnings or {}).get(sym)
    block = earnings_block(sig.index, ed["reaction"] if ed is not None else None)
    sig["entry"] = sig["entry"].where(~block, 0.0)
    return sig


def pead(ctx: Context, syms: list[str], p: dict) -> dict[str, pd.DataFrame]:
    """決算後ドリフト: 良い決算に市場が反応した日の終値で判定し、翌日に指値で入る。"""
    # 引数: p は min_surprise（EPS サプライズの下限%）、min_react（反応日の上昇率の下限）、vol_mult（出来高倍率）、
    #   stop_k、max_hold。決算データのない銘柄はシグナルを作らない。
    out = {}
    for s in syms:
        df = ctx.market[s]
        ed = (ctx.earnings or {}).get(s)
        if ed is None:
            continue
        hit = pd.Series(False, index=df.index)
        for _, row in ed.iterrows():
            pos = df.index.searchsorted(row["reaction"])
            # 反応日以降で最初の足（祝日なら翌営業日）に印を付ける。
            if pos < len(df) and pd.notna(row["surprise"]) and row["surprise"] >= p["min_surprise"]:
                hit.iloc[pos] = True
        day_ret = df["close"] / df["close"].shift(1) - 1
        entry = hit & (day_ret >= p["min_react"]) & (df["volume"] >= p["vol_mult"] * df["vol_avg20"].shift(1)) & (df["close"] > df["sma50"])
        # 出来高の比較は前日までの20日平均と行う。当日の出来高を平均に含めると、急増が自分自身で薄まるため。
        limit = df["close"] + 0.25 * df["atr"]
        # 翌日の指値は終値より 0.25ATR 上。強い銘柄は押し目を作らずに上がることが多いので、少し上まで買いにいく。
        sig = _finish(df, entry, df["close"] < df["sma20"], limit, p["stop_k"], p["max_hold"], rank=day_ret)
        out[s] = _with_earnings(ctx, s, sig)
    return out


def mean_reversion(ctx: Context, syms: list[str], p: dict) -> dict[str, pd.DataFrame]:
    """上昇基調の中での売られすぎの反発。"""
    # 引数: p は rsi_th（RSI(2) の上限）、stop_k、max_hold。
    # 終値が200日線より上（上昇基調）で RSI(2) が極端に低い日に、翌日その終値の指値で買う。
    # 手仕舞いは終値が5日線を上回ったとき。rank は RSI(2) が低いほど優先（−rsi2）。
    out = {}
    for s in syms:
        df = ctx.market[s]
        entry = (df["close"] > df["sma200"]) & (df["rsi2"] < p["rsi_th"])
        out[s] = _finish(df, entry, df["close"] > df["sma5"], df["close"], p["stop_k"], p["max_hold"], rank=-df["rsi2"])
    return out


def breakout(ctx: Context, syms: list[str], p: dict) -> dict[str, pd.DataFrame]:
    """出来高を伴う高値ブレイク。"""
    # 引数: p は lookback（高値の期間）、vol_mult、stop_k、exit_sma（手仕舞いの移動平均の期間）、max_hold。
    # 決算跨ぎ禁止は個別株にだけ適用する（ETF・暗号資産に決算はない）。rank は出来高倍率が大きいほど優先。
    out = {}
    for s in syms:
        df = ctx.market[s]
        prior_high = df["high"].shift(1).rolling(p["lookback"]).max()
        # 当日を含まない「前日までの」高値。当日の高値を含めると、終値（≦当日高値）がそれを超えることはない。
        entry = (df["close"] > prior_high) & (df["volume"] >= p["vol_mult"] * df["vol_avg20"].shift(1)) & (df["close"] > df["sma200"])
        limit = df["close"] + 0.25 * df["atr"]
        exit_ = df["close"] < df[f"sma{p['exit_sma']}"]
        sig = _finish(df, entry, exit_, limit, p["stop_k"], p["max_hold"], rank=df["volume"] / df["vol_avg20"])
        out[s] = _with_earnings(ctx, s, sig) if s in C.STOCKS else sig
    return out


def sector_rotation(ctx: Context, syms: list[str], p: dict) -> dict[str, pd.DataFrame]:
    """週末に強いセクターの上位を買う。市場全体が上昇基調のときだけ。"""
    # 引数: syms はセクター ETF（SPY は含めない。SPY は市場全体の判定にだけ使う）。
    #   p は top_n（上位何本か）、mom（騰落率の期間）、hold（保有本数）、stop_k。
    # 手仕舞いシグナルはなく、hold 本で時間切れの手仕舞いだけ（exit は全日 False）。
    closes = pd.DataFrame({s: ctx.market[s]["close"] for s in syms})
    mom = closes / closes.shift(p["mom"]) - 1
    ranks = mom.rank(axis=1, ascending=False)
    # 同じ日のセクター間で騰落率に順位を付ける（1位が最も強い）。
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
    # 引数: p は vix_th（過去10日に VIX がこの値以上をつけたか）、stop_k、max_hold。VIX のデータがなければ何もしない。
    if ctx.vix is None:
        return {}
    vix = ctx.vix
    sma = vix.rolling(10).mean()
    was_high = (vix >= p["vix_th"]).rolling(10).max().fillna(0) > 0
    calm = (vix < sma) & (vix.shift(1) >= sma.shift(1)) & was_high
    # 「当日に10日平均を下抜けた」1日だけを捉える（前日は平均以上、当日は平均未満）。下抜けた後の毎日ではない。
    out = {}
    for s in syms:
        df = ctx.market[s]
        entry = calm.reindex(df.index).fillna(False) & (df["close"] > df["sma20"])
        limit = df["close"] + 0.25 * df["atr"]
        out[s] = _finish(df, entry, pd.Series(False, index=df.index), limit, p["stop_k"], p["max_hold"])
    return out


@dataclass
class Strategy:
    """事前登録した1つの戦略仮説。

    Attributes:
        id / name / asset_class: 識別子、表示名、資産クラス（"stock" / "etf" / "crypto"）。
        symbols: 対象銘柄。fn: シグナル関数 (ctx, syms, params) → {銘柄: シグナル}。
        grid: パラメータ名 → 試す値のリスト。すべての組み合わせを試行として数える。
        thesis / other_side / breaks_when: 仮説、取引の相手方（誰から利益を得るのか）、効かなくなる局面。
        entry_rule / exit_rule / stop_rule: レポート用のルール文。{パラメータ名} を選ばれた値で埋める。
    """
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
        """格子のすべての組み合わせを dict のリストで返す（並び順は grid の定義順で固定）。"""
        keys = list(self.grid)
        return [dict(zip(keys, v)) for v in itertools.product(*self.grid.values())]

    def signals(self, ctx: Context, p: dict) -> dict[str, pd.DataFrame]:
        """パラメータ p でシグナルを計算する。データのない銘柄は対象から外す。"""
        return self.fn(ctx, [s for s in self.symbols if s in ctx.market], p)


def trial_id(strategy_id: str, params: dict) -> str:
    """戦略とパラメータから試行 ID を作る。

    sort_keys で JSON にしてから SHA-1 を取るので、パラメータの並び順が違っても同じ ID になる。
    試行の登録簿（trial_registry.json）で同じ試行を二重に数えないために使う。
    """
    body = json.dumps({"s": strategy_id, "p": params}, sort_keys=True)
    return strategy_id + ":" + hashlib.sha1(body.encode()).hexdigest()[:10]


# ブレイク戦略3種で共通の新規ルール文。
_BREAK_ENTRY = "終値が前日までの{lookback}日高値を超え、出来高が20日平均の{vol_mult}倍以上、終値が200日線より上、スコア7以上。翌日、終値+0.25ATRの指値。"
# 事前登録した戦略の一覧。引数の並びは Strategy の属性順:
#   (id, 名前, 資産クラス, 銘柄, シグナル関数, パラメータ格子, 仮説, 相手方, 新規ルール, 手仕舞いルール, ストップルール, 効かなくなる局面)
# 格子を広げるほど試行数が増え、デフレートシャープの基準が厳しくなる（結果を見て格子を足すと不利になる仕組み）。
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
    # S5 はここで fn=None として登録し、定義の後で _rotation_fn に差し替える（下のループ）。
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
    """セクターローテーション用の入口。銘柄一覧から SPY を除いてから sector_rotation に渡す。"""
    return sector_rotation(ctx, [s for s in syms if s != "SPY"], p)


# S5_ROTATION の fn を後から設定する。リスト定義の中では _rotation_fn がまだ定義されていないため。
for _s in STRATEGIES:
    if _s.id == "S5_ROTATION":
        _s.fn = _rotation_fn

# データがなく今夜はテストできない仮説。結果を作らず、理由を記録する。
BLOCKED_HYPOTHESES = [
    {"id": "S2_NEWS_MOM", "name": "ニュース後モメンタム", "reason": "銘柄別の過去ニュースのデータがない。Alpacaのキーが入ったら、ニュースAPIの提供範囲を確認して再検討する。"},
]
