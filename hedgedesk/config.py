"""Desk Rules を数値にしたもの。値を変えるときは DESK_RULES.md も同時に直す。"""
#
# 【役割】HedgeDesk 全体で共有する定数（ファイルの置き場所、資金・リスク上限、キルスイッチ条件、
#        コスト、検証期間、銘柄ユニバース）を1か所に集めた設定モジュール。
# 【位置付け】ほぼすべてのモジュール（risk / backtest / research / trader / desk など）が
#        `from . import config as C` で参照する、依存関係の最下層。ここは他の hedgedesk
#        モジュールを import しない（循環 import を避けるため）。
# 【主な依存先】標準ライブラリ pathlib のみ。
# 【注意】テストでは conftest.py が C.STATE などを monkeypatch で一時ディレクトリに差し替える。
#        そのため各モジュールは値を import 時にコピーせず、なるべく C.STATE のように実行時に参照している。
from pathlib import Path

# リポジトリのルート（このファイルの2階層上）。コンテナ内では /app になる。
ROOT = Path(__file__).resolve().parent.parent
# 実行時の状態（チャンピオン、保有、キルスイッチ、ロックなど）。Git 管理外。
STATE = ROOT / "state"
# 市場データ・決算日の pickle キャッシュ（data.py が日付付きファイル名で保存する）。
CACHE = ROOT / "data" / "cache"
# 調査レポート・週次レビューの JSON / Markdown の出力先。
REPORTS = ROOT / "reports"
# モーニングブリーフ（Markdown）の出力先。
BRIEFS = ROOT / "briefs"
REQUESTS = ROOT / "requests"  # サービス画面からの更新依頼（画面が書き込める唯一の場所）
# 追記専用の Trade Journal（JSON Lines）。journal.py だけが書き込む。
JOURNAL = ROOT / "journal" / "trade_journal.jsonl"

# 資金とリスク（オーナー回答）
# CAPITAL: 想定資金（米ドル）。バックテストの開始資金と、ブローカー未接続時の審査用資金に使う。
# RISK_PER_TRADE: 1トレードで失ってよい額の割合（0.5%）。数量 = 資金×0.5% ÷ (指値 − ストップ)。
# MAX_OPEN_RISK: 同時に抱えるリスクの合計上限（2.5%）。0.5% のトレードなら最大5本まで同時に持てる。
# MAX_POSITION_NOTIONAL: 1銘柄の時価の上限（資金の20%）。ストップ幅が狭いときの過大な数量を抑える。
# MIN_HOLD_BARS / MAX_HOLD_BARS: スイングトレードとしての最短・最長の保有本数（日足の本数）。
CAPITAL = 100_000.0
RISK_PER_TRADE = 0.005
MAX_OPEN_RISK = 0.025
MAX_POSITION_NOTIONAL = 0.20
MIN_HOLD_BARS = 2
MAX_HOLD_BARS = 10

# 禁止事項の定義
# MIN_PRICE: 株価がこれ未満の銘柄は扱わない（低位株の値動きとスプレッドを避ける）。
# MIN_DOLLAR_VOLUME: 20日平均売買代金の下限（2,000万ドル）。約定の滑りを小さく保つための流動性条件。
# BANNED: レバレッジ型・インバース型 ETF と VIX 連動商品。日次リバランスで長期保有に向かないため禁止。
MIN_PRICE = 10.0
MIN_DOLLAR_VOLUME = 20_000_000.0
BANNED = {"TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SPXU", "SOXL", "SOXS", "SH", "PSQ", "DOG", "TNA", "TZA", "UVXY", "SVXY"}

# キルスイッチ
# KILL_DAILY_LOSS: 1日の損失がこの割合（2%）に達したら新規取引を止める。
# KILL_CONSECUTIVE_LOSSES: 決済済みトレードがこの回数連続で負けたら止める。
# STALE_MINUTES: データが古いとみなす分数（DESK_RULES 上の基準値。現時点のコードでは参照していない。tick の生存確認は desk.TICK_STALE_MINUTES）。
# STALE_FAILURES: ブローカー API がこの回数連続で失敗したら「データ停止」として止める。
KILL_DAILY_LOSS = 0.02
KILL_CONSECUTIVE_LOSSES = 3
STALE_MINUTES = 15
STALE_FAILURES = 3
# バックテストでは「オーナーが5営業日後に再開を承認した」と仮定する（仮定であり事実ではない）
BACKTEST_KILL_PAUSE_BARS = 5

# Chart Analyst
# indicators.setup_score（10点満点）でこの点数以上の足だけを候補にする。
MIN_SETUP_SCORE = 7

# コスト（推定を含む。DESK_RULES.md 第7節）
# bps = 1万分の1。SLIP_BPS は片道のスリッページ推定。
# 株・ETF は手数料0（Alpaca）。暗号資産は指値の新規をメイカー、ストップや成行の手仕舞いをテイカーとみなす。
SLIP_BPS = 5.0
EQUITY_FEE_BPS = 0.0
CRYPTO_MAKER_BPS = 15.0
CRYPTO_TAKER_BPS = 25.0

# 検証期間
# DATA_START: 取得開始日。200日移動平均などの助走期間を確保するため IS_START より1年早い。
# IS_START〜IS_END: インサンプル（パラメータ選択に使ってよい期間）。
# HOLDOUT_START 以降: ホールドアウト。選んだパラメータを1組だけ当てはめる「最後の確認用」期間。
# TRAIN_YEARS / TEST_MONTHS: ウォークフォワードの学習期間（3年）と検証期間（6か月）。
# MC_RUNS / MC_SEED: モンテカルロの試行回数と乱数シード（結果を再現できるよう固定）。
# SCORE_KEEP: 第8節のスコア（25点満点）でこれ未満は不合格。
# SUSPICIOUS_SHARPE: OOS シャープがこれを超えたら、良すぎるので不具合を疑って失格にする。
# MAX_STRESS_DD: 2020年・2022年の最大ドローダウンの許容上限。
# MIN_DSR: デフレートシャープ（試行回数で割り引いた「真のシャープが正である確率」）の下限。
# MIN_OOS_TRADES: OOS のトレード数がこれ未満だと統計的に判断できないので不合格。
DATA_START = "2017-01-01"
IS_START = "2018-01-01"
IS_END = "2024-12-31"
HOLDOUT_START = "2025-01-01"
TRAIN_YEARS = 3
TEST_MONTHS = 6
MC_RUNS = 1000
MC_SEED = 20261107
SCORE_KEEP = 18
SUSPICIOUS_SHARPE = 3.0
MAX_STRESS_DD = 0.25
MIN_DSR = 0.50
MIN_OOS_TRADES = 30

# ローンチ前に空けるべき高インパクトイベントまでの暦日数（提案）
EVENT_BLOCK_DAYS = 3

# 発注先はペーパーのみ。trader.py はこれ以外のURLを拒否する。
# trader.AlpacaPaper は ALPACA_PAPER_URL を固定値と比較して二重に検査する。
# ALPACA_DATA_URL は市場データ API の URL（現時点のコードでは参照していない）。
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

# 銘柄ユニバース。今日時点の大型株なので、個別株の結果には生存バイアスが残る。
# ETFS は指数 ETF とセクター ETF の合計。セクターローテーション戦略は SECTOR_ETFS だけを順位付けする。
# STOCKS は業種が偏らないように選んだ米国大型株。決算跨ぎ禁止の判定は STOCKS にだけ適用される。
# CRYPTO は Yahoo 表記（BTC-USD）。Alpaca へ送るときは trader.to_broker_symbol で BTC/USD に変換する。
# VIX は指数そのもので売買対象ではない。ボラティリティ局面の判定（strategies.vol_regime）に使う。
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLY", "XLB", "XLRE", "XLC"]
INDEX_ETFS = ["SPY", "QQQ", "IWM", "DIA"]
ETFS = INDEX_ETFS + SECTOR_ETFS
STOCKS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "ORCL", "CSCO", "INTC",
    "JPM", "BAC", "WFC", "GS", "MS", "BLK", "AXP",
    "JNJ", "PFE", "MRK", "UNH", "ABT", "LLY",
    "PG", "KO", "PEP", "WMT", "COST", "MCD", "HD", "NKE",
    "XOM", "CVX", "COP",
    "CAT", "BA", "HON", "UPS", "GE",
    "NEE", "DUK", "LIN", "T", "VZ", "DIS",
]
CRYPTO = ["BTC-USD", "ETH-USD"]
VIX = "^VIX"

# シャープレシオの年率換算に使う1年あたりの足の本数。株は取引日（約252日）、暗号資産は暦日（365日）で取引される。
ANNUALIZATION = {"etf": 252, "stock": 252, "crypto": 365}
