"""pytest 共通の設定とテスト用データの作成関数。

【役割】すべてのテストで本物の state/・journal/・.env・環境変数のキーに触れないよう隔離し、
  バックテストや戦略のテストで使う合成の日足とシグナルを作る。
【位置付け】pytest.ini の pythonpath に tests を入れているので、各テストは `from conftest import ...` で関数を直接使える。
【主な依存先】numpy、pandas、pytest、hedgedesk の config / journal / risk / trader。
"""
import numpy as np
import pandas as pd
import pytest

from hedgedesk import config as C, journal, risk, scout, trader


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """テストが本物のジャーナルや状態ファイルに触れないようにする。"""
    # autouse=True なので全テストに自動で適用される。
    # モジュールが import 時にパスを定数として持っているもの（JOURNAL、KILL_FILE、POSITIONS など）は個別に差し替え、
    # 実行時に C.STATE を参照するものは C.STATE の差し替えで tmp_path に向ける。
    # Alpaca のキーと接続先の環境変数も消し、開発者の PC の設定でテスト結果が変わらないようにする。
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "journal.jsonl")
    monkeypatch.setattr(risk, "KILL_FILE", tmp_path / "kill.json")
    monkeypatch.setattr(trader, "POSITIONS", tmp_path / "positions.json")
    monkeypatch.setattr(trader, "FAILS", tmp_path / "fails.json")
    monkeypatch.setattr(trader, "ENV_FILE", tmp_path / "no.env")
    monkeypatch.setattr(C, "STATE", tmp_path)
    # Scout の保存先は import 時に決まるので個別に差し替える（ニュース分析が本物の見出しを読まないように）。
    monkeypatch.setattr(scout, "OUT", tmp_path / "scout")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("APCA_API_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HEDGEDESK_NEWS_AI", raising=False)
    # 画面の公開設定（Railway の PORT とパスワード）も消し、ローカルの既定で検査する。
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("HEDGEDESK_UI_PASSWORD", raising=False)
    monkeypatch.delenv("HEDGEDESK_UI_USER", raising=False)


def make_bars(rows, start="2024-01-01"):
    """(open, high, low, close) の並びから日足を作る。"""
    # 引数: rows は [[始値, 高値, 安値, 終値], ...]、start は最初の営業日。
    # 戻り値: 営業日のインデックスを持ち、出来高100万・売買代金=終値×出来高 の列を加えた DataFrame。
    idx = pd.bdate_range(start, periods=len(rows))
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    df["volume"] = 1_000_000.0
    df["dollar_volume"] = df["close"] * df["volume"]
    return df


def make_signals(df, entries=(), exits=(), limit=None, stop=None, max_hold=10):
    """backtest.simulate に渡すシグナルを手で組み立てる。

    Args:
        df: 日足（インデックスを使う）。entries / exits: 新規・手仕舞いのシグナルを立てる行番号。
        limit / stop: 指値とストップ（数値なら全日同じ値、Series なら日ごと）。max_hold: 最大保有本数。

    Returns:
        entry / exit / limit / stop / rank 列と attrs["max_hold"] を持つ DataFrame（rank は全日0）。
    """
    sig = pd.DataFrame(index=df.index)
    sig["entry"] = 0.0
    sig["exit"] = 0.0
    sig["limit"] = limit
    sig["stop"] = stop
    sig["rank"] = 0.0
    for i in entries:
        sig.iloc[i, sig.columns.get_loc("entry")] = 1.0
    for i in exits:
        sig.iloc[i, sig.columns.get_loc("exit")] = 1.0
    sig.attrs["max_hold"] = max_hold
    return sig


def random_walk(seed, n=900, start_price=100.0, drift=0.0, vol=0.012):
    """乱数で幾何ランダムウォークの日足を作る（2018-01-01 から n 営業日）。

    Args:
        seed: 乱数シード（同じ値なら同じデータ）。n: 本数。start_price: 初値。
        drift / vol: 日次の対数リターンの平均と標準偏差。

    Returns:
        open / high / low / close / volume / dollar_volume 列の DataFrame。
        高値は始値・終値の大きいほうより上、安値は小さいほうより下になるよう作るので、OHLC の整合が保たれる。
    """
    rng = np.random.default_rng(seed)
    close = start_price * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    opn = np.concatenate(([start_price], close[:-1])) * np.exp(rng.normal(0, vol / 3, n))
    high = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    idx = pd.bdate_range("2018-01-01", periods=n)
    volume = rng.lognormal(15, 0.5, n)
    return pd.DataFrame({"open": opn, "high": high, "low": low, "close": close, "volume": volume, "dollar_volume": close * volume}, index=idx)
