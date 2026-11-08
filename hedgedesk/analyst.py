"""Chart Analyst: 確定した足でチャンピオン戦略のシグナルを判定し、計画を作る。

判断に使うのはAlpaca/Yahooの価格データで、TradingViewのデータは使わない（規約上の理由）。
"""
#
# 【役割】チャンピオン戦略（research が選んだ合格戦略）のシグナルを最新の確定足で判定し、売買計画を作る。
#   保有中の銘柄について、保有本数と手仕舞いシグナルの判定も行う。
# 【位置付け】desk.job_signals から呼ばれ、作った計画は risk.review に渡る。発注はしない。
# 【主な依存先】research（CHAMPION のパスと build_context）、strategies（戦略定義と決算判定）、
#   data.NY（ニューヨーク時間）、risk.fire_kill（データ停止時）、journal（受け渡しの記録）。
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from . import config as C
from . import journal
from .data import NY
from .research import CHAMPION, build_context
from .risk import fire_kill
from .strategies import STRATEGIES, Context, earnings_block

# 計画の保存先として用意されたパス（現時点のコードでは参照していない）。
PLANS = C.STATE / "plans"


def load_champion() -> dict | None:
    """state/champion.json を読む。チャンピオンがいなければ None（取引しない）。"""
    return json.loads(CHAMPION.read_text(encoding="utf-8")) if CHAMPION.exists() else None


def strategy_by_id(sid: str):
    """戦略 ID から Strategy を引く。

    Raises:
        StopIteration: 該当する ID がない場合（チャンピオンの ID が戦略一覧と食い違うのは異常なので止める）。
    """
    return next(s for s in STRATEGIES if s.id == sid)


def make_plans(ctx: Context, champion: dict, now: datetime | None = None) -> list[dict]:
    """最新の確定足でシグナルが出た銘柄の計画を作る。"""
    # 引数: ctx はシグナル計算用のデータ、champion はチャンピオン情報（id と params）、now は判定時刻。
    # 戻り値: 計画（dict）のリスト。qty は0のままで、数量は Risk Officer が決める。
    # 副作用: 計画ごとに Chart Analyst → Risk Officer の handoff をジャーナルに記録する。
    # 最新の足（iloc[-1]）だけを見る。過去の足のシグナルは期限切れ（バックテストの指値の有効期限1日に合わせる）。
    now = now or datetime.now(timezone.utc)
    st = strategy_by_id(champion["id"])
    signals = st.signals(ctx, champion["params"])
    plans = []
    for sym, sig in signals.items():
        if sig.empty:
            continue
        last = sig.iloc[-1]
        date = sig.index[-1]
        if last["entry"] <= 0 or pd.isna(last["limit"]) or pd.isna(last["stop"]):
            # シグナルなし、または指値・ストップが計算できない（ATR が NaN など）足は計画にしない。
            continue
        bar = ctx.market[sym].iloc[-1]
        ed = (ctx.earnings or {}).get(sym)
        # 個別株だけ決算跨ぎを判定する。最新の足が決算前の禁止期間に入っているか、決算日が不明なら True。
        # 最終的な拒否は risk.review が earnings_blocked を見て行う（判定と拒否の担当を分けている）。
        blocked = bool(earnings_block(sig.index, ed["reaction"] if ed is not None else None).iloc[-1]) if st.asset_class == "stock" else False
        plan = {
            # plan_id は戦略・銘柄・シグナル日から作るので、同じ日に同じ計画を作り直しても同じ ID になる。
            # Alpaca の client_order_id にも使われ、ブローカー側で二重発注が拒否される。
            "plan_id": f"{st.id}-{sym.replace('-', '')}-{date.strftime('%Y%m%d')}",
            "symbol": sym, "side": "buy", "asset_class": st.asset_class, "strategy": st.id, "signal_date": str(date.date()),
            "limit": round(float(last["limit"]), 2), "stop": round(float(last["stop"]), 2), "score": int(last["score"]),
            "max_hold": sig.attrs.get("max_hold", C.MAX_HOLD_BARS), "price": round(float(bar["close"]), 2),
            "dollar_volume": float(bar["dv_avg20"]), "earnings_blocked": blocked, "qty": 0.0,
        }
        plans.append(plan)
        journal.handoff("Chart Analyst", "Risk Officer", sym, st.id, f"score={plan['score']}, limit={plan['limit']}, stop={plan['stop']}", "Risk Officerの審査")
    return plans


def run(asset_filter: set[str], now: datetime | None = None) -> dict:
    """チャンピオンが対象の資産クラスならシグナルを判定する。"""
    # 引数: asset_filter は今回判定する資産クラス（株の時間帯なら {"etf", "stock"}、暗号資産なら {"crypto"}）。
    # 戻り値: status と、ok のときは champion / plans / ctx（手仕舞い判定で再利用する）を含む dict。
    #   no_champion / other_asset_class / stale_data / no_session のときは計画を作らない。
    # 副作用: build_context でデータを再取得する（refresh=True）。データが古ければキルスイッチを入れる。
    now = now or datetime.now(timezone.utc)
    champ = load_champion()
    if not champ:
        return {"status": "no_champion"}
    if champ["asset_class"] not in asset_filter:
        return {"status": "other_asset_class", "champion": champ["id"]}
    ctx, _ = build_context(refresh=True, now=now)
    # 足の鮮度を確認する。古い足でシグナルを作らない。
    if champ["asset_class"] == "crypto":
        # 暗号資産は毎日取引されるので、UTC の前日の足まであるのが正常。2日以上古ければ取得が止まっている。
        newest = max(ctx.market[s].index[-1].date() for s in C.CRYPTO)
        if (now.astimezone(timezone.utc).date() - newest).days > 1:
            fire_kill(f"データ停止: 暗号資産の最新足が {newest}")
            return {"status": "stale_data"}
    else:
        if ctx.market["SPY"].index[-1].date() != now.astimezone(NY).date():
            # 祝日か、データ提供の遅れ。今日の足がないのでシグナルは作らない。
            journal.log("no_session", "Chart Analyst", last_bar=str(ctx.market["SPY"].index[-1].date()))
            return {"status": "no_session"}
    plans = make_plans(ctx, champ, now)
    return {"status": "ok", "champion": champ, "plans": plans, "ctx": ctx}


def bars_held_and_exit(ctx: Context, rec: dict, champion: dict) -> tuple[int, bool]:
    """保有中の銘柄について、保有本数と手仕舞いシグナルを確定足で判定する。"""
    # 引数: rec は保有台帳（state/positions.json）の1件。opened_utc（約定を確認した時刻）を使う。
    # 戻り値: (約定日以降の確定足の本数, 最新の足で手仕舞いシグナルが出ているか)。シグナルがなければ (0, False)。
    # 注意: シグナルはチャンピオンの現在のパラメータで計算する。保有中にパラメータが変わると判定も変わる。
    st = strategy_by_id(rec["strategy"])
    sig = st.signals(ctx, champion["params"]).get(rec["symbol"])
    if sig is None or sig.empty:
        return 0, False
    opened = pd.Timestamp(rec["opened_utc"]).tz_localize(None).normalize()
    # 足のインデックスはタイムゾーンなしの日付なので、約定時刻もタイムゾーンを外して日付に丸めてから比較する。
    held = int((sig.index >= opened).sum())
    return held, bool(sig["exit"].iloc[-1] > 0)
