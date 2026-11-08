"""サービス画面が読み取り専用で、キーを漏らさないことを確かめる。"""
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from hedgedesk import config as C, scout, trader, ui


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "BRIEFS", tmp_path / "briefs")
    monkeypatch.setattr(C, "REQUESTS", tmp_path / "requests")
    monkeypatch.setattr(scout, "OUT", tmp_path / "scout")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_binds_to_loopback_only():
    assert ui.HOST == "127.0.0.1"


def test_status_works_with_empty_state(server):
    s = json.load(urlopen(server + "/api/status"))
    assert s["strategies"] == [] and s["champion"] is None and s["tick_healthy"] is False


def test_status_never_contains_keys(server, monkeypatch):
    monkeypatch.setattr(trader, "load_keys", lambda: ("KEY-SECRET-1", "KEY-SECRET-2"))
    body = urlopen(server + "/api/status").read().decode("utf-8")
    assert json.loads(body)["alpaca_keys"] is True
    assert "KEY-SECRET" not in body


def post(server, path="/api/refresh", headers=None):
    req = Request(server + path, data=b"", method="POST", headers=headers if headers is not None else {"X-HedgeDesk-Action": "refresh"})
    try:
        r = urlopen(req)
        return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, e.read()


def test_refresh_requires_custom_header(server):
    """ほかのサイトのフォーム送信（独自ヘッダーを付けられない）は拒否する。"""
    assert post(server, headers={})[0] == 403
    assert post(server, headers={"X-HedgeDesk-Action": "x"})[0] == 403
    assert not (C.REQUESTS / "research.req").exists()


def test_refresh_rejects_other_origin(server):
    assert post(server, headers={"X-HedgeDesk-Action": "refresh", "Origin": "https://evil.example"})[0] == 403


def test_refresh_queues_once_and_reports_state(server):
    code, body = post(server)
    assert code == 202 and body["pending"] is True
    assert post(server)[0] == 409
    s = json.load(urlopen(server + "/api/status"))
    assert s["refresh"]["pending"] is True


def test_refresh_too_soon_after_research(server):
    from datetime import datetime, timezone
    (C.STATE / "latest_research.json").write_text(json.dumps({"as_of_utc": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    code, body = post(server)
    assert code == 429


def test_only_refresh_endpoint_accepts_post(server):
    assert post(server, path="/api/status")[0] == 404
    assert post(server, path="/api/kill")[0] == 404


def test_page_has_no_html_injection_path():
    assert "innerHTML" not in ui.PAGE and "document.write" not in ui.PAGE


def test_unknown_path_is_404(server):
    with pytest.raises(HTTPError) as e:
        urlopen(server + "/state/kill.json")
    assert e.value.code == 404
