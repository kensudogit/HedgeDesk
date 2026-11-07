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


def test_post_is_not_available(server):
    with pytest.raises(HTTPError) as e:
        urlopen(Request(server + "/api/status", data=b"{}", method="POST"))
    assert e.value.code == 501


def test_page_has_no_html_injection_path():
    assert "innerHTML" not in ui.PAGE and "document.write" not in ui.PAGE


def test_unknown_path_is_404(server):
    with pytest.raises(HTTPError) as e:
        urlopen(server + "/state/kill.json")
    assert e.value.code == 404
