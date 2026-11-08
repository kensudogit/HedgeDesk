"""Trader: 発注に触れる唯一のモジュール。接続先はペーパー口座に固定している。

安全装置:
- 接続先URLはコードに固定。環境変数で変えようとすると例外で止まる。
- Risk Officer の承認ハッシュが一致しない計画は発注しない。
- 新規は買いの指値のみ。売りは保有数量まで。
"""
#
# 【位置付け】desk（tick の各ジョブ）から呼ばれる。発注 API（/v2/orders への POST）を呼ぶコードはこのファイルにしかない
#   （tests/test_risk_trader.py の test_only_trader_posts_orders が他ファイルに post_order がないことを検査している）。
# 【保有台帳 state/positions.json】plan_id → 計画と状態。state は working（発注済み・未約定）→ open（保有中）→ 決済で削除。
# 【主な依存先】requests、config、journal、risk（承認ハッシュの再計算とキルスイッチ）。
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import requests

from . import config as C
from . import journal
from .risk import approval_hash, fire_kill, kill_status

# POSITIONS: 保有台帳。FAILS: API の連続失敗回数。ENV_FILE: キーを置く .env（Git 管理外）。
POSITIONS = C.STATE / "positions.json"
FAILS = C.STATE / "api_failures.json"
ENV_FILE = C.ROOT / ".env"


class LiveEndpointRefused(RuntimeError):
    # ペーパー口座以外の接続先が指定されたときに出す例外。ライブ口座への誤発注を起動時に止める。
    pass


def load_keys() -> tuple[str | None, str | None]:
    """環境変数、なければ .env からキーを読む。キーは画面にもログにも出さない。"""
    # 戻り値: (キー ID, シークレット)。見つからない・空なら None。
    # 環境変数のどちらかが欠けていれば .env を読み、見つかった項目で上書きする（"#" で始まる行はコメントとして無視）。
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
    """Yahoo 表記の銘柄を Alpaca 表記にする（BTC-USD → BTC/USD）。株・ETF はそのまま。"""
    return symbol.replace("-", "/") if symbol.endswith("-USD") else symbol


def from_broker_symbol(symbol: str) -> str:
    """Alpaca の銘柄表記を Yahoo 表記に戻す。

    暗号資産は注文では "BTC/USD"、保有一覧では "BTCUSD" と返ってくることがあるので、両方を "BTC-USD" に変換する。
    スラッシュなしの形は、株の銘柄と取り違えないよう BTC と ETH に限定している。
    """
    if symbol.endswith("/USD"):
        return symbol.replace("/", "-")
    if symbol.endswith("USD") and symbol[:-3] in {"BTC", "ETH"}:
        return symbol[:-3] + "-USD"
    return symbol


class AlpacaPaper:
    """Alpaca ペーパー口座の REST API の薄いラッパー。

    Args（コンストラクタ）:
        session: request() を持つ HTTP クライアント。テストで偽物を渡すため。省略時は requests.Session。
        keys: (キー ID, シークレット)。省略時は load_keys() で読む。

    Raises:
        LiveEndpointRefused: 環境変数 APCA_API_BASE_URL がペーパー以外を指す場合、または BASE が書き換えられた場合。
    """
    BASE = C.ALPACA_PAPER_URL

    def __init__(self, session=None, keys: tuple[str | None, str | None] | None = None):
        override = os.environ.get("APCA_API_BASE_URL")
        if override and override.rstrip("/") != self.BASE:
            raise LiveEndpointRefused(f"ペーパー以外の接続先は拒否します: {override}")
        if self.BASE != "https://paper-api.alpaca.markets":
            # BASE はクラス属性なので、コードやテストで書き換えられてもここで止まるよう、文字列リテラルと直接比較する。
            raise LiveEndpointRefused(self.BASE)
        self.key, self.secret = keys if keys is not None else load_keys()
        self.http = session or requests.Session()

    @property
    def available(self) -> bool:
        """キーが両方そろっているか。False のときは何も送らない（未発注として記録するだけ）。"""
        return bool(self.key and self.secret)

    def _req(self, method: str, path: str, **kw):
        """API を1回呼ぶ。HTTP エラーは raise_for_status で例外にする。本文が空（取消の応答など）なら None。"""
        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}
        r = self.http.request(method, self.BASE + path, headers=headers, timeout=20, **kw)
        r.raise_for_status()
        return r.json() if getattr(r, "content", b"") else None

    def account(self) -> dict:
        """口座情報（equity: 現在の資産、last_equity: 前営業日終了時の資産 など）。"""
        return self._req("GET", "/v2/account")

    def positions(self) -> list[dict]:
        """ブローカー上の保有一覧。"""
        return self._req("GET", "/v2/positions")

    def open_orders(self) -> list[dict]:
        """未約定の注文一覧。nested=true で、OTO 注文に付いたストップ（legs）も含めて返させる。"""
        return self._req("GET", "/v2/orders", params={"status": "open", "nested": "true", "limit": 500})

    def closed_orders(self, symbol: str, after: str) -> list[dict]:
        """symbol の、after（ISO 時刻）以降に終わった注文を新しい順に返す（決済価格の確認用）。"""
        return self._req("GET", "/v2/orders", params={"status": "closed", "symbols": to_broker_symbol(symbol), "after": after, "limit": 100, "direction": "desc"})

    def cancel(self, order_id: str) -> None:
        """注文を取り消す。"""
        self._req("DELETE", f"/v2/orders/{order_id}")

    def post_order(self, body: dict) -> dict:
        """注文を送る。body は entry_order_body / crypto_stop_body などで作った Alpaca の注文形式。"""
        return self._req("POST", "/v2/orders", json=body)


def entry_order_body(plan: dict) -> dict:
    """新規注文の中身。株とETFは約定と同時にストップが有効になるOTO、暗号資産は単純な指値。"""
    # 引数: plan は承認済みの計画（qty 入り）。
    # 戻り値: Alpaca の注文 JSON。
    # 例外: ValueError（買い以外。空売り禁止のルールを発注直前でも確かめる）。
    # time_in_force は gtc（取り消すまで有効）。1日で失効させるのは cancel_unfilled の役目（バックテストの有効期限1日に合わせる）。
    # client_order_id に plan_id を入れるので、同じ計画を二重に送ってもブローカー側で拒否される。
    # 暗号資産は Alpaca が OTO に対応していないため、約定を確認してから monitor でストップを別に置く。
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
    # 引数: symbol は Yahoo 表記、qty は約定した数量、stop はストップ価格。
    # 戻り値: stop_limit の売り注文の JSON。暗号資産では単純なストップ（成行）が使えないため stop_limit にしている。
    return {
        "symbol": to_broker_symbol(symbol), "side": "sell", "type": "stop_limit", "qty": f"{qty:.6f}",
        "stop_price": f"{stop:.2f}", "limit_price": f"{stop * 0.99:.2f}", "time_in_force": "gtc",
    }


def _load_positions() -> dict:
    """保有台帳を読む。ファイルがなければ空の dict。"""
    return json.loads(POSITIONS.read_text(encoding="utf-8")) if POSITIONS.exists() else {}


def _save_positions(p: dict) -> None:
    """保有台帳を上書き保存する。"""
    C.STATE.mkdir(parents=True, exist_ok=True)
    POSITIONS.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")


def submit(plans: list[dict], broker: AlpacaPaper | None = None) -> list[dict]:
    """承認済みの計画だけを発注する。キーがなければ何も送らず、未発注と記録する。"""
    # 引数: plans は risk.review が承認した計画。broker は省略時に AlpacaPaper を作る。
    # 戻り値: 計画ごとの {"plan_id", "status"}。status は submitted / refused / not_sent_no_keys。
    # 副作用: 発注した計画を保有台帳に working で登録し、ジャーナルに記録する。
    # 検査の順序: 承認ハッシュ → キルスイッチ → キーの有無。審査の後にキルスイッチが入った場合も発注しない。
    # 例外: ブローカーの HTTP エラーはそのまま上に伝わる（呼び出し元の tick がジョブのエラーとして記録する）。
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
    # 引数: failed は今回の API 呼び出しが失敗したか。成功したら回数を0に戻す。
    # 副作用: state/api_failures.json に連続失敗回数を書き、STALE_FAILURES（3回）以上ならキルスイッチを入れる。
    n = 0
    if failed:
        n = json.loads(FAILS.read_text()).get("n", 0) + 1 if FAILS.exists() else 1
    C.STATE.mkdir(parents=True, exist_ok=True)
    FAILS.write_text(json.dumps({"n": n}))
    if n >= C.STALE_FAILURES:
        fire_kill(f"データ停止: API失敗が{n}回連続")


def consecutive_losses(rows: list[dict] | None = None) -> int:
    """直近から数えた連敗数を返す。

    Args:
        rows: 決済記録（pnl を持つ dict）のリスト。省略時はジャーナルの fill_closed をすべて読む。

    Returns:
        末尾から pnl < 0 が続く件数。損益0や勝ちで止まる（バックテストの連敗の数え方と同じ）。
    """
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
    # 引数: broker は省略時に AlpacaPaper を作る。now は約定確認の時刻として記録する。
    # 戻り値: status（ok / no_keys / api_error）と、ok なら equity・保有件数・未約定件数。
    # 副作用: 保有台帳の状態更新、暗号資産のストップ発注（失敗時は成行で手仕舞い）、決済の記録、キルスイッチの作動。
    # 口座・保有・注文の3つは最初にまとめて取得し、1つでも失敗したら API 失敗として数えて何もしない
    #   （古い情報や一部だけの情報で台帳を書き換えないため）。
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
        # 日次損失は前営業日終了時の資産との比較で判定する（バックテストの「前日大引け比」と同じ考え方）。
        fire_kill(f"日次損失 {equity / last_equity - 1:.2%}")

    book = _load_positions()
    open_ids = {o["id"] for o in orders}
    for pid, rec in list(book.items()):
        sym = rec["symbol"]
        if rec["state"] == "working" and sym in held:
            # 発注済みの銘柄がブローカーの保有に現れたら約定とみなす。価格と数量はブローカーの値を使う
            #   （部分約定でも実際に持っている数量でリスクと損益を計算するため）。
            rec["state"] = "open"
            rec["entry_price"] = float(held[sym]["avg_entry_price"])
            rec["filled_qty"] = float(held[sym]["qty"])
            rec["opened_utc"] = now.isoformat(timespec="seconds")
            journal.log("fill_opened", "Trader", TICKER=sym, plan_id=pid, price=rec["entry_price"], qty=rec["filled_qty"])
        elif rec["state"] == "working" and rec.get("order_id") not in open_ids:
            # 未約定のまま未約定一覧から消えた注文は、取消・失効・拒否のいずれか。台帳から外す。
            journal.log("order_ended", "Trader", TICKER=sym, plan_id=pid, reason="未約定で終了")
            del book[pid]
            continue
        if rec["state"] == "open" and rec["asset_class"] == "crypto" and not rec.get("stop_order_id"):
            # 暗号資産は新規に OTO のストップを付けられないので、約定を確認した最初の見回りでストップを置く。
            try:
                stop = broker.post_order(crypto_stop_body(sym, rec["filled_qty"], rec["stop"]))
                rec["stop_order_id"] = stop.get("id")
                journal.log("stop_placed", "Trader", TICKER=sym, plan_id=pid, stop=rec["stop"])
            except Exception as ex:
                # ストップが置けないまま持たない
                broker.post_order({"symbol": to_broker_symbol(sym), "side": "sell", "type": "market", "qty": f"{rec['filled_qty']:.6f}", "time_in_force": "gtc"})
                journal.log("emergency_exit", "Trader", TICKER=sym, plan_id=pid, reason=f"ストップ発注失敗: {type(ex).__name__}")
        if rec["state"] == "open" and sym not in held:
            # 保有中だった銘柄がブローカーの保有から消えた＝ストップか手仕舞い注文で決済された。
            _record_close(broker, pid, rec)
            del book[pid]
    _save_positions(book)
    streak = consecutive_losses()
    # 連敗の判定はジャーナル全体の決済記録で行う（今回の見回りの決済も記録済みなので含まれる）。
    if streak >= C.KILL_CONSECUTIVE_LOSSES:
        fire_kill(f"{streak}連敗")
    return {"status": "ok", "equity": equity, "open": sum(1 for r in book.values() if r["state"] == "open"), "working": sum(1 for r in book.values() if r["state"] == "working")}


def _record_close(broker: AlpacaPaper, pid: str, rec: dict) -> None:
    """決済の実績を記録する。約定価格が確認できないときは推定せず、不明と記録する。"""
    # 引数: pid は計画 ID、rec は保有台帳の1件（entry_price / filled_qty / opened_utc / stop を使う）。
    # 副作用: ジャーナルに fill_closed（損益と R）または close_unknown を記録する。
    # 直近の売りの約定のうち最も新しいもの（closed_orders は新しい順）の平均約定価格を使う。
    # 取得の失敗は握りつぶして close_unknown にする（見回り全体を止めないため。推定値で成績を作らないため）。
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
    # R の分母は実際の約定価格からストップまでのリスク額。0以下（約定がストップより下）なら R は記録しない。
    journal.log("fill_closed", "Trader", TICKER=rec["symbol"], plan_id=pid, entry=rec["entry_price"], exit=exit_px, qty=rec["filled_qty"], pnl=round(pnl, 2), r=round(pnl / risk, 3) if risk > 0 else None, strategy=rec["strategy"])


def cancel_unfilled(asset_class: str, broker: AlpacaPaper | None = None, max_age_hours: float = 0.0, now: datetime | None = None) -> int:
    """当日に約定しなかった新規注文を取り消す（バックテストの有効期限1日に合わせる）。"""
    # 引数: asset_class は対象の資産クラス。max_age_hours: 発注からこの時間が経っていない注文は残す
    #   （暗号資産は 23 時間を指定し、同じ tick で出す新しい注文を取り消さないようにしている）。
    # 戻り値: 取り消した件数。キーがなければ0。
    # 副作用: ブローカーで取り消し、台帳から削除し、ジャーナルに記録する。
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
    # 引数: pid は計画 ID、reason は手仕舞いの理由（time / signal）。
    # 副作用: 該当銘柄の売り注文（単独のストップと、OTO に付いたストップの legs）を取り消し、成行の売りを出す。
    # ストップを残したまま成行で売ると、両方が約定して売りすぎ（意図しない空売り）になるおそれがあるため、先に消す。
    # 数量は台帳の約定数量（filled_qty）。台帳から計画は消さず、決済の確認と記録は次の monitor が行う。
    broker = broker or AlpacaPaper()
    book = _load_positions()
    rec = book[pid]
    broker_symbol = to_broker_symbol(rec["symbol"])
    for o in broker.open_orders():
        if o.get("symbol") in (broker_symbol, broker_symbol.replace("/", "")) and o.get("side") == "sell":
            # 暗号資産の銘柄は "BTC/USD" と "BTCUSD" のどちらでも返り得るので両方と比較する。
            broker.cancel(o["id"])
    for o in broker.open_orders():
        for leg in o.get("legs") or []:
            if leg.get("symbol") in (broker_symbol, broker_symbol.replace("/", "")) and leg.get("side") == "sell":
                broker.cancel(leg["id"])
    tif = "gtc" if rec["asset_class"] == "crypto" else "day"
    # 株の成行は当日限り（day）。暗号資産は Alpaca の制約で gtc を使う。
    qty = f"{rec['filled_qty']:.6f}" if rec["asset_class"] == "crypto" else str(int(rec["filled_qty"]))
    broker.post_order({"symbol": broker_symbol, "side": "sell", "type": "market", "qty": qty, "time_in_force": tif})
    journal.log("exit_order", "Trader", TICKER=rec["symbol"], plan_id=pid, reason=reason, qty=qty)
