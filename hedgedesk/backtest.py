"""ポートフォリオ単位のシミュレータ。Desk Rules と同じ条件で売買を再現する。

時間の流れ（1日の中の順序）:
  1. 寄付き: 前日の手仕舞い予約を実行。窓開けでストップを割っていれば始値で約定。
  2. 取引中: ストップ判定 → 前日に出した指値の判定（有効期限は1日）。
     指値は「安値が指値を下回った」ときだけ約定する。触れただけでは約定としない。
     同じ足で指値とストップの両方に届いたら、損失側（約定後にストップ）とみなす。
  3. 大引け: 評価、キルスイッチ判定、翌日の注文作成。

シグナルは当日の終値までの情報で作り、最短でも翌日の足で約定する。
"""
#
# 【位置付け】research.evaluate が、全パラメータ・ウォークフォワード・ホールドアウト・ストレスの各期間でこの simulate を呼ぶ。
#   本番の Risk Officer（risk.size）と同じ数量計算・リスク上限・キルスイッチ条件を再現している。
# 【主な依存先】numpy、pandas、config（資金・リスク上限・保有本数・キルスイッチ・コスト）。
# 【実装方針】銘柄×日付の numpy 行列にして日付ごとにループする。銘柄ごとの独立計算ではなく、
#   資金とリスク上限を全銘柄で共有するポートフォリオとして再現するため。
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import config as C


@dataclass
class Costs:
    """1回の約定にかかるコストの設定（いずれも bps = 1万分の1）。

    Attributes:
        slip_bps: スリッページ。買いは高く、売りは安く約定したとみなす。
        entry_fee_bps: 新規（買い）の手数料。
        exit_fee_bps: 手仕舞い（売り）の手数料。
    """
    slip_bps: float = C.SLIP_BPS
    entry_fee_bps: float = C.EQUITY_FEE_BPS
    exit_fee_bps: float = C.EQUITY_FEE_BPS

    def doubled_slip(self) -> "Costs":
        """スリッページだけを2倍にしたコスト（ストレステスト用）。手数料はそのまま。"""
        return Costs(self.slip_bps * 2, self.entry_fee_bps, self.exit_fee_bps)


def costs_for(asset_class: str) -> Costs:
    """資産クラスに応じたコスト設定を返す。株・ETF はスリッページのみ、暗号資産は手数料も含む。"""
    if asset_class == "crypto":
        # 指値の新規はメイカー、ストップや期限切れの手仕舞いはテイカーとみなす
        return Costs(C.SLIP_BPS, C.CRYPTO_MAKER_BPS, C.CRYPTO_TAKER_BPS)
    return Costs()


@dataclass
class Result:
    """simulate の結果。

    Attributes:
        equity: 日次の大引け時点の資産（開始日から最終日まで）。
        trades: TRADE_COLUMNS の列を持つトレード一覧（決済済みのみ）。
        kills: キルスイッチが作動した日と理由のリスト。
    """
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


# トレード一覧の列。entry / exit はコスト（スリッページ）込みの約定価格、pnl は手数料込みの損益、
# risk は約定時点のリスク額（数量 × (約定価格 − ストップ)）、r = pnl ÷ risk、bars は保有した足の本数。
# reason は手仕舞いの理由: time（最大保有）、signal（手仕舞いシグナル）、stop（ストップ）、stop_gap（窓開けでストップ割れ）、
# stop_same_bar（約定と同じ足でストップ）、window_end（検証期間の終わり）。
TRADE_COLUMNS = ["symbol", "signal_date", "entry_date", "entry", "stop", "qty", "exit_date", "exit", "reason", "pnl", "risk", "r", "bars"]


def _matrix(signals: dict[str, pd.DataFrame], col: str, index: pd.DatetimeIndex, symbols: list[str], fill) -> np.ndarray:
    """銘柄ごとのシグナル列を、共通の日付×銘柄の行列にそろえる。

    Args:
        signals: 銘柄ごとのシグナル。col: 取り出す列名。index: 共通の日付。symbols: 列の並び順。
        fill: None なら欠けた日を NaN のまま残し、列自体がない銘柄も NaN で埋める（limit / stop 用）。
            値を渡すと欠けた日をその値で埋める（entry / exit / rank 用）。この場合は列が必須。

    Returns:
        形が (日付数, 銘柄数) の float 行列。
    """
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
    # 引数:
    #   market: 銘柄ごとの日足（open / high / low / close）。signals: 銘柄ごとのシグナル（attrs["max_hold"] も使う）。
    #   start / end: 評価する期間（end が None なら最後まで）。start より前の足はシグナルの助走にだけ使われる。
    #   costs: コスト設定。lag: 注文が遅れる足の本数（ストレステスト用）。capital: 開始資金。
    #   fractional: 端数の数量を許すか（暗号資産）。apply_kill: キルスイッチを再現するか。
    # 戻り値: Result（資産曲線、トレード一覧、キルスイッチの作動記録）。期間に足がなければ空の Result。
    # 副作用: なし（入力の DataFrame は変更しない）。
    symbols = sorted(signals)
    index = pd.DatetimeIndex(sorted(set().union(*[market[s].index for s in symbols])))
    index = index[index <= pd.Timestamp(end)] if end else index
    if len(index) == 0 or not symbols:
        return Result(pd.Series(dtype=float), pd.DataFrame(columns=TRADE_COLUMNS))
    i0 = int(index.searchsorted(pd.Timestamp(start)))
    if i0 >= len(index):
        return Result(pd.Series(dtype=float), pd.DataFrame(columns=TRADE_COLUMNS))

    def price(col):
        # 全銘柄の価格を共通の日付に並べた行列。その日に足のない銘柄は NaN（休場・上場前）。
        return np.column_stack([market[s][col].reindex(index).to_numpy(dtype=float) for s in symbols])

    O, H, L, CL = price("open"), price("high"), price("low"), price("close")
    entry = _matrix(signals, "entry", index, symbols, 0.0) > 0
    # 大文字の O / H / L / CL は価格の行列（[日付, 銘柄]）。C は config の別名に使っているので、終値の行列は CL と呼んでいる。
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
    # bps を割合に直す（5bps → 0.0005）。
    fee_in = costs.entry_fee_bps / 1e4
    fee_out = costs.exit_fee_bps / 1e4
    cash = capital
    equity_prev = capital
    positions: dict[int, dict] = {}
    # positions: 列番号 j → 保有中の建玉。pending: 前日の大引けに出した翌日用の指値注文。
    # losses_in_row: 連敗数。halt_until: キルスイッチで新規を止めている間の、再開する日の番号。curve: 日々の資産。
    pending: list[dict] = []
    trades: list[dict] = []
    kills: list[dict] = []
    losses_in_row = 0
    halt_until = -1
    curve = []

    def close_pos(j: int, i: int, px: float, reason: str):
        """銘柄 j の建玉を i 日目に価格 px で決済し、トレード一覧に記録する。

        スリッページを引いた価格で売り、売りの手数料も引く。pnl は買いの手数料込みの取得額 cost との差。
        副作用: cash と losses_in_row を更新し（nonlocal）、positions から取り除き、trades に追加する。
        """
        nonlocal cash, losses_in_row
        p = positions.pop(j)
        fill = px * (1 - slip)
        cash += p["qty"] * fill * (1 - fee_out)
        pnl = p["qty"] * fill * (1 - fee_out) - p["cost"]
        losses_in_row = losses_in_row + 1 if pnl < 0 else 0
        # 損益0（引き分け）は負けに数えず、連敗をリセットする（本番の trader.consecutive_losses と同じ）。
        trades.append({
            "symbol": symbols[j], "signal_date": p["signal_date"], "entry_date": p["entry_date"], "entry": p["entry"],
            "stop": p["stop"], "qty": p["qty"], "exit_date": index[i], "exit": fill, "reason": reason, "pnl": pnl,
            "risk": p["risk"], "r": pnl / p["risk"], "bars": p["bars"],
        })

    for i in range(i0, len(index)):
        day_start_equity = equity_prev
        # 前日の大引けの資産。日次損失のキルスイッチの基準にする。
        # 1. 保有ポジションの処理
        for j in list(positions):
            p = positions[j]
            o, l = O[i, j], L[i, j]
            if np.isnan(o):
                continue  # この銘柄は今日の足がない
            if p["exit_next"]:
                # 前日に手仕舞いを予約した建玉は、寄付き（始値）で成行決済する。
                close_pos(j, i, o, p["exit_reason"])
            elif o <= p["stop"]:
                # 窓開けでストップより下から始まったら、ストップ価格ではなく始値で約定（ストップより悪い価格）。
                close_pos(j, i, o, "stop_gap")
            elif l <= p["stop"]:
                # 取引中に安値がストップに届いたら、ストップ価格で約定したとみなす。
                close_pos(j, i, p["stop"], "stop")

        # 2. 前日に出した指値の判定
        for od in sorted(pending, key=lambda x: (-x["rank"], x["j"])):
            # 資金とリスク枠には限りがあるので、rank の高い順（同点は銘柄の並び順）に約定を判定する。
            j = od["j"]
            o, l = O[i, j], L[i, j]
            if j in positions or np.isnan(o) or not (l < od["limit"]):
                # 安値が指値を「下回った」ときだけ約定。触れただけ（l == limit）では列の後ろに並んでいて約定しない可能性があるため。
                continue
            fill = min(o, od["limit"]) * (1 + slip)
            # 始値が指値より下で始まったら始値で約定する（指値の買いは、より安い価格なら即約定するため）。
            risk_per_unit = fill - od["stop"]
            # 実際の約定価格からストップまでの幅で数量を決める（指値からではない）。
            if risk_per_unit <= 0:
                continue
            open_risk = sum(p["risk"] for p in positions.values())
            budget = min(C.RISK_PER_TRADE * equity_prev, C.MAX_OPEN_RISK * equity_prev - open_risk)
            # risk.size と同じ予算の式。資金は前日の大引けの資産（当日の値動きを使うと先読みになる）。
            if budget <= 0:
                continue
            qty = min(budget / risk_per_unit, C.MAX_POSITION_NOTIONAL * equity_prev / fill, cash / (fill * (1 + fee_in)))
            # 本番の上限に加え、手元の現金を超えて買えない（信用取引なし）という条件も入れている。
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
                # 日足では約定とストップのどちらが先か分からないので、楽観しないよう「約定してからストップ」とみなす。
                # min() は保険。上で fill > stop（risk_per_unit > 0）を確認済みなので、実際は常にストップ価格で決済される。
                close_pos(j, i, min(od["stop"], fill), "stop_same_bar")
        pending = []

        # 3. 大引けの評価
        mark = cash
        for j, p in positions.items():
            c = CL[i, j]
            if np.isnan(c):
                # 今日の足がない銘柄は、直近の終値（なければ約定価格）で評価する。
                c = p.get("last_close", p["entry"])
            p["last_close"] = c
            mark += p["qty"] * c
        equity = mark
        curve.append(equity)
        equity_prev = equity

        # キルスイッチ（再開は5営業日後と仮定）
        if apply_kill and i >= halt_until:
            # 停止中（halt_until より前）は再判定しない。再開日に連敗数を0から数え直すため、作動時に losses_in_row を0にする。
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
            # 保有本数は、その銘柄に足がある日だけ数える（休場日を保有日に数えない）。
            if np.isnan(CL[i, j]):
                continue
            p["bars"] += 1
            if p["bars"] >= p["max_hold"]:
                # 最大保有に達したら、シグナルにかかわらず翌日の寄付きで手仕舞う。
                p["exit_next"], p["exit_reason"] = True, "time"
            elif exit_[i, j] and p["bars"] >= C.MIN_HOLD_BARS:
                p["exit_next"], p["exit_reason"] = True, "signal"

        # 翌日の注文作成
        if i >= halt_until:
            # キルスイッチで停止中は新規の注文を出さない（手仕舞いとストップは続ける）。
            for j in np.flatnonzero(entry[i]):
                if j in positions or np.isnan(limit[i, j]) or np.isnan(stop[i, j]):
                    # すでに保有中の銘柄には追加で入らない（ナンピン・買い増しなし）。
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
    # 全建玉を決済した後の最終資産に差し替える（最終日の評価額ではなく、決済後の現金で終える）。
    # こうすると最終資産 = 開始資金 + 全トレードの損益 が厳密に一致する（test_equity_reconciles_with_trades）。
    eq = pd.Series(curve, index=index[i0:], name="equity")
    return Result(eq, pd.DataFrame(trades, columns=TRADE_COLUMNS), kills)
