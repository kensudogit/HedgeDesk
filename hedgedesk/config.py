"""Desk Rules を数値にしたもの。値を変えるときは DESK_RULES.md も同時に直す。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
CACHE = ROOT / "data" / "cache"
REPORTS = ROOT / "reports"
BRIEFS = ROOT / "briefs"
REQUESTS = ROOT / "requests"  # サービス画面からの更新依頼（画面が書き込める唯一の場所）
JOURNAL = ROOT / "journal" / "trade_journal.jsonl"

# 資金とリスク（オーナー回答）
CAPITAL = 100_000.0
RISK_PER_TRADE = 0.005
MAX_OPEN_RISK = 0.025
MAX_POSITION_NOTIONAL = 0.20
MIN_HOLD_BARS = 2
MAX_HOLD_BARS = 10

# 禁止事項の定義
MIN_PRICE = 10.0
MIN_DOLLAR_VOLUME = 20_000_000.0
BANNED = {"TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SPXU", "SOXL", "SOXS", "SH", "PSQ", "DOG", "TNA", "TZA", "UVXY", "SVXY"}

# キルスイッチ
KILL_DAILY_LOSS = 0.02
KILL_CONSECUTIVE_LOSSES = 3
STALE_MINUTES = 15
STALE_FAILURES = 3
# バックテストでは「オーナーが5営業日後に再開を承認した」と仮定する（仮定であり事実ではない）
BACKTEST_KILL_PAUSE_BARS = 5

# Chart Analyst
MIN_SETUP_SCORE = 7

# コスト（推定を含む。DESK_RULES.md 第7節）
SLIP_BPS = 5.0
EQUITY_FEE_BPS = 0.0
CRYPTO_MAKER_BPS = 15.0
CRYPTO_TAKER_BPS = 25.0

# 検証期間
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
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

# 銘柄ユニバース。今日時点の大型株なので、個別株の結果には生存バイアスが残る。
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

ANNUALIZATION = {"etf": 252, "stock": 252, "crypto": 365}
