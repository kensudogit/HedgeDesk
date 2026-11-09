"""ニュース分析の答え合わせ: AI の採点が、その後の値動きと合っていたかを記録して集計する。

AI が付けたセンチメントと重要度を、見出しを初めて分析済みとして見た時点で一度だけ記録し、
数日後に「公開前の終値 → N本後の終値」の値動きと突き合わせる。
"""
#
# 【役割】state/news_track.jsonl に AI の判断を1見出し1行で記録し（後から書き換えない）、
#   1本後・3本後の値動きと比べた集計を返す。集計は news_digest.json の evaluation に入り、画面に出る。
# 【位置付け】news.build_digest から呼ばれる。売買判断・審査・発注には使わない（参考情報の検証だけ）。
# 【先読みを避ける工夫】
#   - 記録するのは AI の判断（影響度・センチメント）と、価格を含まない「事前スコア」だけ。
#     画面の重要度は公開後の値動き（価格の確認）を含むので、そのまま答え合わせに使うと自分の答えを見て採点することになる。
#   - 値動きは、公開前の最後の終値を基準に、決まった本数（1本・3本）後の終値で測る。最新の終値ではない。
# 【主な依存先】news（_close_time、parse_time、保存先の C.STATE）。日足は news.build_digest の bars_loader を共有する。
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from . import config as C

HORIZONS = (1, 3)          # 何本後の終値で答え合わせするか
DIRECTION_MIN = 0.2        # |センチメント| がこれ未満の見出しは「方向なし」として一致率の対象から外す
MIN_SAMPLES = 30           # これ未満の件数では「判定に足りない」と表示する
KEEP_DAYS = 120            # 集計に使う期間
SIGMA_BARS = 20


def _track_path():
    """記録の保存先（C.STATE を毎回読むのは、テストで一時ディレクトリに差し替えるため）。"""
    return C.STATE / "news_track.jsonl"


def pre_score(analysis: dict, relevance: int) -> float:
    """価格を含まない事前スコア（0〜75）。影響度×8 + |センチメント|×15 + 関連度（0〜20）。

    AI の生の影響度を使う（解説記事の上限は掛けない）。上限を掛けた効果は kind 別の集計で別に確かめる。
    """
    return round(analysis["impact"] * 8 + abs(analysis["sentiment_score"]) * 15 + relevance, 1)


def _load() -> dict[str, dict]:
    path = _track_path()
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        out.setdefault(row.get("id"), row)
    return out


def record(rows: list[dict], now: datetime) -> int:
    """分析済みでまだ記録していない見出しを追記する。戻り値は追記した件数。

    rows は news.build_digest の行（analysis / importance / price を持つ）。価格の銘柄は行の price.symbol を使う。
    """
    known = _load()
    new = []
    for r in rows:
        a, imp = r.get("analysis"), r.get("importance")
        if not a or not imp or r["id"] in known or not r.get("published_utc"):
            continue
        new.append({
            "id": r["id"], "recorded_utc": now.isoformat(timespec="seconds"), "published_utc": r["published_utc"],
            "price_symbol": (r.get("price") or {}).get("symbol"), "proxy": bool((r.get("price") or {}).get("proxy")),
            "sentiment": a["sentiment"], "sentiment_score": a["sentiment_score"], "impact": a["impact"],
            "category": a.get("category"), "model": a.get("model"), "kind": a.get("kind"),
            "pre_score": pre_score(a, imp["components"]["relevance"]),
        })
    if new:
        _track_path().parent.mkdir(parents=True, exist_ok=True)
        with open(_track_path(), "a", encoding="utf-8") as f:
            f.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in new)
    return len(new)


def outcome(df, symbol: str, published: datetime, horizon: int, now: datetime) -> dict | None:
    """公開前の最後の終値から horizon 本後の終値までの騰落率と、普段の何σか。足が足りなければ None。"""
    from .data import drop_incomplete
    from .news import _close_time

    if df is None or len(df) == 0:
        return None
    df = drop_incomplete(df, symbol, now)
    times = [_close_time(d.date(), symbol) for d in df.index]
    before = [i for i, t in enumerate(times) if t <= published]
    if not before:
        return None
    b = before[-1]
    if b + horizon >= len(df):
        return None
    closes = df["close"]
    ret = float(closes.iloc[b + horizon] / closes.iloc[b] - 1)
    rets = closes.iloc[max(0, b - SIGMA_BARS):b + 1].pct_change().dropna()
    sigma = float(rets.std()) if len(rets) >= 5 else float("nan")
    z = ret / (sigma * math.sqrt(horizon)) if math.isfinite(sigma) and sigma > 0 else None
    return {"ret": ret, "z": z}


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """順位相関。件数が少ないか、どちらかが一定なら None。"""
    if len(xs) < 5:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return round(sxy / (sx * sy), 3) if sx and sy else None


def evaluate(now: datetime, bars_loader) -> dict:
    """記録と値動きを突き合わせて集計する。

    Returns:
        horizons: 本数ごとに n（答え合わせできた件数）、direction（方向一致率と対象件数）、
        by_score（事前スコアの上位半分と下位半分の |z| の平均）、rank_corr（事前スコアと |z| の順位相関）、enough（件数が足りるか）。
        pending: まだ足が揃っていない件数。値はすべて計算値で、件数が少ない間は偶然の影響が大きい。
    """
    from .news import parse_time

    cutoff = now - timedelta(days=KEEP_DAYS)
    rows = [r for r in _load().values() if (parse_time(r.get("published_utc")) or now) >= cutoff and r.get("price_symbol")]
    cache: dict[str, object] = {}
    result = {"recorded": len(rows), "horizons": {}, "min_samples": MIN_SAMPLES, "direction_min": DIRECTION_MIN}
    pending = 0
    for h in HORIZONS:
        pairs = []
        for r in rows:
            sym = r["price_symbol"]
            if sym not in cache:
                try:
                    cache[sym] = bars_loader(sym)
                except Exception:
                    cache[sym] = None
            o = outcome(cache[sym], sym, parse_time(r["published_utc"]), h, now)
            if o is None:
                if h == HORIZONS[0]:
                    pending += 1
                continue
            pairs.append((r, o))
        directional = [(r, o) for r, o in pairs if abs(r["sentiment_score"]) >= DIRECTION_MIN and o["ret"] != 0]
        hits = sum(1 for r, o in directional if (r["sentiment_score"] > 0) == (o["ret"] > 0))
        # kind 別の方向一致率（news-v1 の記録は kind がないので unknown に入る）。解説記事を区別する効果を確かめるため。
        by_kind = {}
        for k in ("new", "recap", "unknown"):
            sub = [(r, o) for r, o in directional if (r.get("kind") or "unknown") == k]
            if sub:
                k_hits = sum(1 for r, o in sub if (r["sentiment_score"] > 0) == (o["ret"] > 0))
                by_kind[k] = {"n": len(sub), "hit_rate": round(k_hits / len(sub), 3)}
        scored = [(r["pre_score"], abs(o["z"])) for r, o in pairs if o["z"] is not None]
        scored.sort(key=lambda t: t[0])
        half = len(scored) // 2
        low, high = scored[:half], scored[len(scored) - half:] if half else []
        mean = (lambda v: round(sum(x for _, x in v) / len(v), 2) if v else None)
        result["horizons"][str(h)] = {
            "n": len(pairs),
            "direction": {"n": len(directional), "hit_rate": round(hits / len(directional), 3) if directional else None, "by_kind": by_kind},
            "by_score": {"high_abs_z": mean(high), "low_abs_z": mean(low), "n_each": half},
            "rank_corr": _spearman([s for s, _ in scored], [z for _, z in scored]),
            "enough": len(directional) >= MIN_SAMPLES,
        }
    result["pending"] = pending
    return result
