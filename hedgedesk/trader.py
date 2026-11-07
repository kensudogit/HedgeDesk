"""Trader: 発注に触れる唯一のモジュール。接続先はペーパー口座に固定している。

安全装置:
- 接続先URLはコードに固定。環境変数で変えようとすると例外で止まる。
- Risk Officer の承認ハッシュが一致しない計画は発注しない。
- 新規は買いの指値のみ。売りは保有数量まで。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import requests

from . import config as C
from . import journal
from .risk import approval_hash, fire_kill, kill_status

POSITIONS = C.STATE / "positions.json"
FAILS = C.STATE / "api_failures.json"
ENV_FILE = C.ROOT / ".env"


class LiveEndpointRefused(RuntimeError):
    pass


def load_keys() -> tuple[str | None, str | None]:
    """環境変数、なければ .env からキーを読む。キーは画面にもログにも出さない。"""
    key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if (not key or not secret) and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() == "APCA_API_KEY_ID":
                    key = v.strip()
                elif k.strip() == "APCA_API_SECRET_KEY":
                    secret = v.strip()
    return key or None, secret or None


def to_broker_symbol(symbol: str) -> str:
    return symbol.replace("-", "/") if symbol.endswith("-USD") else symbol


def from_broker_symbol(symbol: str) -> str:
    if symbol.endswith("/USD"):
        return symbol.replace("/", "-")
    if symbol.endswith("USD") and symbol[:-3] in {"BTC", "ETH"}:
        return symbol[:-3] + "-USD"
    return symbol


class AlpacaPaper:
    BASE = C.ALPACA_PAPER_URL

    def __init__(self, session=None, keys: tuple[str | None, str | None] | None = None):
        override = os.environ.get("APCA_API_BASE_URL")
        if override and override.rstrip("/") != self.BASE:
            raise LiveEndpointRefused(f"ペーパー以外の接続先は拒否します: {override}")
        if self.BASE != "https://paper-api.alpaca.markets":
            raise LiveEndpointRefused(self.BASE)
        self.key, self.secret = keys if keys is not None else load_keys()
        self.http = session or requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.key and self.secret)

    def _req(self, method: str, path: str, **kw):
        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}
        r = self.http.request(method, self.BASE + path, headers=headers, timeout=20, **kw)
        r.raise_for_status()
        return r.json() if getattr(r, "content", b"") else None

    def account(self) -> dict:
        return self._req("GET", "/v2/account")

    def positions(self) -> list[dict]:
        return self._req("GET", "/v2/positions")

    def open_orders(self) -> list[dict]:
        return self._req("GET", "/v2/orders", params={"status": "open", "nested": "true", "limit": 500})

    def closed_orders(self, symbol: str, after: str) -> list[dict]:
        return self._req("GET", "/v2/orders", params={"status": "closed", "symbols": to_broker_symbol(symbol), "after": after, "limit": 100, "direction": "desc"})

    def cancel(self, order_id: str) -> None:
        self._req("DELETE", f"/v2/orders/{order_id}")

    def post_order(self, body: dict) -> dict:
        return self._req("POST", "/v2/orders", json=body)


def entry_order_body(plan: dict) -> dict:
    """新規注文の中身。株とETFは約定と同時にストップが有効になるOTO、暗号資産は単純な指値。"""
    if plan["side"] != "buy":
        raise ValueError("新規注文は買いのみ")
    body = {
        "symbol": to_broker_symbol(plan["symbol"]),
        "side": "buy",
        "type": "limit",
        "limit_price": f"{plan['limit']:.2f}",
        "time_in_force": "gtc",
        "client_order_id": plan["plan_id"],
    }
    if plan["asset_class"] == "crypto":
        body["qty"] = f"{plan['qty']:.6f}"
    else:
        body["qty"] = str(int(plan["qty"]))
        body["order_class"] = "oto"
        body["stop_loss"] = {"stop_price": f"{plan['stop']:.2f}"}
    return body


def crypto_stop_body(symbol: str, qty: float, stop: float) -> dict:
    """暗号資産のストップ。指値を1%下に置く（飛ぶと約定しないリスクは残る）。"""
    return {
        "symbol": to_broker_symbol(symbol), "side": "sell", "type": "stop_limit", "qty": f"{qty:.6f}",
        "stop_price": f"{stop:.2f}", "limit_price": f"{stop * 0.99:.2f}", "time_in_force": "gtc",
    }


def _load_positions() -> dict:
    return json.loads(POSITIONS.read_text(encoding="utf-8")) if POSITIONS.exists() else {}


def _save_positions(p: dict) -> None:
    C.STATE.mkdir(parents=True, exist_ok=True)
    POSITIONS.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")


def submit(plans: list[dict], broker: AlpacaPaper | None = None) -> list[dict]:
    """承認済みの計画だけを発注する。キーがなければ何も送らず、未発注と記録する。"""
    broker = broker or AlpacaPaper()
    results = []
    for plan in plans:
        if plan.get("approval") != approval_hash(plan):
            journal.log("order_refused", "Trader", TICKER=plan.get("symbol"), reason="承認ハッシュ不一致")
            results.append({"plan_id": plan.get("plan_id"), "status": "refused"})
            continue
        if kill_status().get("active"):
            journal.log("order_refused", "Trader", TICKER=plan["symbol"], reason="キルスイッチ作動中")
            results.append({"plan_id": plan["plan_id"], "status": "refused"})
            continue
        if not broker.available:
            journal.log("order_not_sent", "Trader", TICKER=plan["symbol"], plan_id=plan["plan_id"], reason="Alpacaキー未設定。約定は発生していない")
            results.append({"plan_id": plan["plan_id"], "status": "not_sent_no_keys"})
            continue
        order = broker.post_order(entry_order_body(plan))
        book = _load_positions()
        book[plan["plan_id"]] = {**plan, "order_id": order.get("id"), "state": "working", "submitted_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "bars_held": 0}
        _save_positions(book)
        journal.log("order", "Trader", TICKER=plan["symbol"], plan_id=plan["plan_id"], limit=plan["limit"], stop=plan["stop"], qty=plan["qty"], order_id=order.get("id"))
        results.append({"plan_id": plan["plan_id"], "status": "submitted"})
    return results


def _record_api_failure(failed: bool) -> None:
    """連続でAPIが失敗したらデータ停止としてキルスイッチを入れる。"""
    n = 0
    if failed:
        n = json.loads(FAILS.read_text()).get("n", 0) + 1 if FAILS.exists() else 1
    C.STATE.mkdir(parents=True, exist_ok=True)
    FAILS.write_text(json.dumps({"n": n}))
    if n >= C.STALE_FAILURES:
        fire_kill(f"データ停止: API失敗が{n}回連続")


def consecutive_losses(rows: list[dict] | None = None) -> int:
    rows = rows if rows is not None else [r for r in journal.read() if r["kind"] == "fill_closed"]
    n = 0
    for r in reversed(rows):
        if r["pnl"] < 0:
            n += 1
        else:
            break
    return n


def monitor(broker: AlpacaPaper | None = None, now: datetime | None = None) -> dict:
    """5分ごとの見回り: キルスイッチ、約定確認、暗号資産のストップ、決済の記録。"""
    broker = broker or AlpacaPaper()
    now = now or datetime.now(timezone.utc)
    if not broker.available:
        return {"status": "no_keys"}
    try:
        account = broker.account()
        held = {from_broker_symbol(p["symbol"]): p for p in broker.positions()}
        orders = broker.open_orders()
        _record_api_failure(False)
    except Exception as ex:
        _record_api_failure(True)
        return {"status": "api_error", "error": type(ex).__name__}

    equity, last_equity = float(account["equity"]), float(account["last_equity"])
    if last_equity > 0 and equity <= last_equity * (1 - C.KILL_DAILY_LOSS):
        fire_kill(f"日次損失 {equity / last_equity - 1:.2%}")

    book = _load_positions()
    open_ids = {o["id"] for o in orders}
    for pid, rec in list(book.items()):
        sym = rec["symbol"]
        if rec["state"] == "working" and sym in held:
            rec["state"] = "open"
            rec["entry_price"] = float(held[sym]["avg_entry_price"])
            rec["filled_qty"] = float(held[sym]["qty"])
            rec["opened_utc"] = now.isoformat(timespec="seconds")
            journal.log("fill_opened", "Trader", TICKER=sym, plan_id=pid, price=rec["entry_price"], qty=rec["filled_qty"])
        elif rec["state"] == "working" and rec.get("order_id") not in open_ids:
            journal.log("order_ended", "Trader", TICKER=sym, plan_id=pid, reason="未約定で終了")
            del book[pid]
            continue
        if rec["state"] == "open" and rec["asset_class"] == "crypto" and not rec.get("stop_order_id"):
            try:
                stop = broker.post_order(crypto_stop_body(sym, rec["filled_qty"], rec["stop"]))
                rec["stop_order_id"] = stop.get("id")
                journal.log("stop_placed", "Trader", TICKER=sym, plan_id=pid, stop=rec["stop"])
            except Exception as ex:
                # ストップが置けないまま持たない
                broker.post_order({"symbol": to_broker_symbol(sym), "side": "sell", "type": "market", "qty": f"{rec['filled_qty']:.6f}", "time_in_force": "gtc"})
                journal.log("emergency_exit", "Trader", TICKER=sym, plan_id=pid, reason=f"ストップ発注失敗: {type(ex).__name__}")
        if rec["state"] == "open" and sym not in held:
            _record_close(broker, pid, rec)
            del book[pid]
    _save_positions(book)
    streak = consecutive_losses()
    if streak >= C.KILL_CONSECUTIVE_LOSSES:
        fire_kill(f"{streak}連敗")
    return {"status": "ok", "equity": equity, "open": sum(1 for r in book.values() if r["state"] == "open"), "working": sum(1 for r in book.values() if r["state"] == "working")}


def _record_close(broker: AlpacaPaper, pid: str, rec: dict) -> None:
    """決済の実績を記録する。約定価格が確認できないときは推定せず、不明と記録する。"""
    exit_px = None
    try:
        for o in broker.closed_orders(rec["symbol"], rec["opened_utc"]):
            if o.get("side") == "sell" and o.get("filled_avg_price"):
                exit_px = float(o["filled_avg_price"])
                break
    except Exception:
        pass
    if exit_px is None:
        journal.log("close_unknown", "Trader", TICKER=rec["symbol"], plan_id=pid, reason="決済価格を確認できない")
        return
    pnl = (exit_px - rec["entry_price"]) * rec["filled_qty"]
    risk = (rec["entry_price"] - rec["stop"]) * rec["filled_qty"]
    journal.log("fill_closed", "Trader", TICKER=rec["symbol"], plan_id=pid, entry=rec["entry_price"], exit=exit_px, qty=rec["filled_qty"], pnl=round(pnl, 2), r=round(pnl / risk, 3) if risk > 0 else None, strategy=rec["strategy"])


def cancel_unfilled(asset_class: str, broker: AlpacaPaper | None = None, max_age_hours: float = 0.0, now: datetime | None = None) -> int:
    """当日に約定しなかった新規注文を取り消す（バックテストの有効期限1日に合わせる）。"""
    broker = broker or AlpacaPaper()
    now = now or datetime.now(timezone.utc)
    if not broker.available:
        return 0
    book = _load_positions()
    n = 0
    for pid, rec in list(book.items()):
        if rec["state"] != "working" or rec["asset_class"] != asset_class:
            continue
        if now - datetime.fromisoformat(rec["submitted_utc"]) < timedelta(hours=max_age_hours):
            continue
        broker.cancel(rec["order_id"])
        journal.log("order_cancelled", "Trader", TICKER=rec["symbol"], plan_id=pid, reason="有効期限切れ")
        del book[pid]
        n += 1
    _save_positions(book)
    return n


def exit_position(pid: str, reason: str, broker: AlpacaPaper | None = None) -> None:
    """時間切れやシグナルでの手仕舞い。先にストップ注文を消し、保有数量だけ成行で売る。"""
    broker = broker or AlpacaPaper()
    book = _load_positions()
    rec = book[pid]
    broker_symbol = to_broker_symbol(rec["symbol"])
    for o in broker.open_orders():
        if o.get("symbol") in (broker_symbol, broker_symbol.replace("/", "")) and o.get("side") == "sell":
            broker.cancel(o["id"])
    for o in broker.open_orders():
        for leg in o.get("legs") or []:
            if leg.get("symbol") in (broker_symbol, broker_symbol.replace("/", "")) and leg.get("side") == "sell":
                broker.cancel(leg["id"])
    tif = "gtc" if rec["asset_class"] == "crypto" else "day"
    qty = f"{rec['filled_qty']:.6f}" if rec["asset_class"] == "crypto" else str(int(rec["filled_qty"]))
    broker.post_order({"symbol": broker_symbol, "side": "sell", "type": "market", "qty": qty, "time_in_force": tif})
    journal.log("exit_order", "Trader", TICKER=rec["symbol"], plan_id=pid, reason=reason, qty=qty)
