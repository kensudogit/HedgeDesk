"""サービス画面: Deskの状況を表示する読み取り専用のローカルサーバー。

- 127.0.0.1 だけで待ち受ける。PCの外からは見えない。
- GETのみ。発注、キルスイッチ解除、設定変更の機能は持たない。
- ニュースの見出しなど外部の文字列（AI による訳と理由を含む）は、画面側で textContent としてのみ表示する。
"""
#
# 【位置付け】ui コンテナで `python -m hedgedesk ui` として常駐する。state/ と journal/ と briefs/ を読み取り専用で読み、
#   書き込むのは refresh.request による「今すぐ更新」の依頼ファイル（requests/）だけ。
# 【エンドポイント】GET / （画面の HTML）、GET /api/status （画面に出す JSON）、POST /api/refresh （調査のやり直し依頼）。
# 【主な依存先】標準ライブラリの http.server、config、journal、refresh、risk、scout、trader（キーの有無と保有台帳の読み取りのみ）。
#   ニュース分析は news.py が書いた state/news_digest.json を読むだけ（AI の呼び出しは tick コンテナで行う）。
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as C
from . import journal, refresh, risk, scout, trader

# 既定はこのPCのみ。コンテナの中でだけ 0.0.0.0 にし、公開範囲は Compose 側で 127.0.0.1 に絞る。
# この画面に認証はないので、インターネットへ直接公開しないこと。
HOST = os.environ.get("HEDGEDESK_UI_HOST", "127.0.0.1")
# 待ち受けポート（固定）。ホスト側の公開ポートは Compose の HEDGEDESK_UI_PORT で変えられる。
# TICK_STALE_SECONDS: 最終 tick からこの秒数（10分）を超えたら、画面で「停止の可能性」と表示する。
PORT = 8765
TICK_STALE_SECONDS = 600
# 画面のロゴとファビコン（/logo.png で返す）。イメージには Dockerfile で public/PC.png だけをコピーする。
LOGO = C.ROOT / "public" / "PC.png"

# 画面の「スケジュール」表に出す説明用の一覧。実際の時刻は desk._tick と scheduler の定数で決まる（ここは表示だけ）。
SCHEDULE = [
    {"job": "Scout（ニュース収集）", "when": "30分ごと", "runner": "tickコンテナ"},
    {"job": "ニュース分析（翻訳・センチメント・重要度）", "when": "収集の直後", "runner": "tickコンテナ"},
    {"job": "キルスイッチ監視、約定確認", "when": "5分ごと", "runner": "tickコンテナ"},
    {"job": "株のシグナル判定と審査", "when": "米国の引け後（NY 16:15）", "runner": "tickコンテナ"},
    {"job": "株の再審査、発注", "when": "NY 08:45、09:15", "runner": "tickコンテナ"},
    {"job": "暗号資産のシグナル判定と発注", "when": "09:05 JST", "runner": "tickコンテナ"},
    {"job": "夜間リサーチ（Strategist）", "when": "毎日 21:00 JST", "runner": "jobsコンテナ"},
    {"job": "モーニングブリーフ", "when": "毎日 07:15 JST", "runner": "jobsコンテナ"},
    {"job": "週次レビュー", "when": "日曜 10:00 JST", "runner": "jobsコンテナ"},
]


def _read_json(path, default=None):
    """JSON ファイルを読む。存在しない・読めない・壊れている場合は default（画面は表示を続ける）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _last_tick() -> dict | None:
    """tick.log の最終行（最後の tick の記録）を返す。ログがなければ None。"""
    path = C.STATE / "tick.log"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return json.loads(lines[-1]) if lines else None


def _strategy_row(r: dict) -> dict:
    """調査レポートの1戦略を、画面の「戦略の順位」表の1行分に縮める。

    stress_better は「遅延ストレスのほうが通常より良い」不自然な結果の印で、画面で「要確認」と表示する。
    """
    oos, stress = r["out_of_sample"], r["stress_late_double_slip"]
    return {
        "id": r["id"], "name": r["name"], "score": r["score"]["total"], "oos_sharpe": oos["sharpe"], "dsr": r["deflated_sharpe"],
        "trades": oos["trades"], "max_dd": oos["max_drawdown"], "net_pnl": oos["net_pnl"], "stress_pnl": stress["net_pnl"],
        "stress_better": stress["net_pnl"] > oos["net_pnl"], "passed": r["passed"], "failures": r["failures"],
    }


def _latest_brief() -> dict | None:
    """briefs/ の最新のブリーフ（ファイル名の日付順で最後）の名前と本文。なければ None。"""
    files = sorted(C.BRIEFS.glob("*.md")) if C.BRIEFS.exists() else []
    if not files:
        return None
    return {"name": files[-1].name, "text": files[-1].read_text(encoding="utf-8")}


# 画面に出すニュース分析の件数（news_digest.json には最大100件ある）。
NEWS_LIMIT = 30


def _news() -> dict | None:
    """news.py が作った一覧を読み、画面用に件数を絞る。まだ作られていなければ None。

    ここでは news.py を import しない（画面のコンテナで AI のクライアントや pandas を読み込まないため）。
    """
    d = _read_json(C.STATE / "news_digest.json")
    if not d:
        return None
    return {**d, "items": (d.get("items") or [])[:NEWS_LIMIT]}


def status(now: datetime | None = None) -> dict:
    """画面に出す内容をまとめる。キーの値は含めない。"""
    # 引数: now は表示上の現在時刻（tick の経過秒数の計算に使う）。
    # 戻り値: 画面の JavaScript がそのまま描画する dict。ジャーナルは最新15件、見出しは最新12件を新しい順に並べる。
    # alpaca_keys はキーがあるかどうかの真偽値だけ。キーの値やブローカーへの問い合わせ結果は含めない。
    now = now or datetime.now(timezone.utc)
    tick = _last_tick()
    age = None
    if tick:
        age = int((now - datetime.fromisoformat(tick["ts"])).total_seconds())
    research = _read_json(C.STATE / "latest_research.json") or {}
    brief = _read_json(C.STATE / "brief_data.json") or {}
    rows = journal.read()
    return {
        "now_utc": now.isoformat(timespec="seconds"),
        "last_tick": tick["ts"] if tick else None,
        "tick_age_seconds": age,
        "tick_healthy": age is not None and age <= TICK_STALE_SECONDS,
        "champion": _read_json(C.STATE / "champion.json"),
        "kill_switch": risk.kill_status(),
        "alpaca_keys": trader.AlpacaPaper().available,
        "open_book": list(trader._load_positions().values()),
        "research_as_of": research.get("as_of_utc"),
        "n_trials": research.get("n_trials_total"),
        "passed_ids": research.get("passed_ids", []),
        "data_issues": (research.get("data") or {}).get("issues", {}),
        "strategies": [_strategy_row(r) for r in research.get("ranked", [])],
        "blocked": research.get("blocked_hypotheses", []),
        "events": (brief.get("events") or {}).get("events", []),
        "events_as_of": brief.get("as_of_utc"),
        "schedule": SCHEDULE,
        "journal": rows[-15:][::-1],
        "headlines": scout.recent(12, now)[::-1],
        "news": _news(),
        "forward": _read_json(C.STATE / "forward.json"),
        "brief": _latest_brief(),
        "refresh": refresh.state(),
    }


# 画面の HTML・CSS・JavaScript（1ファイル完結。外部の CDN を読まない）。
# 文字列の中身はそのままブラウザに送られるので、コメントは文字列の外（ここ）に書いている。
# JavaScript の要点:
#   - 15秒ごとに /api/status を取得して再描画する（load と setInterval）。
#   - 値はすべて textContent か DOM の要素作成で表示し、HTML として解釈させない。ニュースの見出しなど外部の文字列が
#     スクリプトとして動かないようにするため（tests/test_ui.py が innerHTML などの不使用を検査している）。
#   - リンクは http(s) で始まるものだけを a 要素にし、target=_blank には rel="noopener noreferrer" を付ける。
#   - 「今すぐ更新」は独自ヘッダー X-HedgeDesk-Action を付けて POST する（do_POST の CSRF 対策と対になっている）。
#   - 配色はダーク基調（トレーディング端末風のガラス調パネル）。OS がライトモードなら prefers-color-scheme で
#     CSS 変数を明るい配色に切り替える。動きを減らす設定（prefers-reduced-motion）ではアニメーションを止める。
#   - 稼働状況はタイル、重要度とスコアは横棒、センチメントは中央0の横棒で示す。幅は style.width などの
#     プロパティで数値だけを設定する（HTML を組み立てない）。
PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HedgeDesk</title><link rel="icon" type="image/png" href="/logo.png">
<style>
:root{color-scheme:dark;--bg:#070b14;--bg2:#0c1324;--panel:rgba(16,24,42,.74);--panel2:rgba(255,255,255,.028);--ink:#e6ecf5;--mute:#8d99ad;--line:rgba(148,163,184,.13);--line2:rgba(148,163,184,.26);--accent:#5eead4;--accent2:#818cf8;--ok:#4ade80;--okbg:rgba(74,222,128,.12);--bad:#fb7185;--badbg:rgba(251,113,133,.13);--warn:#fbbf24;--warnbg:rgba(251,191,36,.13);--off:#a3adbd;--offbg:rgba(148,163,184,.12);--shadow:0 1px 0 rgba(255,255,255,.05) inset,0 18px 40px -22px rgba(0,0,0,.8);--mono:"Cascadia Mono","JetBrains Mono",ui-monospace,Consolas,monospace}
@media (prefers-color-scheme:light){:root{color-scheme:light;--bg:#eef2f8;--bg2:#f4f7fb;--panel:rgba(255,255,255,.8);--panel2:rgba(15,23,42,.025);--ink:#0f172a;--mute:#566174;--line:rgba(15,23,42,.09);--line2:rgba(15,23,42,.17);--accent:#0d9488;--accent2:#4f46e5;--ok:#15803d;--okbg:rgba(21,128,61,.1);--bad:#be123c;--badbg:rgba(190,18,60,.09);--warn:#a16207;--warnbg:rgba(202,138,4,.14);--off:#525c6c;--offbg:rgba(15,23,42,.06);--shadow:0 1px 2px rgba(15,23,42,.05),0 14px 34px -18px rgba(15,23,42,.22)}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--ink);font:14.5px/1.65 "Segoe UI Variable","Segoe UI","Yu Gothic UI","Hiragino Sans",system-ui,sans-serif;background:radial-gradient(1100px 560px at 8% -12%,color-mix(in srgb,var(--accent2) 20%,transparent),transparent 62%),radial-gradient(900px 520px at 100% -6%,color-mix(in srgb,var(--accent) 13%,transparent),transparent 58%),var(--bg);background-attachment:fixed}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);background-size:46px 46px;-webkit-mask-image:radial-gradient(ellipse 80% 60% at 50% 0%,#000,transparent 75%);mask-image:radial-gradient(ellipse 80% 60% at 50% 0%,#000,transparent 75%)}
.top{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--bg) 72%,transparent);-webkit-backdrop-filter:saturate(1.5) blur(14px);backdrop-filter:saturate(1.5) blur(14px);border-bottom:1px solid var(--line)}
.top .in{max-width:1240px;margin:0 auto;padding:11px 20px;display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px}
.brand{display:flex;align-items:center;gap:10px}
img.logo{width:34px;height:34px;border-radius:10px;object-fit:cover;border:1px solid color-mix(in srgb,var(--accent) 45%,transparent);box-shadow:0 0 22px -6px var(--accent)}
.logo[hidden]{display:none}
.logo{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;background:conic-gradient(from 210deg,var(--accent),var(--accent2),var(--accent));color:#06101f;font:800 12px/1 var(--mono);box-shadow:0 0 26px -6px var(--accent)}
h1{font-size:17px;margin:0;font-weight:700;letter-spacing:.01em}
.pill{font:600 11.5px/1 var(--mono);letter-spacing:.06em;padding:6px 10px;border-radius:999px;border:1px solid color-mix(in srgb,var(--accent) 40%,transparent);color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,transparent)}
.live{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--mute);font-size:12.5px;font-variant-numeric:tabular-nums}
.dot{width:8px;height:8px;border-radius:50%;background:var(--off);flex:none}
.dot[data-s=ok]{color:var(--ok);background:var(--ok);animation:pulse 2.2s infinite}.dot[data-s=bad]{color:var(--bad);background:var(--bad);animation:pulse 1.2s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 transparent}45%{box-shadow:0 0 0 6px color-mix(in srgb,currentColor 28%,transparent)}}
main{max-width:1240px;margin:0 auto;padding:24px 20px 56px}
h2{font-size:15.5px;margin:0;font-weight:680;display:flex;align-items:center;gap:10px}
h2::before{content:"";width:4px;height:16px;border-radius:2px;background:linear-gradient(var(--accent),var(--accent2));flex:none}
.mute{color:var(--mute);font-size:12.5px;font-weight:400}
.hero{--hc:var(--accent);position:relative;overflow:hidden;border-radius:20px;padding:22px 24px 20px;margin-bottom:20px;background:linear-gradient(135deg,color-mix(in srgb,var(--hc) 15%,var(--panel)) 0%,var(--panel) 55%);border:1px solid color-mix(in srgb,var(--hc) 32%,var(--line));box-shadow:var(--shadow);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);animation:rise .5s ease both}
.hero::after{content:"";position:absolute;right:-80px;top:-80px;width:260px;height:260px;border-radius:50%;background:radial-gradient(circle,color-mix(in srgb,var(--hc) 30%,transparent),transparent 70%);pointer-events:none}
.hero[data-s=ok]{--hc:var(--ok)}.hero[data-s=bad]{--hc:var(--bad)}.hero[data-s=warn]{--hc:var(--warn)}
.eyebrow{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;letter-spacing:.08em;color:var(--hc);margin-bottom:6px}
.eyebrow::before{content:"";width:7px;height:7px;border-radius:2px;background:var(--hc);box-shadow:0 0 12px var(--hc)}
.lead{position:relative;font-size:clamp(18px,2.3vw,24px);font-weight:680;line-height:1.45;margin:0 0 18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,138px),1fr));gap:10px}
.tile{--tc:var(--off);position:relative;min-width:0;padding:11px 14px 12px 16px;border-radius:12px;background:var(--panel2);border:1px solid var(--line);transition:border-color .2s,transform .2s}
.tile:hover{border-color:var(--line2);transform:translateY(-1px)}
.tile::before{content:"";position:absolute;left:0;top:12px;bottom:12px;width:3px;border-radius:0 3px 3px 0;background:var(--tc);box-shadow:0 0 10px var(--tc)}
.t-ok{--tc:var(--ok)}.t-bad{--tc:var(--bad)}.t-warn{--tc:var(--warn)}.t-off{--tc:var(--off)}
.tile .k{font-size:12px;color:var(--mute)}.tile .v{font-size:16.5px;font-weight:700;color:var(--tc);margin:1px 0 2px;overflow-wrap:anywhere}.tile .why{font-size:12px;color:var(--mute);overflow-wrap:anywhere}
section{background:var(--panel);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);border:1px solid var(--line);border-radius:18px;padding:18px 20px;margin-bottom:20px;box-shadow:var(--shadow);animation:rise .5s ease both}
@keyframes rise{from{opacity:0;transform:translateY(8px)}}
.head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px 14px;margin-bottom:14px}.head .mute{display:block;margin:2px 0 0 14px}
section>h2{margin-bottom:14px}.act{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr));gap:20px;margin-bottom:20px}.two section{margin:0}
.scroll{overflow:auto;max-height:620px;border:1px solid var(--line);border-radius:12px;background:var(--panel2)}
.scroll:has(thead tr:empty){display:none}
table{border-collapse:separate;border-spacing:0;width:100%}
th{position:sticky;top:0;z-index:1;text-align:left;padding:10px 12px;background:color-mix(in srgb,var(--bg2) 94%,transparent);border-bottom:1px solid var(--line2);font:600 11px/1.4 var(--mono);letter-spacing:.05em;color:var(--mute);white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr{transition:background .15s}tbody tr:hover{background:color-mix(in srgb,var(--accent) 6%,transparent)}tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
#newsai td:nth-child(2),#news td:first-child{white-space:nowrap;font-family:var(--mono);font-size:13px}#strat td:first-child{min-width:190px}
td.empty{text-align:center;color:var(--mute);padding:22px 12px}
.badge{display:inline-flex;align-items:center;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:650;white-space:nowrap;border:1px solid color-mix(in srgb,currentColor 24%,transparent)}
.ok{color:var(--ok);background:var(--okbg)}.bad{color:var(--bad);background:var(--badbg)}.warn{color:var(--warn);background:var(--warnbg)}.off{color:var(--off);background:var(--offbg)}
.meter{height:4px;border-radius:2px;background:var(--offbg);margin-top:7px;overflow:hidden;min-width:64px}
.meter i,.senti i{display:block;height:100%;border-radius:2px;background:var(--mc,var(--accent))}
.senti{position:relative;width:104px;height:4px;border-radius:2px;background:var(--offbg);margin-top:7px}
.senti::after{content:"";position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--line2)}.senti i{position:absolute;top:0}
.m-ok{--mc:var(--ok)}.m-bad{--mc:var(--bad)}.m-warn{--mc:var(--warn)}.m-off{--mc:var(--off)}.m-acc{--mc:linear-gradient(90deg,var(--accent2),var(--accent))}
.fail{font-size:12.5px;color:var(--mute);margin-top:3px}
a{color:inherit;text-decoration-color:color-mix(in srgb,var(--accent) 55%,transparent);text-underline-offset:3px}a:hover{color:var(--accent)}
pre{white-space:pre-wrap;word-break:break-word;margin:0;font:inherit;font-size:14px;padding:14px 16px;border-radius:12px;background:var(--panel2);border:1px solid var(--line)}
p.mute{margin:12px 2px 0}
button{font:inherit;font-size:13px;font-weight:680;padding:7px 16px;border-radius:10px;color:var(--ink);cursor:pointer;border:1px solid color-mix(in srgb,var(--accent) 45%,transparent);background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 22%,transparent),color-mix(in srgb,var(--accent2) 22%,transparent));transition:transform .15s,box-shadow .15s}
button:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 18%,transparent),0 8px 22px -10px var(--accent)}button:disabled{opacity:.5;cursor:default}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
details summary{cursor:pointer;font-weight:680;list-style:none;display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"";width:7px;height:7px;margin:0 2px 0 4px;border-right:2px solid var(--accent);border-bottom:2px solid var(--accent);transform:rotate(-45deg);transition:transform .2s}
details[open] summary::before{transform:rotate(45deg)}details[open] summary{margin-bottom:14px}
.foot{display:flex;gap:10px;align-items:flex-start;color:var(--mute);font-size:12.5px;padding:14px 16px;border-radius:14px;border:1px dashed var(--line2)}
.gbtn{padding:5px 13px;font-size:12px;border-radius:999px;border-color:color-mix(in srgb,var(--accent2) 50%,transparent);background:color-mix(in srgb,var(--accent2) 16%,transparent)}
.guide{--gc:var(--accent2);position:fixed;right:16px;bottom:16px;z-index:30;width:min(392px,calc(100vw - 32px));max-height:min(700px,calc(100vh - 32px));display:flex;flex-direction:column;border-radius:20px;overflow:hidden;background:var(--bg2);-webkit-backdrop-filter:blur(18px) saturate(1.4);backdrop-filter:blur(18px) saturate(1.4);border:1px solid color-mix(in srgb,var(--accent2) 32%,var(--line));box-shadow:0 26px 64px -22px rgba(0,0,0,.65),0 0 0 1px rgba(255,255,255,.03) inset}
.guide[hidden]{display:none}
.gbar{flex:none;display:flex;align-items:center;gap:10px;padding:12px 12px 12px 14px;cursor:grab;touch-action:none;user-select:none;-webkit-user-select:none;border-bottom:1px solid var(--line)}
.gbar:active{cursor:grabbing}.guide[data-fold="1"] .gbar{border-bottom:0}
.grip{color:var(--mute);font-size:17px;line-height:1}
.gtitle{min-width:0}.gtitle b{display:flex;align-items:center;gap:8px;font-size:15.5px;font-weight:750}
.gtitle b::before{content:"";width:4px;height:17px;border-radius:2px;background:linear-gradient(var(--accent2),color-mix(in srgb,var(--accent2) 55%,var(--accent)));flex:none}
.gtitle small{display:block;margin:1px 0 0 12px;font:800 9.5px/1.3 var(--mono);letter-spacing:.13em;color:var(--mute)}
.ghint{margin-left:auto;font-size:11.5px;font-weight:650;color:var(--accent2);white-space:nowrap}
.gfold{flex:none;width:36px;height:36px;padding:0;border-radius:50%;display:grid;place-items:center;font-size:12px;color:var(--accent2);background:color-mix(in srgb,var(--accent2) 12%,transparent);border-color:color-mix(in srgb,var(--accent2) 40%,transparent)}
.gfold span{transition:transform .2s}.guide[data-fold="1"] .gfold span{transform:rotate(180deg)}
.gbody{flex:1 1 auto;min-height:0;overflow:auto;overscroll-behavior:contain;padding:14px 14px 18px}.gbody>*+*{margin-top:12px}
.guide[data-fold="1"] .gbody{display:none}
.gcard{border-radius:14px;padding:13px 15px 14px;background:linear-gradient(135deg,color-mix(in srgb,var(--gc) 15%,var(--panel)),color-mix(in srgb,var(--gc) 5%,var(--panel)));border:1px solid color-mix(in srgb,var(--gc) 32%,var(--line))}
.gcard.w{--gc:var(--warn)}.gcard.r{--gc:var(--bad)}.gcard.t{--gc:var(--accent)}
.gk{font:800 10.5px/1.2 var(--mono);letter-spacing:.12em;color:var(--gc);margin-bottom:7px}
.gcard h3{margin:0 0 6px;font-size:14.5px;font-weight:720;line-height:1.5}
.gcard p{margin:0 0 6px;font-size:12.8px;line-height:1.7}
.gtag{display:inline-block;font:800 9.5px/1 var(--mono);letter-spacing:.1em;padding:5px 8px;border-radius:6px;background:var(--gc);color:#fff;margin-right:8px;vertical-align:2px}
.gcard.w .gtag,.gcard.t .gtag{color:#111827}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}.chips span{font-size:11.3px;font-weight:600;padding:4px 10px;border-radius:999px;border:1px solid color-mix(in srgb,var(--gc) 36%,transparent);background:color-mix(in srgb,var(--bg2) 55%,transparent)}
.gcard ul,.gcard ol{margin:4px 0 0;padding-left:19px;font-size:12.5px;line-height:1.65;color:var(--mute)}.gcard li{margin:4px 0}.gcard li b{color:var(--ink);font-weight:650}
.gcard code{font:600 11.5px/1 var(--mono);padding:1px 5px;border-radius:5px;background:color-mix(in srgb,var(--gc) 14%,transparent);color:var(--ink);overflow-wrap:anywhere}
.gsec{font:800 10.5px/1 var(--mono);letter-spacing:.14em;color:var(--mute);margin:8px 2px 0}
pre.gcode{margin:8px 0 2px;padding:11px 13px;border-radius:11px;background:#0b1020;color:#dbe4ff;font:11.5px/1.65 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;border:1px solid rgba(129,140,248,.28)}
pre.gtopo{margin-top:8px;font-size:11.3px;white-space:pre;overflow-wrap:normal}
.gcard pre.gcode .c,pre.gcode .c{color:#8b9bc7}
@media (max-width:560px){main{padding:16px 16px 40px}.top .in{padding:10px 16px}.live{margin-left:0;width:100%}.hero,section{padding:16px}.head .mute{margin-left:0}.ghint{display:none}.gfold{margin-left:auto}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<header class="top"><div class="in"><div class="brand"><img class="logo" id="logo" src="/logo.png" alt="" width="34" height="34"><span class="logo" id="logotext" aria-hidden="true" hidden>HD</span><h1>HedgeDesk</h1></div><span class="pill">ペーパー取引のみ</span><button class="gbtn" id="guidebtn" type="button" aria-controls="guide" aria-expanded="false">利用手順</button><span class="live"><span class="dot" id="dot"></span><span id="updated">接続中</span></span></div></header>
<main>
<div class="hero" id="hero"><div class="eyebrow">デスクの状態</div><p class="lead" id="lead">読み込み中です</p><div class="tiles" id="stat"></div></div>
<section><div class="head"><div><h2>戦略の順位</h2><span class="mute" id="research"></span></div><span class="act"><span class="mute" id="refreshmsg"></span><button id="refresh" type="button">今すぐ更新</button></span></div><div class="scroll"><table id="strat"></table></div><p class="mute" id="stratnote"></p></section>
<section><div class="head"><div><h2>フォワード記録</h2><span class="mute">記録開始日より後のデータだけで全戦略を追跡。発注はしません</span></div><span class="mute" id="fwdasof"></span></div><div class="scroll"><table id="fwd"></table></div><p class="mute" id="fwdnote"></p></section>
<section><div class="head"><div><h2>ニュース分析</h2><span class="mute">外部データのAI分析。売買判断には使いません</span></div><span class="mute" id="newsasof"></span></div><div class="tiles" id="newsstat"></div><div class="scroll" style="margin-top:14px"><table id="newsai"></table></div><p class="mute" id="newsnote"></p></section>
<div class="two">
<section><h2>今後のイベント</h2><div class="scroll"><table id="events"></table></div><p class="mute" id="eventsnote"></p></section>
<section><h2>スケジュール</h2><div class="scroll"><table id="sched"></table></div></section>
</div>
<section><h2>最近のジャーナル</h2><div class="scroll"><table id="journal"></table></div></section>
<section><details><summary>最近の見出し（原文） <span class="mute">外部データ。Deskの見解ではありません</span></summary><div class="scroll"><table id="news"></table></div></details></section>
<section><details><summary id="briefname">最新のブリーフ</summary><pre id="brief"></pre></details></section>
<p class="foot">この画面からできる操作は「今すぐ更新」（調査のやり直し）だけです。発注や設定変更はできません。検証データであり、投資助言ではありません。</p>
</main>
<aside class="guide" id="guide" role="dialog" aria-modal="false" aria-labelledby="guidetitle" hidden>
<div class="gbar" id="guidebar" tabindex="0" title="ドラッグで移動（矢印キーでも動かせます）"><span class="grip" aria-hidden="true">☰</span><div class="gtitle"><b id="guidetitle">利用手順</b><small>ARCHITECTURE &amp; OPS</small></div><span class="ghint" aria-hidden="true">ドラッグで移動</span><button class="gfold" id="guidefold" type="button" aria-expanded="true" aria-controls="guidebody" title="たたむ"><span aria-hidden="true">▼</span></button></div>
<div class="gbody" id="guidebody">
<div class="gcard"><div class="gk">HEDGEDESK · PAPER ONLY</div><h3>米国株・ETF・暗号資産のスイング戦略デスク</h3><p>戦略をウォークフォワードで検証し、基準を通ったチャンピオン戦略だけを Alpaca のペーパー口座で動かします。ニュースは Claude で訳して採点し、鮮度と値動きで裏を取ります。実資金の発注はできません。</p><div class="chips"><span>Python 3.12 · pandas</span><span>yfinance · 日足</span><span>Docker Compose</span><span>Alpaca Paper API</span><span>Claude opus-5-5</span><span>FRB · Yahoo RSS</span><span>ウォークフォワード · DSR</span><span>TradingView Pine（目視）</span><span>pytest</span></div></div>
<div class="gcard"><p><span class="gtag">ARCHITECTURE</span><b>3つの常駐コンテナ ＋ 手動ツール</b></p><p>この画面（ui）は読み取り専用です。処理は tick と jobs が受け持ち、結果は共有フォルダの state/ などに書きます。キーは .env からコンテナを作るときに渡し、イメージには入りません。</p><ul><li><b>ui :8765</b> — サービス画面。127.0.0.1 だけに公開。書けるのは「今すぐ更新」の依頼だけ</li><li><b>tick</b> — 5分ごと。Scout、ニュース分析、キルスイッチ監視、シグナル、Trader</li><li><b>jobs</b> — 夜間リサーチ 21:00、ブリーフ 07:15、週次レビュー 日曜 10:00（JST）</li><li><b>desk</b>（--profile tools）— 調査や状態確認の手動コマンド</li><li><b>test</b>（--profile test）— pytest 一式</li></ul></div>
<div class="gsec">SERVICE TOPOLOGY</div>
<pre class="gcode gtopo">Browser（このPCだけ）
   │ http://127.0.0.1:8765
   ▼
ui :8765 ── 読み取り専用 ── state/ journal/ briefs/
   │ 今すぐ更新 → requests/
   ▼
jobs ─ Strategist  21:00 夜間リサーチ
     ├ Brief       07:15 モーニングブリーフ
     └ Weekly      日 10:00 週次レビュー
tick ─ Scout       30分ごと FRB・Yahoo RSS
     ├ News        収集直後 → Claude API
     ├ Risk        キルスイッチ・審査
     └ Trader      → Alpaca Paper API</pre>
<div class="gsec">SETUP · はじめ方</div>
<div class="gcard t"><div class="gk">STEP 1 · 起動する</div><ol><li>Docker Desktop を起動し、Settings › General の <b>Start Docker Desktop when you sign in</b> をオンにします（オフだと再起動後に止まったままです）</li><li>HedgeDesk のフォルダで全サービスを起動します<pre class="gcode">docker compose --profile tick up -d --build</pre></li><li>ブラウザで <code>http://127.0.0.1:8765</code> を開きます。上部の点が緑なら定期実行が動いています</li></ol><p class="mute">ビルドが buildx のエラーで失敗するときは、PowerShell で次を設定してからやり直します。</p><pre class="gcode">$env:COMPOSE_BAKE="false"; $env:DOCKER_CONTEXT="desktop-linux"</pre></div>
<div class="gcard t"><div class="gk">STEP 2 · Alpaca ペーパー口座をつなぐ</div><ol><li>Alpaca の <b>Paper Trading</b> 画面で API キーを発行します</li><li><code>.env.example</code> を <code>.env</code> にコピーし、<code>APCA_API_KEY_ID</code> と <code>APCA_API_SECRET_KEY</code> を書きます。キーはチャットに貼りません</li><li>.env はコンテナを作るときにしか読まれないので、tick を作り直します<pre class="gcode">docker compose --profile tick up -d --force-recreate tick</pre></li><li>読み取りだけでキーを確かめます（発注はしません）。<code>ok: true</code> なら接続できています<pre class="gcode">docker compose --profile tools run --rm desk python -m hedgedesk check-alpaca</pre></li><li>「Alpacaペーパー口座」のタイルが <b>キー設定済み</b> になれば完了です</li></ol></div>
<div class="gcard t"><div class="gk">STEP 3 · ニュースのAI分析（任意）</div><ol><li><code>.env</code> に <code>ANTHROPIC_API_KEY</code> を書き、STEP 2 と同じ手順で tick を作り直します</li><li>次の収集（30分ごと）のあと、「AI分析」のタイルが <b>稼働中</b> になります。1回に分析するのは新しい見出しから最大40件です</li><li>モデルは <code>HEDGEDESK_NEWS_MODEL</code> で変更でき、<code>HEDGEDESK_NEWS_AI=off</code> で AI 分析だけを止められます</li></ol></div>
<div class="gsec">DAILY · 画面の読み方</div>
<div class="gcard"><div class="gk">STEP 4 · デスクの状態</div><ul><li><b>上部カードの色</b> — 緑：取引の条件が揃っている／赤：キルスイッチ作動か定期実行の停止／それ以外：調査だけで取引していない</li><li><b>5分ごとの定期実行</b> — 「停止の可能性」なら tick が動いていません</li><li><b>チャンピオン</b> — 基準を通った戦略。なしなら取引しません</li><li><b>保有・発注中</b>・<b>データの不備</b> — 件数と、日足が欠けている銘柄</li></ul></div>
<div class="gcard"><div class="gk">STEP 5 · 戦略の順位と「今すぐ更新」</div><ul><li>合格ラインはスコア <b>18/25 以上</b>、デフレートSR <b>0.50 以上</b>などです</li><li>事前登録した戦略は11本（試行108通り）です。押し目（S7）と値幅縮小（S8）を追加しました。試行が増えるほどデフレートSRの基準は厳しくなります</li><li>遅延ストレス（1本遅れ＋スリッページ2倍）の損益が通常より良いと「要確認」と出ます。検査の不具合を疑ってください</li><li><b>今すぐ更新</b>で調査をやり直します。jobs が1分以内に始め、数分かかります。前回から10分以内は受け付けません</li></ul></div>
<div class="gcard"><div class="gk">STEP 6 · ニュース分析</div><ul><li><b>重要度 0〜100</b> — AIの影響度・方向の強さ・銘柄との関連・値動きを決まった式で点数にし、鮮度で割り引いたもの。カーソルを合わせると内訳</li><li><b>センチメント</b> −1〜+1、<b>価格反応</b>は公開前の終値からの騰落率と普段の何σか</li><li><b>鮮度</b> — 新鮮 6時間以内／24時間以内／古い 72時間以内。72時間を超えると外します</li><li><b>AIの答え合わせ</b> — センチメントの向きが1日後・3日後の値動きと合った割合。同じ銘柄・同じ日の値動きに付いた見出しはまとめて1件と数え、30件たまるまでは参考値です。上位スコアの値動き（σ）が下位より大きければ、採点に意味があります</li><li><b>解説</b> — 既に起きた値動きの解説・まとめ記事の印。影響度を最大2として点数にします。答え合わせのタイルに「新事実」と「解説」の一致率が別々に出ます</li><li><b>フォワード記録</b> — 登録した日より後のデータだけで全戦略を追跡した成績。「整合」はバックテストどおり、「ずれ」はバックテストより悪い（または良すぎる）印。10件たまるまでは判定待ちです</li><li>参考情報です。シグナル・審査・発注には使いません</li></ul></div>
<div class="gcard"><div class="gk">STEP 7 · 1日の流れ（JST）</div><ul><li><b>07:15</b> モーニングブリーフ（画面下の「最新のブリーフ」と briefs/）</li><li><b>09:05</b> 暗号資産のシグナル判定と発注</li><li><b>21:00</b> 夜間リサーチ（戦略の順位とフォワード記録を更新）</li><li><b>NY 16:15</b> 株のシグナル判定と審査 → <b>NY 08:45・09:15</b> 再審査と発注</li><li><b>日曜 10:00</b> 週次レビュー（ペーパーとバックテストの整合）</li></ul></div>
<div class="gsec">OPS · 運用</div>
<div class="gcard t"><div class="gk">STEP 8 · 手動コマンド</div><pre class="gcode"><span class="c"># 状態（alpaca_keys が true か）</span>
docker compose --profile tools run --rm desk python -m hedgedesk status
<span class="c"># 調査をやり直す</span>
docker compose --profile tools run --rm desk python -m hedgedesk research --refresh
<span class="c"># AIを呼ばずに鮮度と価格反応だけ更新</span>
docker compose --profile tools run --rm desk python -m hedgedesk news --no-ai
<span class="c"># テスト</span>
docker compose --profile test run --rm --build test
<span class="c"># すべて止める</span>
docker compose --profile tick down</pre></div>
<div class="gcard r"><div class="gk">KILL SWITCH · キルスイッチ</div><p>損失などの条件で作動すると、新規の取引が止まります。<b>解除はオーナーだけ</b>が行います。原因を確かめてから実行し、解除はジャーナルに記録されます。</p><pre class="gcode">docker compose --profile tools run --rm desk python -m hedgedesk reset-kill</pre></div>
<div class="gcard w"><div class="gk">TROUBLESHOOTING</div><ul><li><b>.env を変えたのに効かない</b> — コンテナを作り直していません。STEP 2 の <code>--force-recreate tick</code> を実行します</li><li><b>定期実行：停止の可能性</b> — PC のスリープや Docker Desktop の停止を確認し、<code>docker compose --profile tick ps</code> と <code>docker compose --profile tick logs --tail 50 tick</code> を見ます</li><li><b>AI分析：エラー</b> — タイルに理由が出ます。キーの無効や残高不足なら .env を直して tick を作り直します</li><li><b>CPI の日程がない</b> — ブリーフが警告し、新規ローンチを止めます。<code>state/events_manual.json</code> に出典付きで追記します</li></ul></div>
<div class="gcard r"><div class="gk">RULES · 守ること</div><ul><li>接続先は Alpaca のペーパー口座に固定です。実資金の発注はできません</li><li>この画面には認証がありません。ポート設定を変えてインターネットに公開しないでください</li><li>キーは .env にだけ置き、チャットや共有ファイルに貼りません</li><li>DESK_RULES.md を変えられるのはオーナーだけです</li><li>TradingView のアラートを Webhook で発注につながないでください</li><li>ここに出る結果は検証データであり、投資助言ではありません</li></ul></div>
</div></aside>
<script>
const $=id=>document.getElementById(id);
const el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined&&text!==null)e.textContent=text;if(cls)e.className=cls;return e};
const jst=s=>s?new Date(s).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';
const usd=v=>v==null?'—':(v<0?'-$':'+$')+Math.abs(Math.round(v)).toLocaleString('en-US');
const pct=v=>v==null?'—':(v*100).toFixed(1)+'%';
function table(t,heads,rows){t.replaceChildren();const h=el('tr');heads.forEach(x=>{const c=el('th',x[0]);if(x[1])c.className='n';h.append(c)});const th=el('thead');th.append(h);const b=el('tbody');
 rows.forEach(r=>{const tr=el('tr');r.forEach((v,i)=>{const td=el('td');if(heads[i][1])td.className='n';if(v instanceof Node)td.append(v);else td.textContent=v??'—';tr.append(td)});b.append(tr)});
 if(!rows.length&&heads.length){const tr=el('tr'),td=el('td','データなし','empty');td.colSpan=heads.length;tr.append(td);b.append(tr)}t.append(th,b)}
function tile(box,name,label,cls,why){const t=el('div',null,'tile t-'+cls);t.append(el('div',name,'k'),el('div',label,'v'),el('div',why||'','why'));box.append(t)}
function row(name,label,cls,why){tile($('stat'),name,label,cls,why)}
function meter(ratio,cls){const m=el('div',null,'meter'),f=el('i',null,'m-'+cls);f.style.width=Math.max(0,Math.min(1,ratio||0))*100+'%';m.append(f);return m}
function senti(v,cls){const s=el('div',null,'senti'),f=el('i',null,'m-'+cls),x=Math.max(-1,Math.min(1,v||0));f.style.left=(50+Math.min(0,x)*50)+'%';f.style.width=Math.abs(x)*50+'%';s.append(f);return s}
const kinds={research:'検証',veto:'拒否',handoff:'受け渡し',order:'発注',fill_opened:'約定',fill_closed:'決済',kill_switch:'キルスイッチ',kill_reset:'キルスイッチ解除',owner_decision:'オーナー判断',champion:'チャンピオン',job_error:'エラー',order_not_sent:'未発注',weekly_review:'週次レビュー',no_session:'休場'};
function jsum(r){if(r.kind==='research')return r.IDEA+'：'+r.NEXT_STEP;if(r.kind==='owner_decision')return r.decision;if(r.kind==='veto')return r.TICKER+'：'+(r.EVIDENCE||[]).join('、');if(r.kind==='kill_switch')return r.reason;if(r.TICKER)return r.TICKER+(r.IDEA?'：'+r.IDEA:'');return r.reason||r.job||r.action||''}
const ago=s=>{if(!s)return null;const m=Math.floor((Date.now()-new Date(s).getTime())/60000);return m<0?null:m};
const agoText=m=>m==null?'—':m<60?m+'分前':m<1440?Math.floor(m/60)+'時間前':Math.floor(m/1440)+'日前';
function freshOf(m){return m==null?['時刻不明','off']:m<=360?['新鮮','ok']:m<=1440?['24時間以内','warn']:m<=4320?['古い','off']:['期限切れ','bad']}
const sentCls={positive:'ok',negative:'bad',neutral:'off',mixed:'warn'};
function sgn(v,d){return v==null?'—':(v>0?'+':'')+(v*100).toFixed(d??1)+'%'}
function renderNews(n){
 const st=$('newsstat');st.replaceChildren();const add=(a,b,c,d)=>tile(st,a,b,c,d);
 if(!n){$('newsasof').textContent='';add('ニュース分析','未作成','off','tickコンテナの次の収集（30分ごと）で作られます');table($('newsai'),[],[]);$('newsnote').textContent='';return}
 const ai=n.ai||{},f=n.freshness||{},c=n.counts||{};
 $('newsasof').textContent=jst(n.as_of_utc)+' JST 時点';
 const aiLabel=!ai.configured?['未設定','off','ANTHROPIC_API_KEY がないため、翻訳とセンチメントは行っていません']:ai.last_status==='error'?['エラー','bad',(ai.last_errors||[]).join(' / ')]:ai.last_status==='partial'?['一部失敗','warn',(ai.last_errors||[]).join(' / ')]:['稼働中','ok','モデル '+ai.model+'、最終成功 '+jst(ai.last_success_utc)+' JST'];
 add('AI分析',aiLabel[0],aiLabel[1],aiLabel[2]);
 const sm=ago(f.scout_last_collected_utc),sStale=sm==null||sm>45;
 add('ニュース収集',sStale?'停止の可能性':'稼働中',sStale?'bad':'ok','最終収集 '+agoText(sm)+'、最新の見出しの公開 '+agoText(ago(f.newest_published_utc)));
 add('分析済み',(c.analyzed||0)+' / '+(c.total||0)+'件',c.pending?(f.analysis_stale?'warn':'off'):'ok',(c.pending?'未分析 '+c.pending+'件'+(f.analysis_stale?'（分析が遅れています）':''):'')+(c.expired_excluded?' 期限切れ（'+f.window_hours+'時間超）で除外 '+c.expired_excluded+'件':''));
 const ps=f.price_stale_symbols||[];add('価格データ',ps.length?'古い銘柄あり':'最新',ps.length?'warn':'ok',ps.length?ps.join('、')+' の日足が古いため、価格反応は参考になりません':'Yahoo日足（夜間リサーチとブリーフで更新）');
 const ev=n.evaluation||{};
 [['1','1日後'],['3','3日後']].forEach(([h,lab])=>{const e=(ev.horizons||{})[h];
  if(!e){add('AIの答え合わせ（'+lab+'）',ev.error?'集計エラー':'記録なし','off',ev.error||'分析済みの見出しが記録されると、値動きと照合します');return}
  const d=e.direction||{},hr=d.hit_rate,bs=e.by_score||{};
  const cls=!e.enough||hr==null?'off':hr>=0.55?'ok':hr<0.45?'bad':'warn';
  const lab2=hr==null?'集計待ち':'方向一致 '+Math.round(hr*100)+'%';
  const why=(e.enough?'値動き '+d.n+'件（見出し '+(d.n_headlines||0)+'件）で判定。':'件数不足のため参考値（値動き '+(d.n||0)+' / '+ev.min_samples+'件、見出し '+(d.n_headlines||0)+'件）。')+(bs.high_abs_z!=null?'事前スコア上位の値動き '+bs.high_abs_z+'σ／下位 '+bs.low_abs_z+'σ':'')+(e.rank_corr!=null?'、順位相関 '+e.rank_corr:'')+(h==='1'&&ev.pending?'。待ち '+ev.pending+'件':'')+(d.by_kind&&(d.by_kind.new||d.by_kind.recap)?'。新事実 '+(d.by_kind.new?Math.round(d.by_kind.new.hit_rate*100)+'%（'+d.by_kind.new.n+'件）':'—')+'／解説 '+(d.by_kind.recap?Math.round(d.by_kind.recap.hit_rate*100)+'%（'+d.by_kind.recap.n+'件）':'—'):'');
  add('AIの答え合わせ（'+lab+'）',lab2,cls,why)});
 table($('newsai'),[['重要度',1],['銘柄'],['見出し'],['センチメント'],['価格反応',1],['鮮度']],(n.items||[]).map(r=>{
  const a=r.analysis,imp=r.importance,p=r.price||{};
  const icls=imp?(imp.label==='高'?'bad':imp.label==='中'?'warn':'off'):'off';
  const ic=el('div');ic.append(el('span',imp?imp.total+' '+imp.label:'未分析','badge '+icls));if(imp)ic.append(meter(imp.total/100,icls));
  if(imp)ic.title='影響度 '+imp.components.impact+' / 方向 '+imp.components.sentiment+' / 関連 '+imp.components.relevance+' / 価格 '+imp.components.price+' × 鮮度 '+imp.freshness_weight;
  const t=el('div');let head=a?a.title_ja:r.title;
  if(/^https?:\\/\\//.test(r.link||'')){const l=el('a',head);l.href=r.link;l.target='_blank';l.rel='noopener noreferrer';t.append(l)}else t.append(el('span',head));
  if(a){t.append(el('div',r.title,'fail'));const cr=el('div',null,'fail');if(a.kind==='recap'){const b=el('span','解説','badge off');b.title='既に起きた値動きや出来事の解説・まとめ。影響度を最大2として点数にしています';cr.append(b,' ')}cr.append(a.category_label+'：'+a.reason_ja);t.append(cr)}else if(r.analysis_error)t.append(el('div','分析失敗：'+r.analysis_error,'fail'));
  const se=el('div');if(a){se.append(el('span',a.sentiment_label+' '+(a.sentiment_score>0?'+':'')+a.sentiment_score.toFixed(2),'badge '+sentCls[a.sentiment]),senti(a.sentiment_score,a.sentiment_score>0?'ok':a.sentiment_score<0?'bad':'off'))}else se.append(el('span','—'));
  const pr=el('div');const why={pending:'公開後の足なし',no_time:'公開時刻不明',no_base:'公開前の足なし',no_data:'データなし'};
  pr.append(el('div',p.status==='ok'?sgn(p.reaction)+(p.z!=null?'（'+p.z.toFixed(1)+'σ）':''):why[p.status]||'—'));
  pr.append(el('div',(p.proxy?'市場全体の代わりに ':'')+(p.symbol||'')+(p.as_of?' '+p.as_of+' 終値':'')+(p.stale?'（古い）':''),'fail'));
  const fm=ago(r.published_utc),fr=freshOf(fm),fc=el('div');fc.append(el('span',fr[0],'badge '+fr[1]),el('div',agoText(fm),'fail'));
  return[ic,r.symbol||(a&&a.symbols.length?a.symbols.join(' '):'市場'),t,se,pr,fc]}));
 $('newsnote').textContent=(n.notice||'')+' 重要度は、AIの影響度・方向の強さ・保有/対象銘柄との関連・公開後の値動き（日足、普段の何σか）を決まった式で点数にし、鮮度で割り引いたものです（重要度にカーソルを合わせると内訳）。翻訳とセンチメントはAIの出力で、誤りを含むことがあります。答え合わせは、AIのセンチメントの向きが公開前の終値から1日後・3日後の値動きと合った割合と（同じ銘柄・同じ日の値動きを共有する見出しはまとめて1件と数えます）、価格を含まない事前スコアの上位半分と下位半分で値動き（普段の何σか）を比べたものです。'}
function renderForward(f){
 if(!f){$('fwdasof').textContent='';table($('fwd'),[],[]);$('fwdnote').textContent='次の夜間リサーチ（21:00 JST）の後に記録が始まります。';return}
 $('fwdasof').textContent=jst(f.as_of_utc)+' JST 時点';
 const r2=v=>v==null?'—':(v>0?'+':'')+v.toFixed(2)+'R';
 table($('fwd'),[['戦略'],['記録開始'],['日数',1],['トレード',1],['平均R',1],['バックテストの90%範囲',1],['整合'],['最新の足のシグナル']],(f.rows||[]).map(r=>{
  const n=el('div');n.append(el('div',r.name));if(r.passed_now)n.append(el('span','合格','badge ok'));
  const c=r.consistent===true?['整合','ok']:r.consistent===false?['ずれ','bad']:['判定待ち','off'];
  const cc=el('div');cc.append(el('span',c[0],'badge '+c[1]));if(r.consistent==null)cc.append(el('div',r.trades+' / '+f.min_trades+'件','fail'));
  const sg=el('div');if(!r.signals_today.length)sg.append(el('span','—'));r.signals_today.forEach(x=>sg.append(el('div',x.symbol+' 指値 '+x.limit+' / 逆指値 '+x.stop)));
  return[n,r.start,r.bars,r.trades,r2(r.mean_r),r.range90?r2(r.range90[0])+' 〜 '+r2(r.range90[1]):'—',cc,sg]}));
 $('fwdnote').textContent='登録した日のパラメータを固定し、その後の足だけで同じシミュレータ（コスト・キルスイッチ込み）を回した成績です。バックテストと違い、過去を見て選び直していません。整合は、平均Rがバックテスト（OOS）のトレード分布から同じ件数を引いたときの90%範囲に入るかで、'+f.min_trades+'件未満は判定しません。最新の足のシグナルは参考表示で、発注には使いません。'+((f.history||[]).length?' パラメータが変わった古い登録 '+f.history.length+'件も追跡を続けています。':'')}
function render(s){
 const champ=s.champion,kill=s.kill_switch&&s.kill_switch.active,canTrade=champ&&s.alpaca_keys&&!kill;
 $('lead').textContent=kill?'キルスイッチが作動中です。新規の取引は止まっています。':canTrade?'ペーパートレードの稼働条件が揃っています。':!s.tick_healthy?'定期実行が止まっている可能性があります。':'調査は稼働中です。取引はしていません。'+(champ?'Alpacaのキーが未設定です。':'基準を通った戦略がありません。');
 $('hero').dataset.s=kill||!s.tick_healthy?'bad':canTrade?'ok':'idle';$('dot').dataset.s=s.tick_healthy?'ok':'bad';
 $('updated').textContent='更新 '+jst(s.now_utc)+' JST（15秒ごとに更新）';
 $('stat').replaceChildren();
 row('5分ごとの定期実行',s.tick_healthy?'稼働中':'停止の可能性',s.tick_healthy?'ok':'bad','最終実行 '+jst(s.last_tick)+' JST');
 row('チャンピオン',champ?champ.id:'なし',champ?'ok':'off',champ?'スコア '+champ.score:'基準を通った戦略がないため、取引しません');
 row('Alpacaペーパー口座',s.alpaca_keys?'キー設定済み':'未接続',s.alpaca_keys?'ok':'off',s.alpaca_keys?'':'キーがない間は発注しません');
 row('キルスイッチ',kill?'作動中':'未作動',kill?'bad':'ok',kill?s.kill_switch.reason:'');
 row('保有・発注中',s.open_book.length+'件',s.open_book.length?'warn':'off','');
 const issues=Object.entries(s.data_issues||{});
 row('データの不備',issues.length?issues.length+'銘柄':'なし',issues.length?'warn':'ok',issues.map(([k,v])=>k+'：'+v.join('、')).join(' / '));
 const rf=s.refresh||{};$('refresh').disabled=!!(rf.pending||rf.running);$('refreshmsg').textContent=rf.running?'調査を実行中です（数分かかります）':rf.pending?'更新を受け付けました。jobsコンテナが1分以内に開始します':'';
 $('research').textContent=s.research_as_of?jst(s.research_as_of)+' JST 時点、累計'+s.n_trials+'試行、合格'+s.passed_ids.length+'本':'';
 table($('strat'),[['戦略'],['判定'],['スコア',1],['OOSシャープ',1],['デフレートSR',1],['トレード',1],['最大DD',1],['OOS損益',1],['遅延ストレス',1]],s.strategies.map(r=>{const n=el('div');n.append(el('div',r.name));if(!r.passed)n.append(el('div',r.failures.join(' / '),'fail'));const st=el('span',usd(r.stress_pnl));if(r.stress_better)st.append(' ',el('span','要確認','badge warn'));const sc=el('div');sc.append(el('div',r.score+'/25'),meter(r.score/25,r.passed?'ok':r.score>=18?'warn':'acc'));return[n,el('span',r.passed?'合格':'不合格','badge '+(r.passed?'ok':'off')),sc,r.oos_sharpe.toFixed(2),r.dsr.toFixed(2),r.trades,pct(r.max_dd),usd(r.net_pnl),st]}));
 $('stratnote').textContent='合格ライン: スコア18以上、デフレートSR 0.50以上など。遅延ストレスは1本遅れ＋スリッページ2倍の損益で、通常より良い場合は検査の不具合を疑います。'+(s.blocked.length?' 未検証: '+s.blocked.map(b=>b.name).join('、')+'。':'');
 table($('events'),[['日付'],['内容'],['出典']],s.events.map(e=>{const a=/^https?:\\/\\//.test(e.source||'')?el('a',e.official_confirmed===false?'第三者情報':'公式'):el('span','—');if(a.tagName==='A'){a.href=e.source;a.target='_blank';a.rel='noopener noreferrer'}return[e.date,e.name,a]}));
 $('eventsnote').textContent=s.events_as_of?'今後7日間。'+jst(s.events_as_of)+' JST 時点。':'イベント情報はまだありません。';
 table($('sched'),[['処理'],['タイミング'],['実行元']],s.schedule.map(x=>[x.job,x.when,x.runner]));
 table($('journal'),[['時刻(JST)'],['種類'],['担当'],['内容']],s.journal.map(r=>[jst(r.ts),kinds[r.kind]||r.kind,r.agent,jsum(r)]));
 renderNews(s.news);renderForward(s.forward);
 table($('news'),[['銘柄'],['見出し'],['出所']],s.headlines.map(h=>{let t=h.title;if(/^https?:\\/\\//.test(h.link||'')){t=el('a',h.title);t.href=h.link;t.target='_blank';t.rel='noopener noreferrer'}return[h.symbol||'—',t,h.source]}));
 if(s.brief){$('briefname').textContent='最新のブリーフ（'+s.brief.name+'）';$('brief').textContent=s.brief.text}
}
async function load(){try{const r=await fetch('/api/status');render(await r.json())}catch(e){$('lead').textContent='状況を取得できません。サーバーが止まっている可能性があります。';$('hero').dataset.s='bad';$('dot').dataset.s='bad';$('updated').textContent='接続できません'}}
async function requestRefresh(){const b=$('refresh');b.disabled=true;$('refreshmsg').textContent='送信中です';
 try{const r=await fetch('/api/refresh',{method:'POST',headers:{'X-HedgeDesk-Action':'refresh'}});const j=await r.json();
  $('refreshmsg').textContent=r.status===202?'更新を受け付けました。jobsコンテナが1分以内に開始します':j.status==='too_soon'?'前回の調査から10分以内です。あと約'+Math.ceil(j.retry_after_seconds/60)+'分お待ちください':j.status==='already_requested'?'すでに更新中です':'更新を受け付けられませんでした';
 }catch(e){$('refreshmsg').textContent='送信に失敗しました'}
 if(document.visibilityState!=='hidden')setTimeout(load,1500);setTimeout(()=>{if(!$('refreshmsg').textContent.includes('中'))b.disabled=false},3000)}
$('refresh').addEventListener('click',requestRefresh);
$('logo').addEventListener('error',()=>{$('logo').hidden=true;$('logotext').hidden=false});
(()=>{const g=$('guide'),bar=$('guidebar'),btn=$('guidebtn'),fb=$('guidefold'),KEY='hedgedesk.guide';let st={};
 try{st=JSON.parse(localStorage.getItem(KEY)||'{}')||{}}catch(e){st={}}
 const save=()=>{try{localStorage.setItem(KEY,JSON.stringify(st))}catch(e){}};
 function place(x,y){const r=g.getBoundingClientRect();x=Math.max(8,Math.min(innerWidth-r.width-8,x));y=Math.max(8,Math.min(innerHeight-r.height-8,y));Object.assign(g.style,{left:x+'px',top:y+'px',right:'auto',bottom:'auto'});st.x=x;st.y=y}
 function fold(f){g.dataset.fold=f?'1':'';fb.setAttribute('aria-expanded',String(!f));fb.title=f?'開く':'たたむ';st.fold=f;if(st.x!=null)place(st.x,st.y);save()}
 function show(open){g.hidden=!open;btn.setAttribute('aria-expanded',String(open));st.open=open;if(open&&st.x!=null)place(st.x,st.y);save()}
 let drag=null;
 bar.addEventListener('pointerdown',e=>{if(e.button!==0||e.target.closest('button'))return;const r=g.getBoundingClientRect();drag={dx:e.clientX-r.left,dy:e.clientY-r.top};bar.setPointerCapture(e.pointerId)});
 bar.addEventListener('pointermove',e=>{if(drag)place(e.clientX-drag.dx,e.clientY-drag.dy)});
 const end=()=>{if(drag){drag=null;save()}};bar.addEventListener('pointerup',end);bar.addEventListener('pointercancel',end);
 bar.addEventListener('keydown',e=>{const d={ArrowLeft:[-16,0],ArrowRight:[16,0],ArrowUp:[0,-16],ArrowDown:[0,16]}[e.key];if(!d)return;e.preventDefault();const r=g.getBoundingClientRect();place(r.left+d[0],r.top+d[1]);save()});
 g.addEventListener('keydown',e=>{if(e.key==='Escape'){show(false);btn.focus()}});
 fb.addEventListener('click',()=>fold(g.dataset.fold!=='1'));
 btn.addEventListener('click',()=>{show(g.hidden);if(!g.hidden)bar.focus()});
 addEventListener('resize',()=>{if(!g.hidden&&st.x!=null)place(st.x,st.y)});
 fold(st.fold??innerWidth<560);show(st.open!==false)})();
load();setInterval(load,15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    """サービス画面の HTTP ハンドラー。ThreadingHTTPServer がリクエストごとにスレッドで生成する。"""
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        """応答を送る。Cache-Control: no-store で、古い状況がブラウザのキャッシュから表示されないようにする。"""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        # パスは完全一致で判定する。state/ のファイルを直接返すような経路は作らない（それ以外は404）。
        if self.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/logo.png":
            # ロゴ画像。決まった1ファイルだけを返す（パスを組み立てないので、ほかのファイルは読めない）。
            try:
                self._send(200, LOGO.read_bytes(), "image/png")
            except OSError:
                self._send(404, b"not found", "text/plain")
        elif self.path == "/api/status":
            body = json.dumps(status(), ensure_ascii=False, default=str).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        """「今すぐ更新」だけを受け付ける。

        ほかのサイトに置かれたフォームやスクリプトから押されないよう、独自ヘッダーを必須にする。
        独自ヘッダー付きの送信はブラウザが事前確認するが、この画面はそれを許可しないので、他サイトからは届かない。
        """
        if self.path != "/api/refresh":
            self._send(404, b"not found", "text/plain")
            return
        if self.headers.get("X-HedgeDesk-Action") != "refresh" or int(self.headers.get("Content-Length") or 0) > 1024:
            self._send(403, b"forbidden", "text/plain")
            return
        origin = self.headers.get("Origin")
        # Origin が付いていれば、Host と同じ（同じ画面から送られた）ことを確かめる。独自ヘッダーと合わせた二重の防御。
        if origin and origin.split("://", 1)[-1] != self.headers.get("Host"):
            self._send(403, b"forbidden", "text/plain")
            return
        research = _read_json(C.STATE / "latest_research.json") or {}
        code, body = refresh.request(research.get("as_of_utc"))
        # 直近の調査時刻を渡して、10分以内の連続依頼を refresh.request 側で断る（429）。
        self._send(code, json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def log_message(self, *args):
        # 既定ではアクセスごとに標準エラーへログを出すが、15秒ごとのポーリングでログが埋まるので出さない。
        pass


def serve() -> None:
    """HOST:PORT でサーバーを起動し、止められるまで戻らない。"""
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
