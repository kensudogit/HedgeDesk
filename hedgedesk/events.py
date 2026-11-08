"""高インパクトイベント（FOMC、CPIなど）。日付が不明なら新規ローンチを止める。

- FOMC: federalreserve.gov の公式ページから取得
- CPIなど: BLSが自動取得を拒否するので、出典付きで state/events_manual.json に記録する
"""
#
# 【役割】FOMC・CPI などの高インパクトイベントの日程を集め、新しい戦略を今ローンチしてよいかを判定する。
# 【位置付け】risk.review（未ローンチのチャンピオンの審査）と desk.brief_data（ブリーフ・画面の表示）が使う。
# 【主な依存先】requests（FRB のページ取得）、config（state/ の場所と判定日数）。
# 【state/events_manual.json の形式】[{"date": "YYYY-MM-DD", "kind": "CPI", "name": "...", "source": "URL",
#   "official_confirmed": true/false}, ...]。kind="CPI" の将来日付がないと「CPI 未確認」としてローンチを止める。
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

import requests

from . import config as C

# FRB 公式の FOMC 日程ページ。UA は既定の python-requests だと拒否されることがあるため、ブラウザ風の名前に研究用の識別を付ける。
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
UA = {"User-Agent": "Mozilla/5.0 (HedgeDesk research client)"}
# 英語の月名 → 月番号。FRB のページは2か月にまたがる回を "Apr/May" と書くので、parse_fomc は "/" の後ろの月を使い、
# MONTHS にない表記（省略形など）の回は読み飛ばす。
MONTHS = {m: i + 1 for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"])}
MANUAL = C.STATE / "events_manual.json"


def parse_fomc(html: str) -> list[date]:
    """FRBの日程ページから、FOMC各回の最終日（声明の出る日）を取り出す。"""
    # 引数: html は日程ページの HTML 文字列。
    # 戻り値: 重複を除いて昇順に並べた日付のリスト。
    # 方法: "2026 FOMC Meetings" の見出しで年ごとに分割し、各回の月（<strong> 内）と日付（"27-28" など）を正規表現で拾う。
    #   2日間の会合は最終日に声明が出るので、月も日も "/" や "-" の後ろ側（最後の値）を使う。
    #   日付の後ろの "*"（記者会見付きなどの注記）は正規表現 [\d\-]+ に含まれないので自然に外れる。
    # 注意: ページの構造が変わると空リストになる。その場合は upcoming() が「FOMC 未確認」として扱い、ローンチを止める。
    out = []
    panels = re.split(r"(\d{4}) FOMC Meetings", html)
    # キャプチャ付きの split なので [前置き, 年, 本文, 年, 本文, ...] の並びになる。奇数番目が年、その次が本文。
    for k in range(1, len(panels) - 1, 2):
        year = int(panels[k])
        body = panels[k + 1]
        pairs = re.findall(r'fomc-meeting__month[^>]*>\s*<strong>\s*([A-Za-z/]+)\s*</strong>.*?fomc-meeting__date[^>]*>\s*([\d\-]+)', body, flags=re.S)
        for month, days in pairs:
            last_month = month.split("/")[-1]
            last_day = int(days.split("-")[-1])
            if last_month in MONTHS:
                out.append(date(year, MONTHS[last_month], last_day))
    return sorted(set(out))


def fetch_fomc() -> list[date]:
    """FRB の日程ページを取得して FOMC の日付を返す。

    Raises:
        requests.RequestException: 通信エラーや HTTP エラー（呼び出し元の upcoming で捕まえて「取得失敗」とする）。
    """
    r = requests.get(FED_URL, headers=UA, timeout=20)
    r.raise_for_status()
    return parse_fomc(r.text)


def load_manual() -> list[dict]:
    """state/events_manual.json の手動登録イベントを読む。ファイルがなければ空リスト。"""
    if not MANUAL.exists():
        return []
    return json.loads(MANUAL.read_text(encoding="utf-8"))


def upcoming(now: datetime | None = None, days: int = 14) -> dict:
    """今後のイベントと、取得できなかった種類を返す。"""
    # 引数: now は基準時刻（省略時は現在の UTC）。days は何日先までを「今後」とするか。
    # 戻り値: {"events": 日付順のイベント, "unknown": 日程を確認できなかった種類の説明}。
    # 例外は外に出さず、取得失敗は unknown に入れる（判断できないことを、止める理由として扱うため）。
    today = (now or datetime.now(timezone.utc)).date()
    horizon = today + timedelta(days=days)
    events, unknown = [], []
    try:
        fomc = fetch_fomc()
        if not any(d >= today for d in fomc):
            # ページは取れたが今日以降の回が1つもない＝翌年の日程が未掲載か解析失敗。どちらも「不明」とする。
            unknown.append("FOMC")
        events += [{"date": d.isoformat(), "name": "FOMC声明", "source": FED_URL} for d in fomc if today <= d <= horizon]
    except Exception as ex:
        # どんな失敗でもブリーフと画面に理由を出す。例外の種類名だけを残し、詳細（URL やレスポンス本文）は出さない。
        unknown.append(f"FOMC（取得失敗: {type(ex).__name__}）")
    manual = load_manual()
    if not any(e.get("kind") == "CPI" and date.fromisoformat(e["date"]) >= today for e in manual):
        unknown.append("CPI")
    events += [e for e in manual if today <= date.fromisoformat(e["date"]) <= horizon]
    return {"events": sorted(events, key=lambda e: e["date"]), "unknown": unknown}


def launch_gate(now: datetime | None = None) -> tuple[bool, list[str]]:
    """新しい戦略を今ローンチしてよいか。不明なものがあれば止める。"""
    # 引数: now は判定時刻。EVENT_BLOCK_DAYS（3暦日）以内のイベントと、日程不明の種類が止める理由になる。
    # 戻り値: (ローンチしてよいか, 止める理由のリスト)。理由が1つもなければ True。
    info = upcoming(now, days=C.EVENT_BLOCK_DAYS)
    reasons = [f"{e['date']} {e['name']} の直前" for e in info["events"]]
    reasons += [f"{u} の日程が未確認" for u in info["unknown"]]
    return (not reasons), reasons
