"""Risk Officer: 計画を機械的に審査する。拒否は最終決定で、他のエージェントは覆せない。

判断できない情報（決算日不明、イベント未確認など）は、すべて拒否側に倒す。
"""
#
# 【役割】Chart Analyst が作った売買計画を Desk Rules に照らして承認・拒否し、承認分の数量を決める。
#   キルスイッチ（state/kill.json）の作動・状態確認もここが持つ。
# 【位置付け】analyst → risk.review → trader.submit の間に必ず入る関門。trader は承認ハッシュを再計算し、
#   ここで承認された中身から変わっていない計画しか発注しない。
# 【主な依存先】config（上限値・禁止銘柄）、events（ローンチ前のイベント確認）、journal（拒否・受け渡しの記録）。
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

from . import config as C
from . import events, journal

# キルスイッチの状態ファイル。存在すれば作動中。import 時のパスで固定されるので、テストでは monkeypatch で差し替える。
KILL_FILE = C.STATE / "kill.json"


def kill_status() -> dict:
    """キルスイッチの状態を返す。

    Returns:
        作動中なら {"active": True, "reason": ..., "ts": ...}、未作動なら {"active": False}。

    Raises:
        json.JSONDecodeError: 状態ファイルが壊れている場合。黙って未作動扱いにせず、止まる側に倒す。
    """
    if KILL_FILE.exists():
        return json.loads(KILL_FILE.read_text(encoding="utf-8"))
    return {"active": False}


def fire_kill(reason: str) -> None:
    """キルスイッチを入れる。解除は `python -m hedgedesk reset-kill` をオーナーが実行したときだけ。"""
    # 引数: reason は作動理由（ブリーフと画面にそのまま表示される）。
    # 副作用: state/kill.json を作り、ジャーナルに kill_switch を記録する。
    # すでに作動中なら何もしない。最初の理由を上書きせず、ジャーナルにも重複して記録しないため。
    if kill_status().get("active"):
        return
    C.STATE.mkdir(parents=True, exist_ok=True)
    rec = {"active": True, "reason": reason, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    KILL_FILE.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    journal.log("kill_switch", "Risk Officer", reason=reason)


def approval_hash(plan: dict) -> str:
    """承認後に中身が変わっていないことを Trader が確認するためのハッシュ。"""
    # 引数: plan は qty を含む承認済みの計画。
    # 戻り値: 発注に関わる8項目だけを sort_keys で並べた JSON の SHA-256（16進文字列）。
    # 数量や価格を承認後に書き換えるとハッシュが変わり、trader.submit が発注を拒否する。
    # score や signal_date などの補足情報は対象外なので、表示用の項目を足してもハッシュは変わらない。
    keys = ("plan_id", "symbol", "side", "qty", "limit", "stop", "asset_class", "strategy")
    return hashlib.sha256(json.dumps({k: plan[k] for k in keys}, sort_keys=True).encode()).hexdigest()


def size(plan: dict, equity: float, open_risk: float, fractional: bool) -> float:
    """ATRストップ幅からの数量。リスクとサイズの上限をすべて満たす最大値。"""
    # 引数:
    #   plan: limit（指値）と stop を持つ計画。equity: 現在の資金。open_risk: 保有中・承認済みのリスク合計（ドル）。
    #   fractional: 端数の数量を許すか（暗号資産は True、株・ETF は整数株）。
    # 戻り値: 数量。ストップ幅が0以下、またはリスクの空きがなければ 0.0。
    # 計算: 予算 = min(資金×1トレードのリスク, 資金×合計リスク上限 − 既存リスク)
    #       数量 = min(予算 ÷ ストップ幅, 資金×1銘柄の上限 ÷ 指値)
    # 端数は切り捨てる（四捨五入するとリスク上限をわずかに超えることがあるため）。暗号資産は小数6桁で切り捨て。
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
    # 引数:
    #   plans: Chart Analyst の計画のリスト。equity / open_risk: 審査時点の資金と既存リスク。
    #   held: 保有中（発注中を含む）の銘柄。champion: チャンピオン情報。未ローンチならイベントの関門を通す。
    #   now: 判定時刻（テストで固定するため。省略時は現在の UTC）。
    # 戻り値: (承認リスト, 拒否リスト)。拒否には veto_reasons（理由の文字列リスト）が付く。
    # 副作用: 拒否ごとに veto、承認ごとに handoff をジャーナルに記録する。
    # 拒否理由は最初の1つで止めず、すべて集める（オーナーが理由をまとめて確認できるように）。
    # 計画は順番に審査し、承認した分のリスクと銘柄を open_risk / held に足していくので、
    # 同じ回の後ろの計画ほど合計リスクの上限に当たりやすい（先着順）。
    now = now or datetime.now(timezone.utc)
    approved, vetoed = [], []
    kill = kill_status()
    gate_ok, gate_reasons = (True, [])
    if champion and not champion.get("launched"):
        # イベント確認はネットワークアクセスを伴うので、未ローンチのチャンピオンがいるときだけ1回だけ行う。
        # ローンチ済みの戦略は、イベントの直前でも既存ルールどおりに続ける（新規ローンチだけを止める規則）。
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
                # 株価と売買代金の基準は株・ETF だけに適用する（暗号資産は価格水準の意味が違うため対象外）。
            if plan.get("price", 0) < C.MIN_PRICE:
                reasons.append(f"株価${plan.get('price')}が${C.MIN_PRICE:.0f}未満")
            if plan.get("dollar_volume", 0) < C.MIN_DOLLAR_VOLUME:
                reasons.append("売買代金が基準未満")
        if plan["asset_class"] == "stock" and plan.get("earnings_blocked", True):
                # キーがない（決算判定をしていない）計画も拒否する。判断できない情報は拒否側に倒す方針。
            reasons.append("決算跨ぎの可能性がある、または決算日が不明")
        fractional = plan["asset_class"] == "crypto"
        qty = size(plan, equity, open_risk, fractional) if plan.get("stop") else 0.0
            # ストップがない計画は上で拒否済みだが、size() の引き算で TypeError にならないよう数量計算も飛ばす。
        if qty <= 0 and not reasons:
            reasons.append("リスク上限により数量が0")
        if reasons:
            vetoed.append({**plan, "veto_reasons": reasons})
            journal.log("veto", "Risk Officer", TICKER=sym, IDEA=plan["strategy"], EVIDENCE=reasons, NEXT_STEP="計画を破棄")
            continue
        out = {**plan, "qty": qty}
        out["approval"] = approval_hash(out)
            # ハッシュは qty を入れた後で計算する（数量の改ざんも検出できるように）。
        open_risk += qty * (plan["limit"] - plan["stop"])
        held = held | {sym}
            # 呼び出し元の集合を書き換えないよう、新しい集合を作って差し替える。
        approved.append(out)
        journal.handoff("Risk Officer", "Trader", sym, plan["strategy"], f"risk=${qty * (plan['limit'] - plan['stop']):.2f}, score={plan['score']}", "指値とストップで発注")
    return approved, vetoed
