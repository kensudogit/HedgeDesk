"""サービス画面が読み取り専用で、キーを漏らさないことを確かめる。"""
#
# 【対象】hedgedesk/ui.py。画面が読み取り専用で、キーを返さず、更新依頼の POST だけを条件付きで受けることを確かめる。
# 【方針】ポート0（空いているポート）で実際に HTTP サーバーを別スレッドで起動し、urllib で叩く。
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from hedgedesk import config as C, journal, scout, trader, ui


@pytest.fixture
def server(tmp_path, monkeypatch):
    """ブリーフ・依頼・見出しの保存先を一時ディレクトリに向けて画面のサーバーを起動し、ベース URL を返す。

    テストが終わったら shutdown で止める（daemon スレッドなので、止め損ねてもテストプロセスの終了は妨げない）。
    """
    monkeypatch.setattr(C, "BRIEFS", tmp_path / "briefs")
    monkeypatch.setattr(C, "REQUESTS", tmp_path / "requests")
    monkeypatch.setattr(scout, "OUT", tmp_path / "scout")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_binds_to_loopback_only():
    """環境変数を設定しない既定では、このPCからしか接続できない 127.0.0.1 で待ち受ける。"""
    assert ui.HOST == "127.0.0.1"


def test_status_works_with_empty_state(server):
    """状態ファイルが何もなくても /api/status は正常に応答する。"""
    s = json.load(urlopen(server + "/api/status"))
    assert s["strategies"] == [] and s["champion"] is None and s["tick_healthy"] is False


def test_status_never_contains_keys(server, monkeypatch):
    """キーがある場合も、応答にはキーの有無（True）だけが入り、キーの文字列は含まれない。"""
    monkeypatch.setattr(trader, "load_keys", lambda: ("KEY-SECRET-1", "KEY-SECRET-2"))
    body = urlopen(server + "/api/status").read().decode("utf-8")
    assert json.loads(body)["alpaca_keys"] is True
    assert "KEY-SECRET" not in body


def post(server, path="/api/refresh", headers=None):
    """POST を送り、(HTTP ステータス, 本文) を返す。2xx 以外は本文をバイト列のまま返す。"""
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
    """Origin が別のサイトなら、独自ヘッダーがあっても拒否する。"""
    assert post(server, headers={"X-HedgeDesk-Action": "refresh", "Origin": "https://evil.example"})[0] == 403


def test_refresh_queues_once_and_reports_state(server):
    """依頼は1回だけ受け付け（202）、待機中の2回目は 409。状態は /api/status に反映される。"""
    code, body = post(server)
    assert code == 202 and body["pending"] is True
    assert post(server)[0] == 409
    s = json.load(urlopen(server + "/api/status"))
    assert s["refresh"]["pending"] is True


def test_refresh_too_soon_after_research(server):
    """直前に調査が終わっていれば 429（10分以内の連続実行を防ぐ）。"""
    from datetime import datetime, timezone
    (C.STATE / "latest_research.json").write_text(json.dumps({"as_of_utc": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    code, body = post(server)
    assert code == 429


def test_only_refresh_endpoint_accepts_post(server):
    """POST を受けるのは /api/refresh だけ。キルスイッチなどの操作の経路はない。"""
    assert post(server, path="/api/status")[0] == 404
    assert post(server, path="/api/kill")[0] == 404


def test_page_has_no_html_injection_path():
    """画面の JavaScript が innerHTML などの HTML を解釈させる書き方をしていない（外部の文字列の埋め込み対策）。"""
    assert "innerHTML" not in ui.PAGE and "document.write" not in ui.PAGE


def test_unknown_path_is_404(server):
    """state/ のファイルなど、決められたパス以外は 404。"""
    with pytest.raises(HTTPError) as e:
        urlopen(server + "/state/kill.json")
    assert e.value.code == 404


def test_logo_is_served_and_other_public_files_are_not(server, tmp_path, monkeypatch):
    """/logo.png はロゴの PNG だけを返す。public/ のほかのファイルや、ロゴがない場合は 404。"""
    with urlopen(server + "/logo.png") as r:
        assert r.headers["Content-Type"] == "image/png" and r.read(8) == b"\x89PNG\r\n\x1a\n"
    for path in ("/public/manifest.json", "/public/PC.png", "/logo.png/../.env"):
        with pytest.raises(HTTPError) as e:
            urlopen(server + path)
        assert e.value.code == 404
    monkeypatch.setattr(ui, "LOGO", tmp_path / "none.png")
    with pytest.raises(HTTPError) as e:
        urlopen(server + "/logo.png")
    assert e.value.code == 404


def test_usage_guide_palette_is_static_and_safe():
    """利用手順のパレットは静的なマークアップで、主要な手順を含み、キーの値や外部スクリプトを含まない。"""
    page = ui.PAGE
    assert 'id="guide"' in page and 'id="guidebtn"' in page and "ARCHITECTURE &amp; OPS" in page
    for step in ("docker compose --profile tick up -d --build", "--force-recreate tick", "reset-kill", "docker compose --profile tick down"):
        assert step in page
    assert "<script src" not in page and "sk-ant-" not in page
    # localStorage は位置と開閉の記憶だけで、失敗しても画面が動くよう try で囲む。
    assert "try{localStorage.setItem" in page


def _get(server, path, auth=None):
    """GET を送り、(HTTP ステータス, ヘッダー, 本文) を返す。auth は (ユーザー, パスワード)。"""
    import base64

    headers = {}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    try:
        r = urlopen(Request(server + path, headers=headers))
        return r.status, r.headers, r.read()
    except HTTPError as e:
        return e.code, e.headers, e.read()


def test_public_deploy_without_password_shows_nothing(server, monkeypatch):
    """Railway など（PORT あり）でパスワードがなければ、画面も状況も返さない。死活確認だけは通す。"""
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.delenv("HEDGEDESK_UI_PASSWORD", raising=False)
    for path in ("/", "/api/status", "/logo.png"):
        code, _, body = _get(server, path)
        assert code == 503 and b"equity" not in body
    assert _get(server, "/healthz")[0] == 200
    assert post(server)[0] == 503


def test_password_requires_basic_auth(server, monkeypatch):
    """パスワードを設定すると Basic 認証が必要になる。正しい組み合わせだけ通る。"""
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("HEDGEDESK_UI_PASSWORD", "s3cret-test")
    code, headers, _ = _get(server, "/api/status")
    assert code == 401 and "Basic" in headers["WWW-Authenticate"]
    assert _get(server, "/api/status", ("desk", "wrong"))[0] == 401
    assert _get(server, "/api/status", ("other", "s3cret-test"))[0] == 401
    code, _, body = _get(server, "/api/status", ("desk", "s3cret-test"))
    assert code == 200 and json.loads(body)["alpaca_keys"] is False
    assert _get(server, "/healthz")[0] == 200


def test_local_default_has_no_password_and_fixed_port(server, monkeypatch):
    """ローカル（PORT なし・パスワードなし）は今までどおり認証なしで見られる。"""
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("HEDGEDESK_UI_PASSWORD", raising=False)
    assert _get(server, "/api/status")[0] == 200
    assert ui.PORT == 8765


def test_demo_serves_saved_snapshot_without_auth_and_never_reads_state(server, monkeypatch, tmp_path):
    """デモは保存した見本だけを返す。公開環境でも認証なしで見られ、実データ（status）は読まず、更新も受け付けない。"""
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps({"now_utc": "2026-11-10T03:00:00+00:00", "strategies": [], "alpaca_keys": True, "refresh": {"pending": True}}), encoding="utf-8")
    monkeypatch.setattr(ui, "DEMO_SNAPSHOT", snap)
    monkeypatch.setattr(ui, "status", lambda now=None: (_ for _ in ()).throw(AssertionError("デモで実データを読んだ")))
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("HEDGEDESK_DEMO", "1")
    monkeypatch.delenv("HEDGEDESK_UI_PASSWORD", raising=False)
    code, _, body = _get(server, "/api/status")
    data = json.loads(body)
    assert code == 200 and data["demo"] == {"as_of_utc": "2026-11-10T03:00:00+00:00"} and data["refresh"] == {}
    assert _get(server, "/")[0] == 200
    assert post(server)[0] == 403


def test_demo_snapshot_drops_internal_details(monkeypatch, tmp_path):
    """見本には保有台帳・更新依頼・エラーの詳細を載せず、件数を絞る。キーの値は status と同じく含まない。"""
    monkeypatch.setattr(ui, "DEMO_SNAPSHOT", tmp_path / "demo" / "snapshot.json")
    journal.log("job_error", "Chief of Staff", job="research", error="Traceback (most recent call last) ...")
    for i in range(20):
        journal.log("research", "Strategist", IDEA=f"S{i}")
    snap = ui.make_demo_snapshot()
    assert snap["open_book"] == [] and snap["refresh"] == {}
    assert len(snap["journal"]) == ui.DEMO_JOURNAL_LIMIT and all("error" not in r for r in snap["journal"])
    assert json.loads((tmp_path / "demo" / "snapshot.json").read_text(encoding="utf-8"))["now_utc"] == snap["now_utc"]


def test_page_has_demo_banner_filled_as_text():
    """デモの注記は静的な要素に textContent で入れる（HTML として組み立てない）。"""
    assert 'id="demo" role="note" hidden' in ui.PAGE
    assert "$('demo').textContent=" in ui.PAGE
