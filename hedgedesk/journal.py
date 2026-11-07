"""Trade Journal: アイデア、拒否、ハンドオフ、トレードをすべて追記で残す。"""
import json
from datetime import datetime, timezone

from .config import JOURNAL


def log(kind: str, agent: str, **fields) -> dict:
    """1行のJSONを追記する。消さない、書き換えない。"""
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "agent": agent, **fields}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return rec


def handoff(agent: str, to: str, ticker: str, idea: str, evidence: str, next_step: str) -> dict:
    """エージェント間の受け渡しは必ずこの4項目で記録する。"""
    return log("handoff", agent, to=to, TICKER=ticker, IDEA=idea, EVIDENCE=evidence, NEXT_STEP=next_step)


def read(since_iso: str | None = None) -> list[dict]:
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
