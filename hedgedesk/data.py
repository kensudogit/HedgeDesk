"""市場データの取得と品質確認。出所は Yahoo Finance（yfinance、非公式経路）。

注意:
- Yahooの Close は分割調整済みで配当は未調整。OHLCは Adj Close に合わせて配当も調整する。
- 未確定の足（当日の取引中の足）は必ず落とす。ここを誤ると先読みになる。
"""
#
# 【役割】Yahoo Finance から日足と決算日を取得し、調整・未確定足の除去・品質確認・キャッシュを行う。
# 【位置付け】research.build_context が唯一の主な呼び出し元（調査、Chart Analyst、ブリーフの市場データすべてがここを通る）。
#   analyst と desk は NY（ニューヨーク時間）だけをここから借りている。
# 【主な依存先】yfinance（関数内で import。テストでは sys.modules に偽物を差し込む）、pandas、numpy、config。
# 【キャッシュの構成】data/cache/ に
#   - <銘柄>_<UTC日付>.pkl … その日に取得したスナップショット（同じ日の2回目以降はこれを使う）
#   - latest_<銘柄>.pkl     … 直近の値。stabilize() で取得ごとの微小な揺れを吸収するための比較元
#   - earn_<銘柄>_<UTC日付>.pkl … 決算日
#   銘柄ごとに別ファイルにしている（2026-11-07 に1ファイルへ上書きされ VIX の中身が別銘柄になった不具合の再発防止）。
from __future__ import annotations

import hashlib
import os
import warnings
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .config import CACHE, DATA_START

# 米国市場の時刻判定に使う。夏時間の切り替えは ZoneInfo が自動で扱う。
NY = ZoneInfo("America/New_York")
SOURCE = "Yahoo Finance via yfinance"
VIX_SYMBOL = "^VIX"
STABLE_TOLERANCE = 1e-5     # 許容する相対差。実測の揺れは1e-6程度で、配当の調整はこれより十分大きい
STABLE_RECENT_BARS = 5      # 直近の足は未確定や訂正があり得るので、常に新しい値を使う


def _adjust(raw: pd.DataFrame) -> pd.DataFrame:
    """配当込みに調整した OHLC を作る。売買代金は分割の影響を受けない値を残す。"""
    # 引数: raw は yfinance の生データ（Open / High / Low / Close / Adj Close / Volume 列）。
    # 戻り値: open / high / low / close / volume / dollar_volume 列、タイムゾーンなしの日付インデックスの DataFrame。
    # 調整係数 = Adj Close ÷ Close を始値・高値・安値にも掛け、ローソク足の形を保ったまま配当分を調整する。
    # 売買代金は「その日の実際の価格 × 出来高」で、流動性の判定に使うので調整前の Close を使う。
    factor = raw["Adj Close"] / raw["Close"]
    df = pd.DataFrame(index=raw.index)
    df["open"] = raw["Open"] * factor
    df["high"] = raw["High"] * factor
    df["low"] = raw["Low"] * factor
    df["close"] = raw["Adj Close"]
    df["volume"] = raw["Volume"]
    df["dollar_volume"] = raw["Close"] * raw["Volume"]
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    # 銘柄によってタイムゾーン付き・なしが混ざるので、すべてタイムゾーンなしの日付に揃える（銘柄間で同じ日を突き合わせるため）。
    return df


def drop_incomplete(df: pd.DataFrame, symbol: str, now: datetime | None = None) -> pd.DataFrame:
    """確定していない最新足を除く。暗号資産はUTC日付、株はNY時間16:15が境目。"""
    # 引数: df は日足、symbol は銘柄（"-USD" で終われば暗号資産とみなす）、now は判定時刻。
    # 戻り値: 未確定の足を除いた DataFrame（確定済みならそのまま）。
    # 暗号資産: Yahoo の日足は UTC の0時区切りなので、UTC の当日の足は取引中＝未確定。
    # 株・ETF: 大引け16:00の後、データの確定を待つため15分の余裕を見て16:15以降に当日足を採用する。
    #   最新の足の日付が NY の今日より先（タイムゾーンのずれ）の場合も、今日以降の足を落とす。
    if df.empty:
        return df
    now = now or datetime.now(timezone.utc)
    last = df.index[-1].date()
    if symbol.endswith("-USD"):
        return df[df.index.date < now.astimezone(timezone.utc).date()]
    ny = now.astimezone(NY)
    if last > ny.date() or (last == ny.date() and ny.time() < time(16, 15)):
        return df[df.index.date < ny.date()]
    return df


def quality_issues(df: pd.DataFrame) -> list[str]:
    """データの不備を文字列で返す。空なら問題なし。"""
    # 引数: df は _adjust 後の日足。
    # 戻り値: 不備の説明のリスト。データなし、NaN、0以下の価格、高値・安値と始値・終値の矛盾、日付の並び・重複を検査する。
    # 副作用: なし（不備があっても例外にはせず、レポートとブリーフに載せて人が確認する）。
    if df.empty:
        return ["no data"]
    out = []
    core = df[["open", "high", "low", "close"]]
    if core.isna().any(axis=None):
        out.append(f"NaN rows={int(core.isna().any(axis=1).sum())}")
    if (core.dropna() <= 0).any(axis=None):
        out.append("non-positive prices")
    tol = 1e-6 * df["close"]
    # 調整係数の掛け算による丸め誤差で、高値がわずかに終値を下回ることがあるので、百万分の1の許容差を置く。
    if ((df["high"] + tol < df[["open", "close"]].max(axis=1)) | (df["low"] - tol > df[["open", "close"]].min(axis=1))).any():
        out.append("OHLC inconsistent")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        out.append("bad index")
    return out


def _cache_path(symbol: str, stamp: str):
    """日付付きのスナップショットのパス。"^VIX" の "^" はファイル名に使いにくいので "_" に置き換える。"""
    return CACHE / f"{symbol.replace('^', '_')}_{stamp}.pkl"


def _save(df: pd.DataFrame, path) -> None:
    """書きかけのファイルを別のコンテナが読まないよう、一時ファイル経由で置き換える。"""
    # 一時ファイル名にプロセス ID を入れ、tick と jobs が同時に書いても一時ファイルが衝突しないようにする。
    # os.replace は同じディレクトリ内なら原子的なので、読む側は古いファイルか新しいファイルの完全な方だけを見る。
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    df.to_pickle(tmp)
    os.replace(tmp, path)


def _latest_path(symbol: str):
    """stabilize() の比較元にする「直近の値」のパス。"""
    return CACHE / f"latest_{symbol.replace('^', '_')}.pkl"


def stabilize(old: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """取得ごとの微小な揺れを吸収する。(データ, 過去の値が本当に変わったか) を返す。

    Yahooの調整後価格は、同じ日でも取得ごとに百万分の1程度揺れ、境界付近の判定を変えてしまう。
    過去の足が許容差内で同じなら前回の値を使い続け、直近の足だけ新しい値にする。
    配当、分割、訂正で本当に変わったときは、新しい値にすべて置き換える。
    """
    # 引数: old は前回保存した latest、new は今回取得した値（どちらも _adjust 済み）。
    # 戻り値: (採用するデータ, 過去の値が許容差を超えて変わったか)。
    # 判定の境目 cut は old の末尾から STABLE_RECENT_BARS 本目の日付。それより前を「過去」として比較する。
    cut = old.index[-STABLE_RECENT_BARS] if len(old) > STABLE_RECENT_BARS else old.index[0]
    past = old.index[old.index < cut]
    if len(past) == 0 or not past.isin(new.index).all():
        # 比較できる過去がない、または過去の日付が今回のデータから欠けている（取得範囲が変わった）ときは新しい値を全部採用する。
        return new, True
    cols = ["open", "high", "low", "close", "volume"]
    a, b = old.loc[past, cols], new.loc[past, cols]
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = ((a - b).abs() / a.abs().where(a != 0, 1.0)).to_numpy()
    # 相対差 |old − new| / |old|。old が0（出来高0の日など）は分母を1にして0除算を避ける。
    # NaN 同士の比較などで出る NaN は nan_to_num で0とみなす。
    if np.nan_to_num(rel).max() > STABLE_TOLERANCE:
        return new, True
    return pd.concat([old.loc[past], new[new.index >= cut]]), False
    # 過去は前回の値をそのまま使い、cut 以降（直近の足と新しく増えた足）だけ新しい値にする。


def _parse(raw: pd.DataFrame, s: str) -> pd.DataFrame:
    """yf.download の結果から1銘柄分を取り出して調整する。列がなければ空の DataFrame。"""
    try:
        sub = raw[s].dropna(subset=["Close"]) if isinstance(raw.columns, pd.MultiIndex) else raw.dropna(subset=["Close"])
        # 複数銘柄なら列が (銘柄, 項目) の MultiIndex、1銘柄だけなら単純な列になるので両方に対応する。
        if s == "^VIX":
            sub = sub.assign(Volume=0.0)
            # VIX は指数で出来高がない（NaN）ので0にしておく。
        return _adjust(sub)
    except KeyError:
        # 取得結果にその銘柄の列がない（上場廃止・取得失敗）なら空にする。空は quality_issues で "no data" になる。
        return pd.DataFrame()


def load(symbols: list[str], refresh: bool = False, now: datetime | None = None) -> tuple[dict[str, pd.DataFrame], dict]:
    """銘柄ごとのDataFrameと、取得記録（出所、期間、欠損、不備）を返す。"""
    # 引数:
    #   symbols: 取得する銘柄（Yahoo 表記）。refresh: True ならその日のキャッシュがあっても取り直す。
    #   now: 基準時刻（キャッシュの日付と未確定足の判定に使う）。
    # 戻り値: ({銘柄: 日足}, 取得記録)。取得記録には source、as_of_utc、銘柄ごとの first / last / bars / issues、
    #   fingerprint（全データの指紋）、取り直したときは history_changed（過去の値が本当に変わったか）が入る。
    # 副作用: data/cache/ にスナップショットと latest を保存する。
    # 例外: VIX の値が不自然なら check_vix が ValueError を出し、調査全体を止める。
    import yfinance as yf

    now = now or datetime.now(timezone.utc)
    CACHE.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d")
    out: dict[str, pd.DataFrame] = {}
    log: dict = {"source": SOURCE, "as_of_utc": now.isoformat(timespec="seconds"), "symbols": {}}
    missing = []
    for s in symbols:
        path = _cache_path(s, stamp)
        if path.exists() and not refresh:
            out[s] = pd.read_pickle(path)
        else:
            missing.append(s)
    if missing:
        # キャッシュのない銘柄だけをまとめて1回で取得する。threads=False は、並列取得で銘柄の取り違えや
        # レート制限が起きにくいようにするため。auto_adjust=False で Close と Adj Close の両方を受け取る（_adjust で使う）。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw = yf.download(missing, start=DATA_START, auto_adjust=False, progress=False, threads=False, group_by="ticker")
        for s in missing:
            df = _parse(raw, s)
            latest = _latest_path(s)
            if len(df) and latest.exists():
                df, changed = stabilize(pd.read_pickle(latest), df)
                log.setdefault("history_changed", {})[s] = changed
            if len(df):
                # 空データで latest を上書きしない（一時的な取得失敗で比較元を失わないため）。
                _save(df, latest)
            _save(df, _cache_path(s, stamp))
            # 日付付きのスナップショットは空でも保存する（同じ日の再実行で何度も取り直さないため）。
            out[s] = df
    for s in symbols:
        # 未確定足の除去はキャッシュ読み込み後に毎回行う。同じ日でも、取得時刻と今の時刻で「確定済み」の範囲が違うため。
        out[s] = drop_incomplete(out[s], s, now)
        df = out[s]
        log["symbols"][s] = {
            "first": str(df.index[0].date()) if len(df) else None,
            "last": str(df.index[-1].date()) if len(df) else None,
            "bars": len(df),
            "issues": quality_issues(df),
        }
    if VIX_SYMBOL in out:
        check_vix(out[VIX_SYMBOL])
    log["fingerprint"] = fingerprint(out)
    return out, log


# ---------------------------------------------------------------------------
# 直近の足の補充（ブリーフとニュース用）
# ---------------------------------------------------------------------------
# 夜間リサーチは 21:00 JST（NY の朝、取引前）に全銘柄を取り直すので、その日の米国の終値はキャッシュに入らない。
# 朝のブリーフ（07:15 JST）やニュースの価格反応がそのキャッシュを使うと、米国株が1営業日古くなる。
# 全銘柄を2018年から取り直すと1分以上かかるので、最新の確定足が欠けている銘柄だけ、直近1か月分を取って継ぎ足す。

RECENT_PERIOD = "1mo"
TOP_UP_INTERVAL_SECONDS = 3600   # 同じ銘柄の補充を試すのは1時間に1回まで（休場日に取り直し続けないため）


def expected_last_session(symbol: str, now: datetime) -> date:
    """now の時点で確定しているはずの最新の足の日付。

    暗号資産は UTC の前日。株・ETF は NY の平日で、16:15 を過ぎていればその日、前なら前の平日。
    米国の祝日は考えない（休場日には「前営業日の足がない」と判定されるが、取り直しても増えないので害はない）。
    """
    if symbol.endswith("-USD"):
        return now.astimezone(timezone.utc).date() - timedelta(days=1)
    ny = now.astimezone(NY)
    d = ny.date() if ny.time() >= time(16, 15) else ny.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def bar_close_utc(day: date, symbol: str) -> datetime:
    """日足1本が確定する時刻（UTC）。株・ETF は NY の16:00、暗号資産は翌日の UTC 0時。"""
    if symbol.endswith("-USD"):
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
    return datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY).astimezone(timezone.utc)


def _cached(symbol: str, now: datetime) -> tuple[pd.DataFrame | None, datetime | None]:
    """今日の日付付きスナップショット、なければ latest を読む。(データ, 保存時刻)。どちらもなければ (None, None)。"""
    for path in (_cache_path(symbol, now.strftime("%Y%m%d")), _latest_path(symbol)):
        if path.exists():
            df = pd.read_pickle(path)
            if len(df):
                return df, datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return None, None


def is_behind(df: pd.DataFrame | None, symbol: str, now: datetime, saved_at: datetime | None = None) -> bool:
    """確定しているはずの最新の足がないか、最新の足が取引中に保存された途中の値か。データがなければ True。

    saved_at（キャッシュの保存時刻）が最新の足の確定時刻より前なら、その足は途中の値なので取り直しが必要。
    例: 夜間リサーチ（12:00 UTC）が保存した暗号資産の当日の足は、翌日には日付だけ見ると確定済みに見えるが、値は昼の時点のもの。
    """
    if df is None or len(df) == 0:
        return True
    df = drop_incomplete(df, symbol, now)
    if len(df) == 0:
        return True
    last = df.index[-1].date()
    if last < expected_last_session(symbol, now):
        return True
    return saved_at is not None and saved_at < bar_close_utc(last, symbol)


def _behind(symbol: str, now: datetime) -> bool:
    df, saved_at = _cached(symbol, now)
    return is_behind(df, symbol, now, saved_at)


def _attempts_path():
    return CACHE / "top_up_attempts.json"


def top_up(symbols: list[str], now: datetime | None = None) -> dict:
    """最新の確定足が欠けている銘柄だけ、直近1か月分を取得してキャッシュに継ぎ足す。

    継ぎ足したデータは、今日の日付付きスナップショットと latest の両方に保存する
    （load(refresh=False) と news がどちらを読んでも新しい足が見えるように）。
    直近の取得は、その期間の配当調整を含めた最新の値で置き換える。それより前の足は夜間リサーチの全件取得に任せる。

    Returns:
        {"refreshed": 足が増えた銘柄, "still_behind": 取得しても足りない銘柄（休場か取得遅れ）, "skipped": 1時間以内に試した銘柄}。
    例外は出さない（取得の失敗は still_behind に残す）。
    """
    import json

    now = now or datetime.now(timezone.utc)
    behind = [s for s in dict.fromkeys(symbols) if _behind(s, now)]
    if not behind:
        return {"refreshed": [], "still_behind": [], "skipped": []}
    try:
        attempts = json.loads(_attempts_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        attempts = {}
    due, skipped = [], []
    for s in behind:
        last = attempts.get(s)
        if last and (now - datetime.fromisoformat(last)).total_seconds() < TOP_UP_INTERVAL_SECONDS:
            skipped.append(s)
        else:
            due.append(s)
    refreshed = []
    if due:
        CACHE.mkdir(parents=True, exist_ok=True)
        for s in due:
            attempts[s] = now.isoformat(timespec="seconds")
        _attempts_path().write_text(json.dumps(attempts), encoding="utf-8")
        try:
            import yfinance as yf

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                raw = yf.download(due, period=RECENT_PERIOD, auto_adjust=False, progress=False, threads=False, group_by="ticker")
        except Exception:  # noqa: BLE001 - 取得の失敗で呼び出し元（ブリーフ・ニュース）を止めない
            raw = None
        for s in due:
            new = _parse(raw, s) if raw is not None else pd.DataFrame()
            old, _ = _cached(s, now)
            if len(new) == 0 or old is None:
                continue
            merged = pd.concat([old[old.index < new.index[0]], new])
            _save(merged, _latest_path(s))
            _save(merged, _cache_path(s, now.strftime("%Y%m%d")))
            refreshed.append(s)
    still = [s for s in due if _behind(s, now)]
    return {"refreshed": refreshed, "still_behind": still, "skipped": skipped}


def check_vix(df: pd.DataFrame) -> None:
    """VIXの列に別の銘柄が混ざっていないか確かめる。おかしければ調査を止める。"""
    # VIX は長期的におおむね10〜40の範囲で動くので、中央値が8〜45を外れたら別の銘柄のデータとみなす。
    # Raises: ValueError（中央値が範囲外のとき）。空のデータは検査しない。
    if df.empty:
        return
    median = float(df["close"].median())
    if not 8 <= median <= 45:
        raise ValueError(f"VIXの中央値が{median:.1f}で不自然。データを確認するまで検証しない")


def fingerprint(frames: dict[str, pd.DataFrame]) -> str:
    """読み込んだ全データの指紋。同じ指紋なら同じ入力で検証したと確認できる。"""
    # 銘柄名の順に、日付と OHLCV の生のバイト列を SHA-256 に入れる。先頭16文字だけを返す（表示用に短くするため）。
    h = hashlib.sha256()
    for s in sorted(frames):
        df = frames[s]
        h.update(s.encode())
        if len(df):
            h.update(df.index.values.tobytes())
            h.update(df[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float).tobytes())
    return h.hexdigest()[:16]


def earnings_dates(symbols: list[str], refresh: bool = False) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """決算日を取得する。列: reaction(最初に反応する営業日), surprise(%)。取れない銘柄は失敗一覧に入れる。"""
    # 引数: symbols は個別株の一覧、refresh は True ならその日のキャッシュがあっても取り直す。
    # 戻り値: ({銘柄: reaction と surprise の DataFrame}, 取得に失敗した銘柄のリスト)。
    # 副作用: data/cache/earn_<銘柄>_<日付>.pkl に保存する。例外は銘柄ごとに捕まえて失敗一覧に入れる。
    import yfinance as yf

    CACHE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out, failed = {}, []
    for s in symbols:
        path = CACHE / f"earn_{s}_{stamp}.pkl"
        if path.exists() and not refresh:
            out[s] = pd.read_pickle(path)
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                raw = yf.Ticker(s).get_earnings_dates(limit=100)
            if raw is None or raw.empty:
                raise ValueError("empty")
            idx = raw.index.tz_convert(NY)
            # 寄付き前（0時より後、9時台前半まで）の発表だけ当日に反応とする。
            # 発表時刻が不明（0時）や取引時間中は、サプライズを終値判定に使うと先読みになるので翌営業日に送る。
            minutes = idx.hour * 60 + idx.minute
            before_open = (minutes > 0) & (minutes < 9 * 60 + 30)
            after_close = ~before_open
            base = pd.DatetimeIndex(idx.tz_localize(None).normalize())
            reaction = base + pd.to_timedelta(after_close.astype(int), unit="D")
            reaction = pd.DatetimeIndex([d + pd.offsets.BDay(0) for d in reaction])
            # BDay(0) を足すと、土日の日付は次の月曜に送られ、平日はそのまま。米国の祝日は考慮しないが、
            # 足の検索側（searchsorted）が「その日以降で最初の足」を使うので、祝日でも翌営業日の足に当たる。
            df = pd.DataFrame({"reaction": reaction, "surprise": raw["Surprise(%)"].to_numpy()}).sort_values("reaction")
            df = df.drop_duplicates("reaction").reset_index(drop=True)
            # 同じ反応日が重複して返ることがあるので、最初の1件だけ残す。
            df.to_pickle(path)
            out[s] = df
        except Exception:
            failed.append(s)
    return out, failed
