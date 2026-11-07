"""Risk Officer の拒否、Trader の安全装置、発注経路の検査。"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hedgedesk import config as C, desk, journal, risk, scout, trader

NOW = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)


def plan(**kw):
    base = {
        "plan_id": "T-1", "symbol": "SPY", "side": "buy", "asset_class": "etf", "strategy": "S3_MR_ETF", "signal_date": "2026-11-06",
        "limit": 500.0, "stop": 490.0, "score": 8, "max_hold": 5, "price": 500.0, "dollar_volume": 1e9, "earnings_blocked": False, "qty": 0.0,
    }
    return {**base, **kw}


def review(p, equity=C.CAPITAL, open_risk=0.0, held=frozenset(), champion=None):
    return risk.review([p], equity, open_risk, set(held), champion, NOW)


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.content = b"x"

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeHttp:
    """送信内容を記録し、実際には何も送らない。"""
    def __init__(self, account=None, positions=(), orders=(), closed=(), fail_stop=False):
        self.calls = []
        self.account = account or {"equity": "100000", "last_equity": "100000"}
        self.positions, self.orders, self.closed = list(positions), list(orders), list(closed)
        self.fail_stop = fail_stop

    def request(self, method, url, headers=None, timeout=None, **kw):
        assert url.startswith("https://paper-api.alpaca.markets/")
        self.calls.append((method, url, kw))
        path = url.split(".markets")[1]
        if method == "POST":
            if self.fail_stop and kw["json"]["type"] == "stop_limit":
                raise OSError("rejected")
            return FakeResponse({"id": f"o{len(self.calls)}"})
        if path == "/v2/account":
            return FakeResponse(self.account)
        if path == "/v2/positions":
            return FakeResponse(self.positions)
        if path == "/v2/orders":
            return FakeResponse(self.closed if kw["params"]["status"] == "closed" else self.orders)
        return FakeResponse(None)


def broker(http):
    return trader.AlpacaPaper(session=http, keys=("k", "s"))


def test_good_plan_is_approved_and_sized_to_half_percent():
    ok, no = review(plan())
    assert len(ok) == 1 and not no
    assert ok[0]["qty"] == 40  # min($500/$10=50株, 20%上限 $20000/$500=40株)
    assert ok[0]["qty"] * 10 <= C.CAPITAL * C.RISK_PER_TRADE


@pytest.mark.parametrize("bad,text", [
    (dict(symbol="TQQQ"), "対象外"),
    (dict(symbol="GME"), "対象外"),
    (dict(side="sell"), "空売り"),
    (dict(stop=None), "ストップ"),
    (dict(stop=505.0), "ストップ"),
    (dict(score=6), "スコア"),
    (dict(price=9.5), "未満"),
    (dict(dollar_volume=1e6), "売買代金"),
    (dict(symbol="AAPL", asset_class="stock", earnings_blocked=True), "決算"),
])
def test_veto_cases(bad, text):
    ok, no = review(plan(**bad))
    assert not ok
    assert any(text in r for r in no[0]["veto_reasons"])


def test_missing_earnings_flag_defaults_to_veto():
    p = plan(symbol="AAPL", asset_class="stock")
    del p["earnings_blocked"]
    ok, no = review(p)
    assert not ok


def test_veto_when_kill_switch_active():
    risk.fire_kill("test")
    ok, no = review(plan())
    assert not ok and "キルスイッチ" in no[0]["veto_reasons"][0]


def test_veto_when_open_risk_full_and_when_held():
    assert not review(plan(), open_risk=C.CAPITAL * C.MAX_OPEN_RISK)[0]
    assert not review(plan(), held={"SPY"})[0]


def test_new_champion_blocked_by_launch_gate(monkeypatch):
    monkeypatch.setattr(risk.events, "launch_gate", lambda now: (False, ["CPI の日程が未確認"]))
    ok, no = review(plan(), champion={"id": "x", "launched": False})
    assert not ok and "ローンチ不可" in no[0]["veto_reasons"][0]
    monkeypatch.setattr(risk.events, "launch_gate", lambda now: (_ for _ in ()).throw(AssertionError("呼ばれない")))
    assert review(plan(), champion={"id": "x", "launched": True})[0]


def test_vetoes_are_logged():
    review(plan(symbol="TQQQ"))
    assert [r for r in journal.read() if r["kind"] == "veto"]


def test_live_endpoint_override_refused(monkeypatch):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(trader.LiveEndpointRefused):
        trader.AlpacaPaper()


def test_no_live_url_anywhere_in_package():
    for f in Path(trader.__file__).parent.glob("*.py"):
        assert "//api.alpaca.markets" not in f.read_text(encoding="utf-8"), f


def test_only_trader_posts_orders():
    """発注に触れるのは Trader だけ（Desk Rules）。"""
    for f in Path(trader.__file__).parent.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        if f.name != "trader.py":
            assert "/v2/orders" not in text and "post_order" not in text, f


def test_no_keys_means_no_orders_and_no_fake_fills():
    ok, _ = review(plan())
    res = trader.submit(ok, trader.AlpacaPaper(keys=(None, None)))
    assert res[0]["status"] == "not_sent_no_keys"
    kinds = [r["kind"] for r in journal.read()]
    assert "order_not_sent" in kinds and "fill_opened" not in kinds


def test_unapproved_or_tampered_plan_refused():
    ok, _ = review(plan())
    http = FakeHttp()
    tampered = dict(ok[0], qty=ok[0]["qty"] * 100)
    assert trader.submit([tampered], broker(http))[0]["status"] == "refused"
    assert trader.submit([plan()], broker(http))[0]["status"] == "refused"
    assert not http.calls


def test_kill_switch_stops_trader_even_with_approved_plan():
    ok, _ = review(plan())
    risk.fire_kill("x")
    http = FakeHttp()
    assert trader.submit(ok, broker(http))[0]["status"] == "refused"
    assert not http.calls


def test_equity_order_is_limit_with_attached_stop():
    ok, _ = review(plan())
    http = FakeHttp()
    trader.submit(ok, broker(http))
    body = http.calls[0][2]["json"]
    assert body["type"] == "limit" and body["side"] == "buy" and body["order_class"] == "oto"
    assert body["stop_loss"]["stop_price"] == "490.00" and body["qty"] == "40"


def test_entry_body_rejects_sell():
    with pytest.raises(ValueError):
        trader.entry_order_body(plan(side="sell", qty=1.0))


def crypto_book():
    return {"C-1": {**plan(plan_id="C-1", symbol="BTC-USD", asset_class="crypto", qty=0.1, limit=70000.0, stop=66000.0), "order_id": "o", "state": "working", "submitted_utc": "2026-11-09T00:10:00+00:00"}}


def test_crypto_stop_placed_after_fill():
    trader._save_positions(crypto_book())
    http = FakeHttp(positions=[{"symbol": "BTCUSD", "qty": "0.1", "avg_entry_price": "69900"}], orders=[])
    res = trader.monitor(broker(http), NOW)
    assert res["open"] == 1
    posts = [c for c in http.calls if c[0] == "POST"]
    assert posts[0][2]["json"]["type"] == "stop_limit" and posts[0][2]["json"]["side"] == "sell"
    assert posts[0][2]["json"]["stop_price"] == "66000.00"


def test_crypto_exits_if_stop_cannot_be_placed():
    trader._save_positions(crypto_book())
    http = FakeHttp(positions=[{"symbol": "BTCUSD", "qty": "0.1", "avg_entry_price": "69900"}], fail_stop=True)
    trader.monitor(broker(http), NOW)
    posts = [c[2]["json"] for c in http.calls if c[0] == "POST"]
    assert posts[-1]["type"] == "market" and posts[-1]["side"] == "sell"
    assert any(r["kind"] == "emergency_exit" for r in journal.read())


def test_daily_loss_fires_kill():
    http = FakeHttp(account={"equity": "97900", "last_equity": "100000"})
    trader.monitor(broker(http), NOW)
    assert risk.kill_status()["active"]


def test_three_losses_fire_kill():
    for pnl in (-10, -10, -10):
        journal.log("fill_closed", "Trader", pnl=pnl, r=-1.0, strategy="x")
    trader.monitor(broker(FakeHttp()), NOW)
    assert "連敗" in risk.kill_status()["reason"]
    assert trader.consecutive_losses([{"pnl": -1}, {"pnl": 5}, {"pnl": -1}]) == 1


def test_stale_api_fires_kill_after_three_failures():
    class Broken(FakeHttp):
        def request(self, *a, **k):
            raise OSError("down")
    b = broker(Broken())
    for _ in range(3):
        assert trader.monitor(b, NOW)["status"] == "api_error"
    assert "データ停止" in risk.kill_status()["reason"]


def test_close_is_recorded_only_with_confirmed_price():
    book = crypto_book()
    book["C-1"].update(state="open", entry_price=69900.0, filled_qty=0.1, opened_utc="2026-11-09T01:00:00+00:00", stop_order_id="s")
    trader._save_positions(book)
    trader.monitor(broker(FakeHttp(positions=[], closed=[])), NOW)
    assert any(r["kind"] == "close_unknown" for r in journal.read())
    assert not [r for r in journal.read() if r["kind"] == "fill_closed"]

    trader._save_positions(book)
    trader.monitor(broker(FakeHttp(positions=[], closed=[{"side": "sell", "filled_avg_price": "66000"}])), NOW)
    closed = [r for r in journal.read() if r["kind"] == "fill_closed"]
    assert closed[0]["pnl"] == -390.0 and closed[0]["r"] == -1.0


def test_exit_only_sells_held_quantity():
    book = crypto_book()
    book["C-1"].update(state="open", entry_price=69900.0, filled_qty=0.1)
    trader._save_positions(book)
    http = FakeHttp(orders=[{"id": "s1", "symbol": "BTC/USD", "side": "sell"}])
    trader.exit_position("C-1", "time", broker(http))
    assert any(c[0] == "DELETE" and c[1].endswith("/v2/orders/s1") for c in http.calls)
    sell = [c[2]["json"] for c in http.calls if c[0] == "POST"][0]
    assert sell["side"] == "sell" and sell["qty"] == "0.100000"


def test_tick_runs_each_job_once_per_day(monkeypatch):
    monkeypatch.setattr(desk, "TICKS", C.STATE / "ticks.json")
    monkeypatch.setattr(desk, "APPROVED", C.STATE / "approved.json")
    monkeypatch.setattr(scout, "collect", lambda now: {"new": 0})
    calls = []
    monkeypatch.setattr(desk, "job_signals", lambda classes, now, submit, broker: calls.append(sorted(classes)) or {"status": "x"})
    b = trader.AlpacaPaper(keys=(None, None))
    # 月曜 NY 17:00 = 株のシグナル後。暗号資産も実行される
    t = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)
    desk.tick(t, b)
    desk.tick(t, b)
    assert calls.count(["etf", "stock"]) == 1
    assert calls.count(["crypto"]) == 1


def test_tick_skips_equity_jobs_on_weekend(monkeypatch):
    monkeypatch.setattr(desk, "TICKS", C.STATE / "ticks.json")
    monkeypatch.setattr(scout, "collect", lambda now: {"new": 0})
    calls = []
    monkeypatch.setattr(desk, "job_signals", lambda classes, now, submit, broker: calls.append(sorted(classes)) or {})
    desk.tick(datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc), trader.AlpacaPaper(keys=(None, None)))
    assert calls == [["crypto"]]


def test_tick_health_reports_a_stopped_container():
    assert desk._tick_health(NOW)["healthy"] is False  # ログなし
    with open(C.STATE / "tick.log", "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-11-09T13:55:00+00:00"}) + "\n")
    assert desk._tick_health(NOW) == {"healthy": True, "last_tick_utc": "2026-11-09T13:55:00+00:00", "age_minutes": 5.0}
    assert desk._tick_health(datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc))["healthy"] is False


def test_tick_is_skipped_while_another_is_running(monkeypatch):
    """Windowsタスクとコンテナの二重起動で、同じ処理を2回走らせない。"""
    called = []
    monkeypatch.setattr(desk, "_tick", lambda now, broker: called.append(1) or {"ok": True})
    (C.STATE / "tick.lock").write_text("")
    assert desk.tick() == {"status": "skipped_locked"}
    assert not called
    (C.STATE / "tick.lock").unlink()
    assert desk.tick() == {"ok": True}
    assert not (C.STATE / "tick.lock").exists()


def test_stale_lock_is_recovered(monkeypatch):
    import os
    monkeypatch.setattr(desk, "_tick", lambda now, broker: {"ok": True})
    lock = C.STATE / "tick.lock"
    lock.write_text("")
    old = lock.stat().st_mtime - desk.LOCK_STALE_SECONDS - 10
    os.utime(lock, (old, old))
    assert desk.tick() == {"ok": True}


def test_lock_released_when_tick_fails(monkeypatch):
    def boom(now, broker):
        raise OSError("x")
    monkeypatch.setattr(desk, "_tick", boom)
    with pytest.raises(OSError):
        desk.tick()
    assert not (C.STATE / "tick.lock").exists()


def test_loop_survives_a_failed_tick(monkeypatch, capsys):
    results = [OSError("x"), {"ok": True}]

    def fake(*a, **k):
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(desk, "tick", fake)
    sleeps = []
    desk.run_loop(300, max_iterations=2, sleep=sleeps.append)
    assert not results and sleeps == [300]
    assert '"ok": true' in capsys.readouterr().out


def test_scout_keeps_failures_visible_and_dedupes(monkeypatch, tmp_path):
    monkeypatch.setattr(scout, "OUT", tmp_path / "scout")
    rss = "<rss><channel><item><title>Hi</title><link>http://a/1</link><pubDate>x</pubDate></item></channel></rss>"

    class Http:
        def get(self, url, headers=None, timeout=None):
            if "federalreserve" in url:
                raise OSError("down")
            r = FakeResponse(None)
            r.content = rss.encode()
            return r
    res = scout.collect(NOW, Http())
    assert res["new"] == 1
    assert any("Federal Reserve" in f for f in res["failures"])
    assert scout.collect(NOW, Http())["new"] == 0
    assert json.loads((tmp_path / "scout" / "2026-11-09.jsonl").read_text(encoding="utf-8").splitlines()[0])["title"] == "Hi"
