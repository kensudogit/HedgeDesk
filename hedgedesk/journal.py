"""Trade Journal: アイデア、拒否、ハンドオフ、トレードをすべて追記で残す。"""
#
# 【役割】全エージェントの行動記録を JSON Lines（1行1レコード）で journal/trade_journal.jsonl に追記する。
# 【位置付け】risk / trader / analyst / research / desk / scheduler などが書き込み、
#   brief（24時間の拒否・取引・エラー）、ui（最近の記録）、trader（連敗数）、desk（週次レビュー）が読む。
# 【主な依存先】config.JOURNAL（テストでは monkeypatch で一時ファイルに差し替えられる）。
# 【設計方針】過去の記録は消さない・書き換えない。後から判断の経緯を検証できるようにするため。
import json
from datetime import datetime, timezone

from .config import JOURNAL


def log(kind: str, agent: str, **fields) -> dict:
    """1行のJSONを追記する。消さない、書き換えない。"""
    # 引数:
    #   kind: 記録の種類（"veto"、"order"、"fill_closed"、"job_error" など）。読む側はこの値で絞り込む。
    #   agent: 記録した担当（"Risk Officer"、"Trader" など）。
    #   **fields: 任意の追加項目。JSON にできない値（Timestamp など）は default=str で文字列にする。
    # 戻り値: 書き込んだレコード（ts、kind、agent と追加項目）。
    # 副作用: journal/ ディレクトリがなければ作り、ファイルの末尾に1行追記する。
    # ts は秒精度の UTC の ISO 形式。文字列のまま比較しても時刻順になる（read の since_iso 比較がこれに依存）。
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "agent": agent, **fields}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return rec


def handoff(agent: str, to: str, ticker: str, idea: str, evidence: str, next_step: str) -> dict:
    """エージェント間の受け渡しは必ずこの4項目で記録する。"""
    # 引数: agent は渡す側、to は受け取る側。TICKER / IDEA / EVIDENCE / NEXT_STEP の4項目は DESK_RULES の書式。
    # 戻り値: log() が書き込んだレコード。
    return log("handoff", agent, to=to, TICKER=ticker, IDEA=idea, EVIDENCE=evidence, NEXT_STEP=next_step)


def read(since_iso: str | None = None) -> list[dict]:
    """ジャーナルを先頭から読み、レコードの一覧を返す。

    Args:
        since_iso: 指定すると、ts がこの ISO 時刻以上のレコードだけを返す。
            ts と同じ形式（UTC、+00:00 付き）で渡す前提の文字列比較である。

    Returns:
        書き込み順のレコードのリスト。ファイルがなければ空リスト。

    Raises:
        json.JSONDecodeError: 壊れた行があった場合（黙って読み飛ばさず、異常に気づけるようにしている）。
    """
    if not JOURNAL.exists():
        return []
    rows = []
    with open(JOURNAL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if since_iso is None or rec["ts"] >= since_iso:
                rows.append(rec)
    return rows
