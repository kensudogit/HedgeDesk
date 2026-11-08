"""Scout: ニュースと開示情報の見出しを集める。ここでは判断せず、集めるだけ。

取得した文面は外部のデータであり、指示として扱わない。
X（旧Twitter）はオーナーの判断で未接続。
"""
#
# 【役割】FRB のプレスリリースと、指数 ETF・暗号資産・保有銘柄の Yahoo Finance ニュースの RSS を集め、
#   state/scout/<UTC日付>.jsonl に追記する。
# 【位置付け】desk._tick から30分ごとに呼ばれる。集めた見出しは画面（ui）に「外部データ」として表示されるだけで、
#   売買判断・ブリーフ本文には使わない（見出しに紛れた指示文で動作が変わらないようにするため）。
# 【主な依存先】requests、xml.etree（RSS の解析）、config、trader._load_positions（保有銘柄の一覧）。
from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from . import config as C
from .trader import _load_positions

UA = {"User-Agent": "Mozilla/5.0 (HedgeDesk research client)"}
FEEDS_FIXED = {
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
}
# 銘柄ごとの Yahoo Finance ニュース RSS。{sym} に Yahoo 表記の銘柄（BTC-USD など）を入れる。
YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
# 収集結果の保存先。1日1ファイル（UTC 日付）。
OUT = C.STATE / "scout"


def _plain(text: str) -> str:
    """description の HTML タグを除き、文字参照を戻し、空白を詰める（表示と AI への入力用。HTML としては使わない）。"""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def parse_rss(xml_bytes: bytes, source: str, symbol: str | None = None) -> list[dict]:
    """RSS 2.0 の XML から item ごとの見出しを取り出す。

    Args:
        xml_bytes: 取得した RSS（バイト列のまま。文字コードは XML 宣言に従って解釈させる）。
        source: 出所の表示名。symbol: 銘柄ごとのフィードなら銘柄、共通フィードなら None。

    Returns:
        source / symbol / title / link / published / summary の dict のリスト。title は300文字、summary は500文字で切る
        （画面の表示崩れと容量対策）。summary は RSS の description からタグを除いた本文で、ニュース分析（news.py）が AI に渡す。

    Raises:
        xml.etree.ElementTree.ParseError: XML として壊れている場合（collect でフィード単位の失敗として扱う）。
    """
    items = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        items.append({
            "source": source, "symbol": symbol, "title": (it.findtext("title") or "").strip()[:300],
            "link": (it.findtext("link") or "").strip(), "published": (it.findtext("pubDate") or "").strip(),
            "summary": _plain(it.findtext("description") or "")[:500],
        })
    return items


def collect(now: datetime | None = None, session=None) -> dict:
    """全フィードを取得し、まだ記録していないリンクの見出しだけを追記する。

    Args:
        now: 収集時刻（ファイル名の日付と collected_utc に使う）。
        session: get() を持つ HTTP クライアント。テストで偽物を渡すため。省略時は requests モジュール。

    Returns:
        {"new": 追記した件数, "failures": 失敗したフィードの説明, "path": 保存先}。

    副作用: state/scout/ に追記する。フィードごとの失敗は例外にせず failures に残して次のフィードへ進む
        （1つのサイトが落ちていても他の収集を止めないため）。
    """
    now = now or datetime.now(timezone.utc)
    http = session or requests
    held = sorted({r["symbol"] for r in _load_positions().values()})
    symbols = sorted(set(C.INDEX_ETFS + C.CRYPTO + held))
    feeds = [(name, url, None) for name, url in FEEDS_FIXED.items()] + [("Yahoo Finance", YAHOO.format(sym=s), s) for s in symbols]
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{now.strftime('%Y-%m-%d')}.jsonl"
    seen = set()
    # 同じ日のファイルに記録済みのリンクを読み込み、重複を除く（30分ごとに同じ見出しが何度も来るため）。
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            seen.add(json.loads(line).get("link"))
    new, failures = 0, []
    with open(path, "a", encoding="utf-8") as f:
        for name, url, sym in feeds:
            try:
                r = http.get(url, headers=UA, timeout=20)
                r.raise_for_status()
                # 文字コードの宣言はXML自身に任せるので、バイト列のまま渡す
                for item in parse_rss(r.content, name, sym):
                    if item["link"] and item["link"] not in seen:
                        seen.add(item["link"])
                        item["collected_utc"] = now.isoformat(timespec="seconds")
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
                        new += 1
            except Exception as ex:
                failures.append(f"{name}{':' + sym if sym else ''} ({type(ex).__name__})")
    return {"new": new, "failures": failures, "path": str(path)}


def recent(limit: int = 60, now: datetime | None = None) -> list[dict]:
    """直近2日分のファイルから、新しい順ではなく記録順で末尾 limit 件を返す。

    Args:
        limit: 返す最大件数。
        now: 互換性のための引数（現時点の実装では使っていない）。

    Returns:
        見出しの dict のリスト。保存先がなければ空リスト。
    """
    # 2日分を読むのは、UTC の日付が変わった直後でも直近の見出しが空にならないようにするため。
    now = now or datetime.now(timezone.utc)
    files = sorted(OUT.glob("*.jsonl"))[-2:] if OUT.exists() else []
    rows = []
    for p in files:
        rows += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[-limit:]
