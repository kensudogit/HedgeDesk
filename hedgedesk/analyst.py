"""Chart Analyst: 確定した足でチャンピオン戦略のシグナルを判定し、計画を作る。

判断に使うのはAlpaca/Yahooの価格データで、TradingViewのデータは使わない（規約上の理由）。
"""
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

PLANS = C.STATE / "plans"


def load_champion() -> dict | None:
    return json.loads(CHAMPION.read_text(encoding="utf-8")) if CHAMPION.exists() else None


def strategy_by_id(sid: str):
    return next(s for s in STRATEGIES if s.id == sid)


def make_plans(ctx: Context, champion: dict, now: datetime | None = None) -> list[dict]:
    """最新の確定足でシグナルが出た銘柄の計画を作る。"""
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
            continue
        bar = ctx.market[sym].iloc[-1]
        ed = (ctx.earnings or {}).get(sym)
        blocked = bool(earnings_block(sig.index, ed["reaction"] if ed is not None else None).iloc[-1]) if st.asset_class == "stock" else False
        plan = {
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
    now = now or datetime.now(timezone.utc)
    champ = load_champion()
    if not champ:
        return {"status": "no_champion"}
    if champ["asset_class"] not in asset_filter:
        return {"status": "other_asset_class", "champion": champ["id"]}
    ctx, _ = build_context(refresh=True, now=now)
    # 足の鮮度を確認する。古い足でシグナルを作らない。
    if champ["asset_class"] == "crypto":
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
    st = strategy_by_id(rec["strategy"])
    sig = st.signals(ctx, champion["params"]).get(rec["symbol"])
    if sig is None or sig.empty:
        return 0, False
    opened = pd.Timestamp(rec["opened_utc"]).tz_localize(None).normalize()
    held = int((sig.index >= opened).sum())
    return held, bool(sig["exit"].iloc[-1] > 0)
