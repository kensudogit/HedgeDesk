"""フォワード記録: 審査の合否に関係なく、全戦略を「登録した日より後のデータだけ」で追跡する。

バックテストの成績は、どれだけ注意しても過去を見て作ったものになる。登録した日のパラメータを固定し、
その後に出た足だけで同じシミュレータを回せば、本物のアウトオブサンプルの成績が積み上がる。
発注はしない（Desk Rules 第3節9: 合格0本ならペーパーでも取引しない）。記録と比較だけを行う。
"""
#
# 【役割】state/forward_registry.json に「試行 ID → 戦略・パラメータ・記録開始日」を一度だけ登録し（書き換えない）、
#   夜間リサーチのたびに backtest.simulate を記録開始日から最新の足まで回して state/forward.json に保存する。
# 【位置付け】research.run_research の最後に呼ばれる。売買・審査・チャンピオンの選択には使わない。画面とブリーフの参考表示だけ。
# 【整合の判定】Desk Rules 第9節の定義に合わせる: フォワードの平均R が、バックテスト（OOS）のトレード分布から
#   同じ件数を引いたときの平均の90%範囲（平均 ± 1.645 × 標準偏差 ÷ √件数）に入っているか。件数が少ない間は判定しない。
# 【先読みを避ける工夫】
#   - パラメータは登録時に固定する。後の調査で最適なパラメータが変わったら、別の試行として新しく登録する。
#   - simulate は開始日の前日に出たシグナルを持ち越さない（開始日の大引けのシグナルから注文が始まる）。
#     登録は開始日の大引けより前に行われるので、登録時点で知り得なかった足を選択に使うことはない。
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from . import config as C
from .backtest import costs_for, simulate
from .strategies import STRATEGIES, Context, trial_id

MIN_TRADES_FOR_CHECK = 10   # これ未満のトレード数では整合を判定しない
Z90 = 1.645                  # 90%範囲（片側5%ずつ）


def _registry_path():
    return C.STATE / "forward_registry.json"


def _out_path():
    return C.STATE / "forward.json"


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def enroll(tested: list[dict], now: datetime) -> dict:
    """TESTED の戦略の現在のパラメータを、まだ登録されていなければ今日の日付で登録する。

    Returns:
        更新後の登録簿。既存の登録は開始日もパラメータも変えない。
    """
    reg = _read(_registry_path(), {})
    start = now.astimezone(timezone.utc).date().isoformat()
    changed = False
    for r in tested:
        if r.get("status") != "TESTED" or not r.get("final_params"):
            continue
        tid = trial_id(r["id"], r["final_params"])
        if tid not in reg:
            reg[tid] = {"strategy": r["id"], "params": r["final_params"], "start": start}
            changed = True
    if changed:
        _registry_path().parent.mkdir(parents=True, exist_ok=True)
        _registry_path().write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    return reg


def consistency(fwd_r: list[float], bt_mean: float | None, bt_std: float | None) -> dict:
    """フォワードの平均R がバックテストの90%範囲に入っているか。件数不足や比較材料がなければ consistent は None。"""
    n = len(fwd_r)
    mean = round(sum(fwd_r) / n, 4) if n else None
    if bt_mean is None or bt_std is None or not n:
        return {"n": n, "mean_r": mean, "range90": None, "consistent": None}
    half = Z90 * bt_std / math.sqrt(n)
    lo, hi = round(bt_mean - half, 4), round(bt_mean + half, 4)
    ok = (lo <= mean <= hi) if n >= MIN_TRADES_FOR_CHECK else None
    return {"n": n, "mean_r": mean, "range90": [lo, hi], "consistent": ok}


def _today_signals(st, sig: dict, ctx: Context) -> list[dict]:
    """最新の確定足でエントリーシグナルが出ている銘柄（指値・ストップ付き）。発注はしない、見るだけの一覧。"""
    out = []
    for sym, df in sig.items():
        if df.empty or sym not in ctx.market or ctx.market[sym].empty:
            continue
        if df.index[-1] != ctx.market[sym].index[-1]:
            continue
        last = df.iloc[-1]
        if last["entry"] > 0 and math.isfinite(float(last["limit"])) and math.isfinite(float(last["stop"])):
            out.append({"symbol": sym, "date": str(df.index[-1].date()), "limit": round(float(last["limit"]), 2), "stop": round(float(last["stop"]), 2)})
    return out


def update(tested: list[dict], ctx: Context, now: datetime | None = None) -> dict:
    """登録し、全登録を最新の足まで回して state/forward.json に保存する。

    Args:
        tested: research の検証結果（TESTED のもの。final_params と out_of_sample、oos_r を使う）。
        ctx: research と同じデータ。

    Returns:
        {"as_of_utc", "rows": [...]}。rows は現在のパラメータの登録を戦略ごとに1行。
        古いパラメータの登録は登録簿に残り、追跡も続く（history に件数だけ出す）。
    """
    now = now or datetime.now(timezone.utc)
    reg = enroll(tested, now)
    by_id = {s.id: s for s in STRATEGIES}
    current = {r["id"]: r for r in tested if r.get("status") == "TESTED"}
    rows, history = [], []
    for tid, rec in sorted(reg.items(), key=lambda kv: kv[1]["start"]):
        st = by_id.get(rec["strategy"])
        if st is None:
            continue
        sig = st.signals(ctx, rec["params"])
        if not sig:
            continue
        res = simulate(ctx.market, sig, rec["start"], None, costs_for(st.asset_class), fractional=st.asset_class == "crypto")
        tr = res.trades
        rs = [float(x) for x in tr["r"]] if len(tr) else []
        bt = current.get(st.id) or {}
        dist = bt.get("oos_r") or {}
        row = {
            "trial": tid, "strategy": st.id, "name": st.name, "asset_class": st.asset_class, "params": rec["params"], "start": rec["start"],
            "bars": len(res.equity), "trades": len(rs), "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 3) if rs else None,
            "net_pnl": round(float(tr["pnl"].sum()), 2) if len(tr) else 0.0,
            "bt_expectancy_r": dist.get("mean"), "passed_now": bool(bt.get("passed")),
            **consistency(rs, dist.get("mean"), dist.get("std")),
            "signals_today": _today_signals(st, sig, ctx),
        }
        if bt and bt.get("final_params") == rec["params"]:
            rows.append(row)
        else:
            history.append({k: row[k] for k in ("trial", "strategy", "start", "trades", "mean_r", "net_pnl")})
    rows.sort(key=lambda r: (r["passed_now"], r["trades"], r["mean_r"] or -9), reverse=True)
    out = {"as_of_utc": now.isoformat(timespec="seconds"), "min_trades": MIN_TRADES_FOR_CHECK, "rows": rows, "history": history}
    _out_path().parent.mkdir(parents=True, exist_ok=True)
    _out_path().write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return out
