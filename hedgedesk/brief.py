"""Chief of Staff: モーニングブリーフと週次メモをテンプレートで作る。

AIを使わないので、APIキーも費用も不要。書くのは brief_data にある値だけで、数値を作らない。
ニュースの見出しは外部データなので、ブリーフには載せない。
"""
#
# 【位置付け】jobs コンテナが毎朝 write_brief、日曜に write_weekly を呼ぶ。材料は desk.brief_data / desk.weekly_review が作る。
#   手動では `python -m hedgedesk brief [--stdout]` で作れる。出力は briefs/<JST日付>.md と reports/weekly_note_<日付>.md。
# 【主な依存先】config、journal。desk は関数内で import する（desk → brief の循環 import を避けるため）。
# 【設計方針】render は材料の dict だけから文字列を作る純粋関数で、ネットワークもファイルも触らない（テストしやすくするため）。
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config as C
from . import journal

JST = ZoneInfo("Asia/Tokyo")
# ブリーフと週次メモの末尾に必ず付ける免責文。
FOOTER = "これは検証データであり投資助言ではありません。"
# セクターの強弱を並べる対象（config.SECTOR_ETFS と同じ並び）。
SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLY", "XLB", "XLRE", "XLC"]
# research.apply_gates の final_check のキーと、ブリーフでの短い表示名の対応。
CHECKS = [
    ("out_of_sample", "OOS"), ("after_costs", "コスト後"), ("late_double_slip", "遅延"),
    ("deflated_sharpe", "デフレート"), ("survived_2020", "2020年"), ("survived_2022", "2022年"),
]
# desk.weekly_review の rung（昇格の段階）を日本語の説明にする対応表。
RUNGS = {
    "no_champion": "基準を通った戦略がなく、取引なし",
    "not_launched": "チャンピオンはいるが未ローンチ",
    "paper_under_2_weeks": "ペーパー2週間未満",
    "insufficient_data": "判定に足るトレードがない",
    "does_not_match_backtest": "ペーパーがバックテストと整合しない（昇格しない）",
    "eligible_for_owner_decision": "10%サイズのライブを検討できる段階。判断と実行はオーナー",
}


def _pct(v) -> str:
    """割合（0.0123）を符号付きの百分率（+1.23%）にする。None は「不明」。"""
    return "不明" if v is None else f"{v * 100:+.2f}%"


def _usd(v) -> str:
    """ドル金額を符号付き・3桁区切り・整数（+$1,234）にする。None は「不明」。"""
    return "不明" if v is None else f"{'-' if v < 0 else '+'}${abs(v):,.0f}"


def _jst(iso: str | None) -> str:
    """ISO 時刻を「月/日 時:分 JST」にする。空や None は「不明」。"""
    if not iso:
        return "不明"
    return datetime.fromisoformat(iso).astimezone(JST).strftime("%m/%d %H:%M JST")


def _row_line(name: str, r: dict | None) -> str:
    """市場の1銘柄分の箇条書きを作る（終値、日付、前日比、21営業日騰落率、200日線との位置）。データがなければ「データなし」。"""
    if not r:
        return f"- {name}: データなし"
    trend = "200日線より上" if r["above_sma200"] else "200日線より下"
    return f"- {name} {r['close']:,.2f}（{r['date']}）、前日比 {_pct(r['ret_1d'])}、21営業日 {_pct(r['ret_21d'])}、{trend}"


def _market(m: dict) -> list[str]:
    """「市場」の節: SPY・QQQ・VIX・BTC、セクターの強い3つと弱い3つ、出来高急増の銘柄（最大8件）。"""
    rows = m.get("rows", {})
    out = [
        _row_line("SPY", rows.get("SPY")),
        _row_line("QQQ", rows.get("QQQ")),
        f"- VIX {m['vix']}（{m['vix_date']}）" if m.get("vix") is not None else "- VIX: データなし",
        _row_line("BTC", rows.get("BTC-USD")),
    ]
    ranked = sorted(((s, rows[s]["ret_21d"]) for s in SECTORS if s in rows and rows[s].get("ret_21d") is not None), key=lambda x: -x[1])
    # 21営業日騰落率の高い順。弱い側は末尾3つを逆順にして、最も弱いものから並べる。
    if ranked:
        top = "、".join(f"{s} {_pct(v)}" for s, v in ranked[:3])
        low = "、".join(f"{s} {_pct(v)}" for s, v in ranked[-3:][::-1])
        out.append(f"- セクター（21営業日）: 強い {top} / 弱い {low}")
    else:
        out.append("- セクター: データなし")
    if m.get("unusual_volume"):
        names = "、".join(f"{u['symbol']} {u['volume_vs_avg20']}倍" for u in m["unusual_volume"][:8])
        out.append(f"- 出来高が20日平均の2倍以上: {names}")
    return out


def _events(ev: dict, today: str) -> list[str]:
    """「高インパクトイベント」の節。今日の予定がなければその旨を書き、日程不明の種類は太字で警告する。

    official_confirmed が False のイベントは「第三者情報で公式未確認」と明記する（出典の確からしさを読み手に伝えるため）。
    """
    items = ev.get("events", [])
    out = [] if any(e["date"] == today for e in items) else ["- 本日: データにある高インパクトイベントはなし"]
    for e in items:
        note = "。第三者情報で公式未確認" if e.get("official_confirmed") is False else ""
        out.append(f"- {e['date']}: {e['name']}（出典: {e.get('source', '不明')}{note}）")
    for u in ev.get("unknown", []):
        out.append(f"- **{u} の日程が未確認。** 確認できるまで新規ローンチを止める。`state/events_manual.json` に出典付きで追記が必要")
    return out


def _strategy(rank: int, r: dict) -> list[str]:
    """「戦略トップ3」の1戦略分。スコア、OOS の成績、ストレス結果、6項目のチェック、失敗し得る理由を書く。

    不合格なら、理由を2行目（見出しとスコアの間）に差し込んで目立たせる。末尾に空行を1つ付ける。
    """
    oos, st = r["out_of_sample"], r["stress_late_double_slip"]
    checks = " ".join(f"{label}{'○' if r['final_check'][key] else '×'}" for key, label in CHECKS)
    lines = [
        f"### {rank}. {r['name']}: {'合格' if r['passed'] else '不合格'}",
        f"- スコア {r['score']['total']}/25、OOSシャープ {oos['sharpe']:.2f}、デフレートシャープ {r['deflated_sharpe']:.2f}。",
        f"- OOS {oos['trades']}トレード、損益 {_usd(oos['net_pnl'])}、最大DD {oos['max_drawdown'] * 100:.1f}%。",
        f"- 1本遅れ＋スリッページ2倍: {_usd(st['net_pnl'])}。",
        f"- チェック: {checks}。",
        f"- WHY THIS COULD FAIL: {' '.join(r['why_this_could_fail'])}",
    ]
    if r["failures"]:
        lines.insert(2, f"- 不合格の理由: {' / '.join(r['failures'])}。")
    return lines + [""]


def _attention(research: dict | None) -> list[str]:
    """人が確認すべきデータの不備と不自然な結果。"""
    if not research:
        return ["- 調査結果がまだありません。"]
    out = []
    data = research.get("data", {})
    for sym, issues in data.get("issues", {}).items():
        out.append(f"- データの不備: {sym}（{'、'.join(issues)}）")
    if data.get("earnings_failed"):
        out.append(f"- 決算データの取得失敗: {len(data['earnings_failed'])}銘柄")
    for r in research.get("ranked", []):
        if r["out_of_sample"]["net_pnl"] < r["stress_late_double_slip"]["net_pnl"]:
            # 遅延＋コスト2倍のほうが通常より儲かるのは不自然（悪条件で良くなるはずがない）。検査の不具合を疑う合図。
            out.append(f"- {r['name']}: 遅延テストが通常より良い。検査の不具合か偶然を確認する")
        if r["out_of_sample"]["sharpe"] > C.SUSPICIOUS_SHARPE:
            out.append(f"- {r['name']}: OOSシャープが異常に高い。不具合を疑う")
    return out or ["- なし"]


def _decision(data: dict) -> str:
    """「判断事項」の節: オーナーに求める判断を1文で書く。チャンピオンがいれば承認・却下・再検証、いなければ再検証の候補。"""
    champ = data.get("champion")
    if champ:
        return f"**承認、却下、再検証のいずれか**: チャンピオン {champ['id']}（スコア {champ['score']}）のペーパー運用について判断してください。"
    ranked = (data.get("research") or {}).get("ranked", [])
    if ranked:
        top = ranked[0]
        return f"チャンピオンなし（合格0本）。**再検証の候補**: {top['name']}（{' / '.join(top['failures']) or '理由なし'}）。基準は変えません。"
    return "チャンピオンなし。調査結果がありません。jobsコンテナの状態を確認してください。"


def render(data: dict, now: datetime | None = None) -> str:
    """ブリーフの材料（desk.brief_data の dict）から Markdown の本文を作る。

    Args:
        data: ブリーフの材料。キーが欠けていても「データなし」「不明」として書ける。
        now: 作成時刻（見出しの日付は JST）。

    Returns:
        Markdown の文字列。scout_headlines_untrusted（外部の見出し）は意図的に使わない。
    """
    now = now or datetime.now(timezone.utc)
    jst = now.astimezone(JST)
    research = data.get("research")
    ranked = (research or {}).get("ranked", [])
    passed = len((research or {}).get("passed_ids", []))
    tick = data.get("tick_health") or {}
    out = [f"# モーニングブリーフ {jst.strftime('%Y-%m-%d')}", "", f"作成 {_jst(now.isoformat())}。テンプレートで自動作成。", ""]
    if not tick.get("healthy"):
        # 最も重要な警告（定期実行の停止、キルスイッチ）は、市場の節より前の冒頭に引用ブロックで出す。
        out += [f"> **5分ごとの処理（tickコンテナ）が止まっている可能性があります。** 最終実行 {_jst(tick.get('last_tick_utc'))}。PCのスリープや再起動、Docker Desktopの停止を確認してください。", ""]
    if data.get("kill_switch", {}).get("active"):
        out += [f"> **キルスイッチ作動中:** {data['kill_switch'].get('reason')}。解除はオーナーのみ。", ""]
    out += ["## 市場", *_market(data.get("market", {})), ""]
    out += ["## 高インパクトイベント", *_events(data.get("events", {}), jst.date().isoformat()), ""]
    as_of = _jst(research.get("as_of_utc")) if research else "不明"
    out += [f"## 戦略トップ3: 合格 {passed}本（全{(research or {}).get('n_trials_total', '不明')}試行、調査 {as_of}）", ""]
    for i, r in enumerate(ranked[:3], 1):
        out += _strategy(i, r)
    if not ranked:
        out += ["調査結果がありません。", ""]
    vetoes = data.get("vetoes_24h", [])
    out += ["## Risk Officerの拒否"]
    out += [f"- {v.get('TICKER')} / {v.get('IDEA')}: {'、'.join(v.get('EVIDENCE') or [])}" for v in vetoes] or ["- なし"]
    champ = data.get("champion")
    trades = data.get("trades_24h", [])
    out += [
        "", "## ボットの状況",
        f"- 5分ごとの処理: {'稼働中' if tick.get('healthy') else '停止の可能性'}（最終実行 {_jst(tick.get('last_tick_utc'))}）",
        f"- チャンピオン: {champ['id'] if champ else 'なし'}",
        f"- Alpacaペーパー口座: {'接続あり' if data.get('alpaca_connected') else '未接続'}",
        f"- キルスイッチ: {'作動中' if data.get('kill_switch', {}).get('active') else '未作動'}",
        f"- 夜間のペーパートレード: {len(trades)}件" if trades else "- 夜間のペーパートレード: なし",
    ]
    errors = data.get("errors_24h", [])
    if errors:
        out += [f"- **処理エラー（24時間）: {len(errors)}件。** `journal/trade_journal.jsonl` を確認してください"]
    out += ["", "## 確認事項", *_attention(research), "", "## 判断事項", _decision(data), "", FOOTER, ""]
    return "\n".join(out)


def write_brief(now: datetime | None = None) -> str:
    """材料を集めてブリーフをファイルに保存する。"""
    from .desk import brief_data

    now = now or datetime.now(timezone.utc)
    text = render(brief_data(now), now)
    C.BRIEFS.mkdir(parents=True, exist_ok=True)
    path = C.BRIEFS / f"{now.astimezone(JST).strftime('%Y-%m-%d')}.md"
    path.write_text(text, encoding="utf-8")
    journal.log("brief", "Chief of Staff", path=path.name)
    return str(path)


def write_weekly(now: datetime | None = None) -> str:
    """週次レビューの結果を短いメモにする。ライブ移行の実行や推奨はしない。"""
    from .desk import weekly_review

    now = now or datetime.now(timezone.utc)
    r = weekly_review(now)
    lines = [
        f"# 週次レビュー {now.astimezone(JST).strftime('%Y-%m-%d')}", "",
        f"- 昇格の階段: {RUNGS.get(r['rung'], r['rung'])}",
        f"- ペーパートレード数: {r['paper_trades']}",
    ]
    if "matches_backtest" in r:
        lines += [
            f"- ペーパーの平均R: {r['paper_expectancy_r']}（90%信頼区間 {r['paper_ci90']}）",
            f"- バックテストの平均R: {r['backtest_expectancy_r']}",
            f"- 整合: {'あり' if r['matches_backtest'] else 'なし'}",
        ]
    lines += ["", FOOTER, ""]
    C.REPORTS.mkdir(parents=True, exist_ok=True)
    path = C.REPORTS / f"weekly_note_{now.strftime('%Y%m%d')}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
