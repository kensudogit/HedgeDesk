"""HedgeDesk: one-person paper-trading research desk. Read DESK_RULES.md first."""
#
# 【役割】hedgedesk パッケージの目印。1人で運用するペーパートレード専用の調査デスク。
# 【構成（エージェントに見立てたモジュール）】
#   - research.py   … Strategist: 全仮説をウォークフォワード／ホールドアウトで検証し、チャンピオンを選ぶ
#   - analyst.py    … Chart Analyst: チャンピオン戦略のシグナルから売買計画を作る
#   - risk.py       … Risk Officer: 計画を機械的に審査し、数量を決める（拒否は最終決定）
#   - trader.py     … Trader: Alpaca のペーパー口座だけに発注する唯一のモジュール
#   - scout.py      … Scout: ニュース見出しを集めるだけ（判断はしない）
#   - desk.py / scheduler.py / brief.py … Chief of Staff: 定期実行、ブリーフ、週次レビュー
#   - ui.py         … 読み取り専用のサービス画面
# 【主な依存先】なし（このファイルは何も import しない）。
