"""高インパクトイベント（FOMC、CPIなど）。日付が不明なら新規ローンチを止める。

- FOMC: federalreserve.gov の公式ページから取得
- CPIなど: BLSが自動取得を拒否するので、出典付きで state/events_manual.json に記録する
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

import requests

from . import config as C

FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
UA = {"User-Agent": "Mozilla/5.0 (HedgeDesk research client)"}
MONTHS = {m: i + 1 for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"])}
MANUAL = C.STATE / "events_manual.json"


def parse_fomc(html: str) -> list[date]:
    """FRBの日程ページから、FOMC各回の最終日（声明の出る日）を取り出す。"""
    out = []
    panels = re.split(r"(\d{4}) FOMC Meetings", html)
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
    r = requests.get(FED_URL, headers=UA, timeout=20)
    r.raise_for_status()
    return parse_fomc(r.text)


def load_manual() -> list[dict]:
    if not MANUAL.exists():
        return []
    return json.loads(MANUAL.read_text(encoding="utf-8"))


def upcoming(now: datetime | None = None, days: int = 14) -> dict:
    """今後のイベントと、取得できなかった種類を返す。"""
    today = (now or datetime.now(timezone.utc)).date()
    horizon = today + timedelta(days=days)
    events, unknown = [], []
    try:
        fomc = fetch_fomc()
        if not any(d >= today for d in fomc):
            unknown.append("FOMC")
        events += [{"date": d.isoformat(), "name": "FOMC声明", "source": FED_URL} for d in fomc if today <= d <= horizon]
    except Exception as ex:
        unknown.append(f"FOMC（取得失敗: {type(ex).__name__}）")
    manual = load_manual()
    if not any(e.get("kind") == "CPI" and date.fromisoformat(e["date"]) >= today for e in manual):
        unknown.append("CPI")
    events += [e for e in manual if today <= date.fromisoformat(e["date"]) <= horizon]
    return {"events": sorted(events, key=lambda e: e["date"]), "unknown": unknown}


def launch_gate(now: datetime | None = None) -> tuple[bool, list[str]]:
    """新しい戦略を今ローンチしてよいか。不明なものがあれば止める。"""
    info = upcoming(now, days=C.EVENT_BLOCK_DAYS)
    reasons = [f"{e['date']} {e['name']} の直前" for e in info["events"]]
    reasons += [f"{u} の日程が未確認" for u in info["unknown"]]
    return (not reasons), reasons
