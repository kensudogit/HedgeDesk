# HedgeDesk

米国株、ETF、暗号資産を対象に、戦略の検証とペーパートレードを行う1人用のデスクです。ルールは [DESK_RULES.md](DESK_RULES.md) にあります。

実資金の発注はできません。接続先はAlpacaのペーパー口座に固定されています。ここに出る結果は検証データであり、投資助言や利益の保証ではありません。

## 誰が何をするか

| エージェント | 実体 | 動くタイミング |
|---|---|---|
| Chief of Staff | `brief.py`、`scheduler.py` | 毎朝07:15、日曜10:00 |
| Scout | `scout.py` | 30分ごと |
| Chart Analyst | `analyst.py`、`indicators.py` | 米国株の引け後、暗号資産はUTC 00:05 |
| Strategist | `research.py` | 毎晩21:00 |
| Risk Officer | `risk.py` | シグナルの直後と、米国の寄付き前 |
| Trader | `trader.py`（発注に触れる唯一のモジュール） | 寄付きの15分前、暗号資産はシグナルの直後 |

## できること、できないこと

- 動くのはPCとDocker Desktopの起動中だけです。Claudeアプリは不要です。ノートPCを閉じても動かすには、常時稼働のサーバーが必要です。
- スリープなどで時刻を過ぎた調査やブリーフは、起きたときに1回だけ遅れて実行します。5分ごとの処理の抜けは取り戻しません。
- ブリーフはテンプレートで作り、AIもWeb検索も使いません。`briefs/` に保存し、サービス画面で読めます。通知は届きません。
- CPIなどの日程は自動で更新されません。今後のCPI日程がなくなると、ブリーフが警告し、新規ローンチを止めます。`state/events_manual.json` に出典付きで追記してください。
- Alpacaのキーがなければ、発注は一切行いません。約定を作り出すこともありません。
- 発注まわりは模擬のHTTPでテストしただけで、**Alpacaの実環境では未検証**です。
- 基準を通った戦略がなければ、ペーパーでも取引しません。

## はじめ方（Alpacaのペーパー口座をつなぐ）

1. [Alpaca](https://alpaca.markets) で口座を作り、**Paper Trading** の画面でAPIキーを発行します。
2. `.env.example` をコピーして `.env` という名前にし、キーを貼って保存します。チャットには貼らないでください。
3. 接続を確認します。`alpaca_keys` が `true` になれば成功です。

```bash
python -m hedgedesk status
```

## サービス画面

稼働状況、戦略の順位、イベント、ジャーナルを見る画面です。読み取り専用で、このPCからしか見られません。ブラウザで http://127.0.0.1:8765 を開きます。

Dockerコンテナで動かします（次節）。Dockerを使わない場合は、次のコマンドでも起動できます。同じポートを使うので、コンテナとは同時に動かせません。

```bash
python -m hedgedesk ui
```

## Docker

Python 3.12と固定した依存パッケージで動きます。データはこのフォルダの `state/` などに残り、キーと実行結果はイメージに入りません。

| サービス | 役割 | 起動 |
|---|---|---|
| `ui` | サービス画面。データは読み取り専用 | 既定で起動 |
| `tick` | 5分ごとの処理（Scout、監視、シグナル、Trader） | `--profile tick` |
| `jobs` | 夜間リサーチ、モーニングブリーフ、週次レビュー | `--profile tick` |
| `desk` | 調査などの手動コマンド | `--profile tools` |
| `test` | テスト | `--profile test` |

すべてのサービスを起動します。

```bash
docker compose --profile tick up -d --build
```

**Docker Desktopの自動起動を有効にしてください。** Settings の General にある「Start Docker Desktop when you sign in」です。オフのままだと、PCを再起動してもコンテナは動き出しません。止まっているとサービス画面も見られないので、モーニングブリーフの `tick_health` で停止を報告します。

テストを実行します。

```bash
docker compose --profile test run --rm --build test
```

調査を手動で実行します。

```bash
docker compose --profile tools run --rm desk python -m hedgedesk research --refresh
```

ビルドが `use docker --context=desktop-linux buildx` というエラーで失敗するときは、PowerShellで次を設定してからやり直してください。

```powershell
$env:COMPOSE_BAKE="false"; $env:DOCKER_CONTEXT="desktop-linux"
```

### Dockerで解決しないこと

- PCがスリープするとコンテナも止まります。24時間動かすには常時稼働のサーバーが必要です。
- 自動で再起動するのは、Docker Desktopが動いているときだけです。
- サービス画面には認証がありません。`docker-compose.yml` のポート設定を変えて、インターネットに直接公開しないでください。

## よく使うコマンド

調査を手動で実行します。

```bash
python -m hedgedesk research --refresh
```

ブリーフの材料を作ります。

```bash
python -m hedgedesk brief-data
```

キルスイッチを解除します。**解除はオーナーだけが行います。** 原因を確認してから実行してください。

```bash
python -m hedgedesk reset-kill
```

テストを実行します。

```bash
python -m pytest -q
```

## 止め方

すべてのサービスを止めます。

```bash
docker compose --profile tick down
```

以前の定期タスク（`hedgedesk-strategist-nightly`、`hedgedesk-morning-brief`、`hedgedesk-weekly-review`）はClaudeアプリで停止してあります。再び有効にするなら、二重に実行されないよう `jobs` コンテナを止めてください。

## TradingViewの指標

[pine/hedge_desk_watch.pine](pine/hedge_desk_watch.pine) をPineエディタに貼り付けて、チャートに追加します。目視確認専用です。TradingViewの規約により、アラートをWebhookでボットや発注につながないでください。

## ファイルの場所

| 場所 | 中身 |
|---|---|
| `briefs/` | モーニングブリーフ |
| `reports/` | 調査と週次レビューの結果 |
| `journal/trade_journal.jsonl` | アイデア、拒否、ハンドオフ、トレードの全記録 |
| `state/` | チャンピオン、キルスイッチ、試行の登録簿、イベント日程 |
| `state/questions.md` | Deskからオーナーへの質問 |
"# HedgeDesk" 
