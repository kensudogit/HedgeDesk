"""Risk Officer: 計画を機械的に審査する。拒否は最終決定で、他のエージェントは覆せない。

判断できない情報（決算日不明、イベント未確認など）は、すべて拒否側に倒す。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

from . import config as C
from . import events, journal

KILL_FILE = C.STATE / "kill.json"


def kill_status() -> dict:
    if KILL_FILE.exists():
        return json.loads(KILL_FILE.read_text(encoding="utf-8"))
    return {"active": False}


def fire_kill(reason: str) -> None:
    """キルスイッチを入れる。解除は `python -m hedgedesk reset-kill` をオーナーが実行したときだけ。"""
    if kill_status().get("active"):
        return
    C.STATE.mkdir(parents=True, exist_ok=True)
    rec = {"active": True, "reason": reason, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    KILL_FILE.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    journal.log("kill_switch", "Risk Officer", reason=reason)


def approval_hash(plan: dict) -> str:
    """承認後に中身が変わっていないことを Trader が確認するためのハッシュ。"""
    keys = ("plan_id", "symbol", "side", "qty", "limit", "stop", "asset_class", "strategy")
    return hashlib.sha256(json.dumps({k: plan[k] for k in keys}, sort_keys=True).encode()).hexdigest()


def size(plan: dict, equity: float, open_risk: float, fractional: bool) -> float:
    """ATRストップ幅からの数量。リスクとサイズの上限をすべて満たす最大値。"""
    width = plan["limit"] - plan["stop"]
    if width <= 0:
        return 0.0
    budget = min(C.RISK_PER_TRADE * equity, C.MAX_OPEN_RISK * equity - open_risk)
    if budget <= 0:
        return 0.0
    qty = min(budget / width, C.MAX_POSITION_NOTIONAL * equity / plan["limit"])
    return round(math.floor(qty * 1e6) / 1e6, 6) if fractional else float(math.floor(qty))


def review(plans: list[dict], equity: float, open_risk: float, held: set[str], champion: dict | None, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """(承認, 拒否) を返す。承認された計画には数量と approval が付く。"""
    now = now or datetime.now(timezone.utc)
    approved, vetoed = [], []
    kill = kill_status()
    gate_ok, gate_reasons = (True, [])
    if champion and not champion.get("launched"):
        gate_ok, gate_reasons = events.launch_gate(now)
    universe = set(C.ETFS + C.STOCKS + C.CRYPTO)
    for plan in plans:
        reasons = []
        sym = plan["symbol"]
        if kill.get("active"):
            reasons.append(f"キルスイッチ作動中: {kill.get('reason')}")
        if not gate_ok:
            reasons += [f"ローンチ不可: {r}" for r in gate_reasons]
        if sym in C.BANNED or sym not in universe:
            reasons.append("Desk Rulesの対象外銘柄")
        if plan.get("side") != "buy":
            reasons.append("空売り禁止（買いのみ）")
        if not plan.get("stop") or plan["stop"] >= plan["limit"]:
            reasons.append("ストップがない、または指値より上")
        if plan.get("score", 0) < C.MIN_SETUP_SCORE:
            reasons.append(f"セットアップスコア{plan.get('score')}が7未満")
        if sym in held:
            reasons.append("すでに保有中")
        if plan["asset_class"] != "crypto":
            if plan.get("price", 0) < C.MIN_PRICE:
                reasons.append(f"株価${plan.get('price')}が${C.MIN_PRICE:.0f}未満")
            if plan.get("dollar_volume", 0) < C.MIN_DOLLAR_VOLUME:
                reasons.append("売買代金が基準未満")
        if plan["asset_class"] == "stock" and plan.get("earnings_blocked", True):
            reasons.append("決算跨ぎの可能性がある、または決算日が不明")
        fractional = plan["asset_class"] == "crypto"
        qty = size(plan, equity, open_risk, fractional) if plan.get("stop") else 0.0
        if qty <= 0 and not reasons:
            reasons.append("リスク上限により数量が0")
        if reasons:
            vetoed.append({**plan, "veto_reasons": reasons})
            journal.log("veto", "Risk Officer", TICKER=sym, IDEA=plan["strategy"], EVIDENCE=reasons, NEXT_STEP="計画を破棄")
            continue
        out = {**plan, "qty": qty}
        out["approval"] = approval_hash(out)
        open_risk += qty * (plan["limit"] - plan["stop"])
        held = held | {sym}
        approved.append(out)
        journal.handoff("Risk Officer", "Trader", sym, plan["strategy"], f"risk=${qty * (plan['limit'] - plan['stop']):.2f}, score={plan['score']}", "指値とストップで発注")
    return approved, vetoed
