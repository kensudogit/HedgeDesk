"""Deskの運行: 5分ごとの tick、ブリーフ用データ、週次レビュー。

tick は判断にLLMを使わないので、常時稼働のサーバーでもそのまま動く。
時刻はニューヨーク時間とUTCで判定するので、夏時間の切り替えでずれない。
"""
#
# 【位置付け】tick コンテナ（`python -m hedgedesk loop`）や Windows タスク（run_tick.pyw）が tick() を呼ぶ。
#   brief_data / weekly_review は jobs コンテナの brief.write_brief / write_weekly から呼ばれる。
# 【1日の流れ（NY 時間、平日）】
#   08:45〜09:15 保留中の株の計画を再審査 → 09:15〜11:00 発注 → 09:20〜11:00 手仕舞い予約の実行
#   15:50〜16:15 未約定の新規を取り消し → 16:15 以降 確定足でシグナル判定と審査（発注は翌朝）
#   暗号資産は UTC 00:05 以降に1日1回、取り消し・シグナル・発注・手仕舞いをまとめて行う。
# 【主な依存先】analyst、risk、trader、events、scout、news、journal、config。
# 【状態ファイル】state/ticks.json（ジョブごとの最終実行キー）、state/approved_plans.json（承認済みの計画）、
#   state/tick.lock（同時実行防止）、state/tick.log（実行ログ。ブリーフと画面の生存確認に使う）。
from __future__ import annotations

import json
import os
import sys
import time as _time
import traceback
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from . import analyst, config as C, events, journal, news, risk, scout, trader

NY = ZoneInfo("America/New_York")
TICKS = C.STATE / "ticks.json"
APPROVED = C.STATE / "approved_plans.json"
# ロックがこの秒数より古ければ、異常終了で残ったものとみなして片付ける（30分。通常の tick はこれより十分短い）。
# TICK_STALE_MINUTES: 最後の tick からこの分数を超えたら、ブリーフで「停止の可能性」と報告する。
LOCK_STALE_SECONDS = 1800
TICK_STALE_MINUTES = 15


def _load(path, default):
    """JSON ファイルを読む。ファイルがなければ default を返す。"""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _save(path, value):
    """JSON を上書き保存する（state/ がなければ作る）。日付などは default=str で文字列にする。"""
    C.STATE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _book_state(broker) -> tuple[float, float, set[str]]:
    """審査に使う資金、保有中のリスク、保有銘柄。"""
    # 引数: broker は AlpacaPaper。キーがあれば口座の資産を使い、なければ想定資金 CAPITAL を使う。
    # 戻り値: (資金, 保有・発注中のリスク合計（ドル）, 保有・発注中の銘柄の集合)。
    # 未約定（working）の計画も、約定すればリスクになるので含める。約定済みなら実際の約定価格と数量、未約定なら指値と計画数量で計算する。
    # max(0, …) は、約定価格がストップより下になった場合に、負のリスクで枠が増えないようにするため。
    book = trader._load_positions()
    held = {r["symbol"] for r in book.values()}
    open_risk = sum(max(0.0, r.get("entry_price", r["limit"]) - r["stop"]) * r.get("filled_qty", r["qty"]) for r in book.values())
    equity = float(broker.account()["equity"]) if broker.available else C.CAPITAL
    return equity, open_risk, held


def job_signals(asset_classes: set[str], now: datetime, submit_now: bool, broker=None) -> dict:
    """Chart Analyst → Risk Officer。暗号資産はそのまま発注、株は寄付き前まで保留。"""
    # 引数: asset_classes は対象の資産クラス、now は判定時刻、submit_now は承認後すぐ発注するか（暗号資産だけ True）。
    # 戻り値: 承認・拒否の件数と発注結果。チャンピオンなし・休場などのときは analyst.run の status を返す（ctx は除く）。
    # 副作用: 承認済みの計画を approved_plans.json に保存（株は "equity"、暗号資産は "crypto" のキー）。
    #   承認が出たら未ローンチのチャンピオンをローンチ済みにし、保有中の手仕舞い予約も判定する。
    # 株の計画を引け後すぐに発注しないのは、翌朝の寄付き前に最新の状況（キルスイッチ、イベント、保有）で再審査するため。
    broker = broker or trader.AlpacaPaper()
    out = analyst.run(asset_classes, now)
    if out["status"] != "ok":
        return {k: v for k, v in out.items() if k != "ctx"}
    equity, open_risk, held = _book_state(broker)
    approved, vetoed = risk.review(out["plans"], equity, open_risk, held, out["champion"], now)
    plans_key = "crypto" if "crypto" in asset_classes else "equity"
    store = _load(APPROVED, {})
    store[plans_key] = approved
    _save(APPROVED, store)
    if approved and not out["champion"].get("launched"):
        # 最初に承認が出た時点を「ローンチ」とする。以後はイベントの関門を通さない（risk.review を参照）。
        champ = dict(out["champion"], launched=True, launched_utc=now.isoformat(timespec="seconds"))
        _save(analyst.CHAMPION, champ)
    sent = trader.submit(approved, broker) if submit_now else []
    _manage_exits(out, asset_classes, broker)
    return {"status": "ok", "approved": len(approved), "vetoed": len(vetoed), "sent": sent}


def _manage_exits(out: dict, asset_classes: set[str], broker) -> None:
    """保有本数の上限とシグナルで手仕舞い予約を判定する。ストップは別に常時有効。"""
    # 引数: out は analyst.run の結果（ctx と champion を使う）、asset_classes は対象、broker はキーの有無の確認用。
    # 副作用: 保有台帳の bars_held と exit_due（time / signal）を更新する。実際の売りは翌日の job_exits が行う。
    # バックテストと同じく「判定は引け後、執行は翌日の寄付き」。最低保有本数に達するまではシグナルでは手仕舞わない。
    if not broker.available:
        return
    book = trader._load_positions()
    changed = False
    for pid, rec in book.items():
        if rec.get("state") != "open" or rec["asset_class"] not in asset_classes:
            continue
        held, signal = analyst.bars_held_and_exit(out["ctx"], rec, out["champion"])
        rec["bars_held"] = held
        changed = True
        if held >= rec["max_hold"]:
            rec["exit_due"] = "time"
        elif signal and held >= C.MIN_HOLD_BARS:
            rec["exit_due"] = "signal"
    if changed:
        trader._save_positions(book)


def job_exits(asset_classes: set[str], broker=None) -> int:
    """手仕舞い予約（exit_due）のある保有を手仕舞う。

    Args:
        asset_classes: 対象の資産クラス。broker: 省略時は AlpacaPaper。

    Returns:
        手仕舞い注文を出した件数。キーがなければ0。

    副作用: trader.exit_position で売り注文を出し、二重に売らないよう台帳に exit_sent を付ける。
    """
    broker = broker or trader.AlpacaPaper()
    n = 0
    if not broker.available:
        return n
    book = trader._load_positions()
    for pid, rec in book.items():
        if rec.get("state") == "open" and rec["asset_class"] in asset_classes and rec.get("exit_due") and not rec.get("exit_sent"):
            trader.exit_position(pid, rec["exit_due"], broker)
            rec["exit_sent"] = True
            n += 1
    trader._save_positions(book)
    return n


def job_preopen_recheck(now: datetime, broker=None) -> dict:
    """寄付き前の再審査: 保留中の計画をもう一度 Risk Officer に通す。"""
    # 引数: now は判定時刻、broker は省略時に AlpacaPaper。
    # 戻り値: 承認・拒否の件数。保留中の計画がなければ {"status": "nothing"}。
    # 前回の承認ハッシュ（approval）を外してから審査し直す。数量も最新の資金で計算し直す。
    # 副作用: approved_plans.json の "equity" を再審査で承認されたものだけに置き換える。
    broker = broker or trader.AlpacaPaper()
    store = _load(APPROVED, {})
    plans = store.get("equity", [])
    if not plans:
        return {"status": "nothing"}
    equity, open_risk, held = _book_state(broker)
    approved, vetoed = risk.review([{k: v for k, v in p.items() if k != "approval"} for p in plans], equity, open_risk, held, analyst.load_champion(), now)
    store["equity"] = approved
    _save(APPROVED, store)
    return {"status": "ok", "approved": len(approved), "vetoed": len(vetoed)}


def job_submit_equity(broker=None) -> dict:
    """再審査済みの株の計画を発注し、保留リストを空にする。

    Returns:
        {"sent": trader.submit の結果}。
    副作用: 発注の成否にかかわらず保留リストを空にする（同じ計画を翌日以降に持ち越さない。指値の有効期限1日）。
    """
    store = _load(APPROVED, {})
    plans = store.get("equity", [])
    sent = trader.submit(plans, broker) if plans else []
    store["equity"] = []
    _save(APPROVED, store)
    return {"sent": sent}


def _due(state: dict, name: str, key: str) -> bool:
    """ジョブ name の最終実行キーが key と違えば、まだ実行していない（実行すべき）とみなす。"""
    return state.get(name) != key


def _acquire_lock() -> bool:
    """同時に1つの tick だけが動くようにする。Windowsタスクとコンテナの二重起動も防ぐ。"""
    C.STATE.mkdir(parents=True, exist_ok=True)
    path = C.STATE / "tick.lock"
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                # O_EXCL 付きの作成は「なければ作る」を原子的に行う。既にあれば FileExistsError になり、二重起動を防げる。
            return True
        except FileExistsError:
            try:
                if _time.time() - path.stat().st_mtime < LOCK_STALE_SECONDS:
                    return False
                path.unlink()  # 異常終了で残ったロックは片付けて、もう一度だけ試す
            except FileNotFoundError:
                # 確認している間に別のプロセスがロックを消した。もう一度作成を試す。
                pass
    return False


def tick(now: datetime | None = None, broker=None) -> dict:
    """5分ごとの処理の入口。別の tick が実行中なら何もしない。"""
    # 引数: now は処理時刻（テスト用）、broker は省略時に AlpacaPaper。
    # 戻り値: 実行したジョブの結果（_tick の戻り値）。ロック中なら {"status": "skipped_locked"}。
    # 例外が起きてもロックは finally で必ず消す。
    if not _acquire_lock():
        return {"status": "skipped_locked"}
    try:
        return _tick(now, broker)
    finally:
        (C.STATE / "tick.lock").unlink(missing_ok=True)


def run_loop(interval_seconds: int = 300, max_iterations: int | None = None, sleep=_time.sleep) -> None:
    """コンテナで常駐するときのループ。1回失敗しても止まらず、次の回に進む。"""
    n = 0
    while max_iterations is None or n < max_iterations:
        try:
            print(json.dumps(tick(), ensure_ascii=False, default=str), flush=True)
        except Exception:
            print(traceback.format_exc(), file=sys.stderr, flush=True)
        n += 1
        if max_iterations is None or n < max_iterations:
            sleep(interval_seconds)


def _tick(now: datetime | None = None, broker=None) -> dict:
    """各ジョブは1日1回（Scoutは30分ごと）だけ実行する。"""
    # 引数・戻り値: tick と同じ。戻り値は {"ts": 時刻, "ran": ジョブ名 → 結果} で、tick.log にも1行追記する。
    # 時刻の窓は NY 時間で判定するので、夏時間の切り替えでも米国市場の時刻に合う。暗号資産だけ UTC の日付で区切る。
    # 窓の幅を持たせているのは、PC のスリープなどで5分ごとの実行が遅れても、その日のうちに1回は実行するため。
    now = now or datetime.now(timezone.utc)
    ny = now.astimezone(NY)
    state = _load(TICKS, {})
    today_ny = ny.date().isoformat()
    today_utc = now.date().isoformat()
    weekday = ny.weekday() < 5
    ran = {}

    def run(name, key, fn):
        """ジョブ name を、キー key（日付）につき1回だけ実行する。例外は記録して次のジョブへ進む。"""
        if not _due(state, name, key):
            return
        state[name] = key  # 失敗しても同じ日に繰り返し発注しないよう、先に記録する
        _save(TICKS, state)
        try:
            ran[name] = fn()
        except Exception as ex:
            ran[name] = f"error: {type(ex).__name__}"
            journal.log("job_error", "Chief of Staff", job=name, error=traceback.format_exc()[-800:])

    last_scout = state.get("scout_ts")
    if not last_scout or now - datetime.fromisoformat(last_scout) >= timedelta(minutes=30):
        # Scout は30分ごと。失敗しても他のジョブを止めず、次の30分後に再試行する（先に時刻を記録するため）。
        state["scout_ts"] = now.isoformat(timespec="seconds")
        _save(TICKS, state)
        try:
            ran["scout"] = scout.collect(now)
        except Exception as ex:
            ran["scout"] = f"error: {type(ex).__name__}"

    ran["monitor"] = trader.monitor(broker, now)
    # 見回り（キルスイッチ・約定確認・暗号資産のストップ）は毎回実行する。

    t = ny.time()
    if weekday and time(8, 45) <= t < time(9, 15):
        run("recheck", today_ny, lambda: job_preopen_recheck(now, broker))
    if weekday and time(9, 15) <= t < time(11, 0):
        run("submit_equity", today_ny, lambda: job_submit_equity(broker))
    if weekday and time(9, 20) <= t < time(11, 0):
        run("exits_equity", today_ny, lambda: job_exits({"etf", "stock"}, broker))
    if weekday and time(15, 50) <= t < time(16, 15):
        run("cancel_equity", today_ny, lambda: {"etf": trader.cancel_unfilled("etf", broker), "stock": trader.cancel_unfilled("stock", broker)})
    if weekday and t >= time(16, 15):
        run("signals_equity", today_ny, lambda: job_signals({"etf", "stock"}, now, False, broker))
    if now.time() >= time(0, 5):
        run("signals_crypto", today_utc, lambda: (trader.cancel_unfilled("crypto", broker, 23.0, now), job_signals({"crypto"}, now, True, broker), job_exits({"crypto"}, broker))[1])
        # 暗号資産: 前日の未約定の新規を取り消し（23時間以上前の注文だけ）→ シグナル判定と即発注 → 手仕舞い予約の実行。
        # タプルで3つを順に実行し、結果としてはシグナルの結果（[1]）だけを記録する。

    if "scout" in ran:
        # ニュース分析（日本語訳・センチメント・重要度・価格反応）は Scout の収集の直後に行う。
        # 発注のジョブを待たせないよう最後に回す。結果は画面用の参考情報で、ここより上のジョブには渡さない。
        try:
            ran["news"] = news.run(now)
        except Exception as ex:
            ran["news"] = f"error: {type(ex).__name__}"

    log_line = {"ts": now.isoformat(timespec="seconds"), "ran": ran}
    with open(C.STATE / "tick.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(log_line, ensure_ascii=False, default=str) + "\n")
    return log_line


def market_snapshot(refresh: bool | None = None) -> dict:
    """ブリーフ用の市場データ。計算した値だけを返す。"""
    # 戻り値: 主要 ETF・暗号資産の終値・前日比・21営業日騰落率・200日線との位置・出来高倍率・スコアと、
    #   VIX、出来高が20日平均の2倍以上の銘柄の一覧。
    # 既定は refresh=False（夜間リサーチと同じキャッシュを読むだけ）。朝のブリーフで毎回全銘柄を
    #   Yahoo から取り直すと 1 分超かかるため。取り直しが必要なときは環境変数
    #   HEDGEDESK_BRIEF_MARKET_REFRESH=1 または引数 refresh=True。
    from .research import build_context

    if refresh is None:
        refresh = os.environ.get("HEDGEDESK_BRIEF_MARKET_REFRESH", "").strip().lower() in ("1", "true", "yes")
    ctx, log = build_context(refresh=refresh)
    rows = {}
    for s in C.INDEX_ETFS + C.SECTOR_ETFS + C.CRYPTO:
        df = ctx.market.get(s)
        if df is None or len(df) < 2:
            continue
        last = df.iloc[-1]
        rows[s] = {
            "date": str(df.index[-1].date()), "close": round(float(last["close"]), 2),
            "ret_1d": round(float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1), 4),
            "ret_21d": round(float(df["close"].iloc[-1] / df["close"].iloc[-22] - 1), 4) if len(df) > 22 else None,
            "above_sma200": bool(last["close"] > last["sma200"]),
            "volume_vs_avg20": round(float(last["volume"] / last["vol_avg20"]), 2) if last["vol_avg20"] else None,
            "score": None if last["score"] != last["score"] else int(last["score"]),
            # NaN は自分自身と等しくならない性質を使って、スコアが NaN（判定不能）なら None にしている。
        }
    unusual = []
    for s in C.STOCKS + C.ETFS:
        df = ctx.market.get(s)
        if df is not None and len(df) > 21 and df["vol_avg20"].iloc[-1] and df["volume"].iloc[-1] / df["vol_avg20"].iloc[-1] >= 2:
            unusual.append({"symbol": s, "volume_vs_avg20": round(float(df["volume"].iloc[-1] / df["vol_avg20"].iloc[-1]), 2), "ret_1d": round(float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1), 4)})
    vix = round(float(ctx.vix.iloc[-1]), 2) if ctx.vix is not None and len(ctx.vix) else None
    return {"source": log["source"], "rows": rows, "vix": vix, "vix_date": str(ctx.vix.index[-1].date()) if vix else None, "unusual_volume": unusual}


def _tick_health(now: datetime) -> dict:
    """5分処理の生存確認。コンテナが止まっていると、画面もログも更新されないので、ブリーフで報告する。"""
    # 引数: now は判定時刻。
    # 戻り値: healthy（最終実行から TICK_STALE_MINUTES 以内か）、last_tick_utc、age_minutes。ログがなければ healthy=False。
    try:
        lines = (C.STATE / "tick.log").read_text(encoding="utf-8").splitlines()
        last = json.loads(lines[-1])["ts"]
    except (OSError, IndexError, ValueError, KeyError):
        return {"healthy": False, "last_tick_utc": None, "age_minutes": None}
    age = (now - datetime.fromisoformat(last)).total_seconds() / 60
    return {"healthy": age <= TICK_STALE_MINUTES, "last_tick_utc": last, "age_minutes": round(age, 1)}


def brief_data(now: datetime | None = None) -> dict:
    """Chief of Staff がブリーフを書くための材料を1つのJSONにまとめる。"""
    # 引数: now は作成時刻。ジャーナルは直近24時間分を読む。
    # 戻り値: ブリーフの材料の dict（brief.render が読む）。
    # 副作用: state/brief_data.json に保存する（画面のイベント表示にも使われる）。
    #   市場スナップショットは通常キャッシュ読み取り（events は events モジュールが取得）。
    # ニュースの見出しは scout_headlines_untrusted という名前で入れ、外部データだと分かるようにしている（ブリーフ本文には使わない）。
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = journal.read(since)
    gate_ok, gate_reasons = events.launch_gate(now)
    latest = _load(C.STATE / "latest_research.json", None)
    broker = trader.AlpacaPaper()
    account = None
    if broker.available:
        try:
            a = broker.account()
            account = {"equity": a["equity"], "last_equity": a["last_equity"]}
        except Exception as ex:
            account = {"error": type(ex).__name__}
    out = {
        "as_of_utc": now.isoformat(timespec="seconds"),
        "tick_health": _tick_health(now),
        "market": market_snapshot(),
        "events": events.upcoming(now, days=7),
        "launch_gate": {"ok": gate_ok, "reasons": gate_reasons},
        "research": latest,
        "champion": analyst.load_champion(),
        "vetoes_24h": [r for r in rows if r["kind"] == "veto"],
        "trades_24h": [r for r in rows if r["kind"] in ("order", "fill_opened", "fill_closed", "exit_order", "order_not_sent", "emergency_exit", "close_unknown")],
        "errors_24h": [r for r in rows if r["kind"] == "job_error"],
        "kill_switch": risk.kill_status(),
        "alpaca_connected": broker.available,
        "account": account,
        "open_book": trader._load_positions(),
        "scout_headlines_untrusted": scout.recent(60, now),
    }
    _save(C.STATE / "brief_data.json", out)
    return out


def weekly_review(now: datetime | None = None, seed: int = C.MC_SEED) -> dict:
    """ペーパーとバックテストを比較して、昇格の階段の位置を判定する。"""
    # 引数: now は判定時刻、seed はブートストラップの乱数シード（結果を再現できるよう固定）。
    # 戻り値: rung（昇格の段階）と、判定に使った値（ペーパーのトレード数・日数・平均R・90%信頼区間・整合の有無）。
    # 副作用: reports/weekly_<日付>.json に保存し、ジャーナルに記録する。ライブ移行の判断と実行はしない（オーナーの役目）。
    # 段階: no_champion → not_launched → paper_under_2_weeks → insufficient_data →
    #   eligible_for_owner_decision（整合あり）または does_not_match_backtest（整合なし）。
    now = now or datetime.now(timezone.utc)
    champ = analyst.load_champion()
    latest = _load(C.STATE / "latest_research.json", None)
    closed = [r for r in journal.read() if r["kind"] == "fill_closed"]
    result = {"as_of_utc": now.isoformat(timespec="seconds"), "champion": champ, "paper_trades": len(closed), "rung": "no_champion"}
    if champ:
        closed = [r for r in closed if r.get("strategy") == champ["id"] and r.get("r") is not None]
        launched = champ.get("launched_utc")
        days = (now - datetime.fromisoformat(launched)).days if launched else 0
        result.update(paper_trades=len(closed), paper_days=days)
        top = next((r for r in (latest or {}).get("ranked", []) if r["id"] == champ["id"]), None)
        if not launched:
            result["rung"] = "not_launched"
        elif days < 14:
            result["rung"] = "paper_under_2_weeks"
        elif not closed or top is None:
            result["rung"] = "insufficient_data"
        else:
            result["rung"] = "paper_review"
        if closed and top:
            r = np.array([x["r"] for x in closed], dtype=float)
            result["paper_expectancy_r"] = round(float(r.mean()), 3)
            result["backtest_expectancy_r"] = top["out_of_sample"]["expectancy_r"]
            # バックテストの1トレード分布が手元にないので、期待値と標準偏差の推定はペーパー側で行う
            rng = np.random.default_rng(seed)
            boots = [rng.choice(r, size=r.size, replace=True).mean() for _ in range(C.MC_RUNS)]
            # ペーパーの R を復元抽出して平均を取り直し（ブートストラップ）、平均 R の 90% 信頼区間を作る。
            # バックテストの平均 R がこの区間に入っていれば「ペーパーはバックテストと整合している」とみなす。
            lo, hi = np.percentile(boots, [5, 95])
            result["paper_ci90"] = [round(float(lo), 3), round(float(hi), 3)]
            matches = lo <= top["out_of_sample"]["expectancy_r"] <= hi
            result["matches_backtest"] = bool(matches)
            if result["rung"] == "paper_review":
                result["rung"] = "eligible_for_owner_decision" if matches else "does_not_match_backtest"
    C.REPORTS.mkdir(parents=True, exist_ok=True)
    _save(C.REPORTS / f"weekly_{now.strftime('%Y%m%d')}.json", result)
    journal.log("weekly_review", "Chief of Staff", **{k: result.get(k) for k in ("rung", "paper_trades", "matches_backtest")})
    return result
