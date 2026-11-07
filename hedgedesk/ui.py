"""サービス画面: Deskの状況を表示する読み取り専用のローカルサーバー。

- 127.0.0.1 だけで待ち受ける。PCの外からは見えない。
- GETのみ。発注、キルスイッチ解除、設定変更の機能は持たない。
- ニュースの見出しなど外部の文字列は、画面側で textContent としてのみ表示する。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as C
from . import journal, risk, scout, trader

# 既定はこのPCのみ。コンテナの中でだけ 0.0.0.0 にし、公開範囲は Compose 側で 127.0.0.1 に絞る。
# この画面に認証はないので、インターネットへ直接公開しないこと。
HOST = os.environ.get("HEDGEDESK_UI_HOST", "127.0.0.1")
PORT = 8765
TICK_STALE_SECONDS = 600

SCHEDULE = [
    {"job": "Scout（ニュース収集）", "when": "30分ごと", "runner": "tickコンテナ"},
    {"job": "キルスイッチ監視、約定確認", "when": "5分ごと", "runner": "tickコンテナ"},
    {"job": "株のシグナル判定と審査", "when": "米国の引け後（NY 16:15）", "runner": "tickコンテナ"},
    {"job": "株の再審査、発注", "when": "NY 08:45、09:15", "runner": "tickコンテナ"},
    {"job": "暗号資産のシグナル判定と発注", "when": "09:05 JST", "runner": "tickコンテナ"},
    {"job": "夜間リサーチ（Strategist）", "when": "毎日 21:00 JST", "runner": "jobsコンテナ"},
    {"job": "モーニングブリーフ", "when": "毎日 07:15 JST", "runner": "jobsコンテナ"},
    {"job": "週次レビュー", "when": "日曜 10:00 JST", "runner": "jobsコンテナ"},
]


def _read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _last_tick() -> dict | None:
    path = C.STATE / "tick.log"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return json.loads(lines[-1]) if lines else None


def _strategy_row(r: dict) -> dict:
    oos, stress = r["out_of_sample"], r["stress_late_double_slip"]
    return {
        "id": r["id"], "name": r["name"], "score": r["score"]["total"], "oos_sharpe": oos["sharpe"], "dsr": r["deflated_sharpe"],
        "trades": oos["trades"], "max_dd": oos["max_drawdown"], "net_pnl": oos["net_pnl"], "stress_pnl": stress["net_pnl"],
        "stress_better": stress["net_pnl"] > oos["net_pnl"], "passed": r["passed"], "failures": r["failures"],
    }


def _latest_brief() -> dict | None:
    files = sorted(C.BRIEFS.glob("*.md")) if C.BRIEFS.exists() else []
    if not files:
        return None
    return {"name": files[-1].name, "text": files[-1].read_text(encoding="utf-8")}


def status(now: datetime | None = None) -> dict:
    """画面に出す内容をまとめる。キーの値は含めない。"""
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
        "brief": _latest_brief(),
    }


PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HedgeDesk</title>
<style>
:root{--bg:#f7f6f3;--panel:#fff;--ink:#1f2328;--mute:#626a73;--line:#e2e0da;--ok:#17694a;--okbg:#e2f3ec;--bad:#a32b2b;--badbg:#fbe9e7;--warn:#8a5a00;--warnbg:#fbf1dc;--off:#555d66;--offbg:#eceae5}
@media (prefers-color-scheme:dark){:root{--bg:#15171a;--panel:#1e2125;--ink:#e8e6e1;--mute:#a0a6ad;--line:#33373c;--ok:#6fd0a8;--okbg:#17332a;--bad:#f09a90;--badbg:#3c1f1d;--warn:#e6bd6a;--warnbg:#3a2e14;--off:#b3b8be;--offbg:#2a2d31}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "Segoe UI","Yu Gothic UI","Hiragino Sans",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:4px 16px;margin-bottom:6px}
h1{font-size:24px;margin:0;font-weight:650}h2{font-size:16px;margin:0 0 10px;font-weight:650}
.mute{color:var(--mute);font-size:13px}.lead{font-size:17px;margin:0 0 20px}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:16px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:7px 10px 7px 0;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;font-weight:600;color:var(--mute);white-space:nowrap}tr:last-child td{border-bottom:0}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:13px;font-weight:600;white-space:nowrap}
.ok{color:var(--ok);background:var(--okbg)}.bad{color:var(--bad);background:var(--badbg)}.warn{color:var(--warn);background:var(--warnbg)}.off{color:var(--off);background:var(--offbg)}
.stat{display:grid;grid-template-columns:minmax(150px,220px) auto 1fr;gap:6px 12px;align-items:baseline}.stat .why{color:var(--mute);font-size:13px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}.two section{margin:0}.two{margin-bottom:16px}
.fail{font-size:13px;color:var(--mute);margin-top:2px}a{color:inherit}pre{white-space:pre-wrap;word-break:break-word;margin:0;font:inherit;font-size:14px}
details summary{cursor:pointer;font-weight:650}details[open] summary{margin-bottom:10px}
@media (max-width:560px){.stat{grid-template-columns:1fr auto}.stat .why{grid-column:1/-1;margin-bottom:6px}}
</style></head><body><main>
<header><h1>HedgeDesk</h1><span class="mute" id="updated"></span></header>
<p class="lead" id="lead">読み込み中です</p>
<section><h2>稼働状況</h2><div class="stat" id="stat"></div></section>
<section><h2>戦略の順位 <span class="mute" id="research"></span></h2><div class="scroll"><table id="strat"></table></div><p class="mute" id="stratnote"></p></section>
<div class="two">
<section><h2>今後のイベント</h2><div class="scroll"><table id="events"></table></div><p class="mute" id="eventsnote"></p></section>
<section><h2>スケジュール</h2><div class="scroll"><table id="sched"></table></div></section>
</div>
<section><h2>最近のジャーナル</h2><div class="scroll"><table id="journal"></table></div></section>
<section><h2>最近の見出し <span class="mute">外部データ。Deskの見解ではありません</span></h2><div class="scroll"><table id="news"></table></div></section>
<section><details><summary id="briefname">最新のブリーフ</summary><pre id="brief"></pre></details></section>
<p class="mute">読み取り専用の画面です。ここから発注や設定変更はできません。検証データであり、投資助言ではありません。</p>
</main>
<script>
const $=id=>document.getElementById(id);
const el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined&&text!==null)e.textContent=text;if(cls)e.className=cls;return e};
const jst=s=>s?new Date(s).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';
const usd=v=>v==null?'—':(v<0?'-$':'+$')+Math.abs(Math.round(v)).toLocaleString('en-US');
const pct=v=>v==null?'—':(v*100).toFixed(1)+'%';
function table(t,heads,rows){t.replaceChildren();const h=el('tr');heads.forEach(x=>{const c=el('th',x[0]);if(x[1])c.className='n';h.append(c)});const th=el('thead');th.append(h);const b=el('tbody');
 rows.forEach(r=>{const tr=el('tr');r.forEach((v,i)=>{const td=el('td');if(heads[i][1])td.className='n';if(v instanceof Node)td.append(v);else td.textContent=v??'—';tr.append(td)});b.append(tr)});t.append(th,b)}
function row(name,label,cls,why){$('stat').append(el('span',name),el('span',label,'badge '+cls),el('span',why||'','why'))}
const kinds={research:'検証',veto:'拒否',handoff:'受け渡し',order:'発注',fill_opened:'約定',fill_closed:'決済',kill_switch:'キルスイッチ',kill_reset:'キルスイッチ解除',owner_decision:'オーナー判断',champion:'チャンピオン',job_error:'エラー',order_not_sent:'未発注',weekly_review:'週次レビュー',no_session:'休場'};
function jsum(r){if(r.kind==='research')return r.IDEA+'：'+r.NEXT_STEP;if(r.kind==='owner_decision')return r.decision;if(r.kind==='veto')return r.TICKER+'：'+(r.EVIDENCE||[]).join('、');if(r.kind==='kill_switch')return r.reason;if(r.TICKER)return r.TICKER+(r.IDEA?'：'+r.IDEA:'');return r.reason||r.job||r.action||''}
function render(s){
 const champ=s.champion,kill=s.kill_switch&&s.kill_switch.active,canTrade=champ&&s.alpaca_keys&&!kill;
 $('lead').textContent=kill?'キルスイッチが作動中です。新規の取引は止まっています。':canTrade?'ペーパートレードの稼働条件が揃っています。':!s.tick_healthy?'定期実行が止まっている可能性があります。':'調査は稼働中です。取引はしていません。'+(champ?'Alpacaのキーが未設定です。':'基準を通った戦略がありません。');
 $('updated').textContent='更新 '+jst(s.now_utc)+' JST（30秒ごとに更新）';
 $('stat').replaceChildren();
 row('5分ごとの定期実行',s.tick_healthy?'稼働中':'停止の可能性',s.tick_healthy?'ok':'bad','最終実行 '+jst(s.last_tick)+' JST');
 row('チャンピオン',champ?champ.id:'なし',champ?'ok':'off',champ?'スコア '+champ.score:'基準を通った戦略がないため、取引しません');
 row('Alpacaペーパー口座',s.alpaca_keys?'キー設定済み':'未接続',s.alpaca_keys?'ok':'off',s.alpaca_keys?'':'キーがない間は発注しません');
 row('キルスイッチ',kill?'作動中':'未作動',kill?'bad':'ok',kill?s.kill_switch.reason:'');
 row('保有・発注中',s.open_book.length+'件',s.open_book.length?'warn':'off','');
 const issues=Object.entries(s.data_issues||{});
 row('データの不備',issues.length?issues.length+'銘柄':'なし',issues.length?'warn':'ok',issues.map(([k,v])=>k+'：'+v.join('、')).join(' / '));
 $('research').textContent=s.research_as_of?jst(s.research_as_of)+' JST 時点、累計'+s.n_trials+'試行、合格'+s.passed_ids.length+'本':'';
 table($('strat'),[['戦略'],['判定'],['スコア',1],['OOSシャープ',1],['デフレートSR',1],['トレード',1],['最大DD',1],['OOS損益',1],['遅延ストレス',1]],s.strategies.map(r=>{const n=el('div');n.append(el('div',r.name));if(!r.passed)n.append(el('div',r.failures.join(' / '),'fail'));const st=el('span',usd(r.stress_pnl));if(r.stress_better)st.append(' ',el('span','要確認','badge warn'));return[n,el('span',r.passed?'合格':'不合格','badge '+(r.passed?'ok':'off')),r.score+'/25',r.oos_sharpe.toFixed(2),r.dsr.toFixed(2),r.trades,pct(r.max_dd),usd(r.net_pnl),st]}));
 $('stratnote').textContent='合格ライン: スコア18以上、デフレートSR 0.50以上など。遅延ストレスは1本遅れ＋スリッページ2倍の損益で、通常より良い場合は検査の不具合を疑います。'+(s.blocked.length?' 未検証: '+s.blocked.map(b=>b.name).join('、')+'。':'');
 table($('events'),[['日付'],['内容'],['出典']],s.events.map(e=>{const a=/^https?:\\/\\//.test(e.source||'')?el('a',e.official_confirmed===false?'第三者情報':'公式'):el('span','—');if(a.tagName==='A'){a.href=e.source;a.target='_blank';a.rel='noopener noreferrer'}return[e.date,e.name,a]}));
 $('eventsnote').textContent=s.events_as_of?'今後7日間。'+jst(s.events_as_of)+' JST 時点。':'イベント情報はまだありません。';
 table($('sched'),[['処理'],['タイミング'],['実行元']],s.schedule.map(x=>[x.job,x.when,x.runner]));
 table($('journal'),[['時刻(JST)'],['種類'],['担当'],['内容']],s.journal.map(r=>[jst(r.ts),kinds[r.kind]||r.kind,r.agent,jsum(r)]));
 table($('news'),[['銘柄'],['見出し'],['出所']],s.headlines.map(h=>{let t=h.title;if(/^https?:\\/\\//.test(h.link||'')){t=el('a',h.title);t.href=h.link;t.target='_blank';t.rel='noopener noreferrer'}return[h.symbol||'—',t,h.source]}));
 if(s.brief){$('briefname').textContent='最新のブリーフ（'+s.brief.name+'）';$('brief').textContent=s.brief.text}
}
async function load(){try{const r=await fetch('/api/status');render(await r.json())}catch(e){$('lead').textContent='状況を取得できません。サーバーが止まっている可能性があります。'}}
load();setInterval(load,30000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            body = json.dumps(status(), ensure_ascii=False, default=str).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):
        pass


def serve() -> None:
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
