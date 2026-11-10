# HedgeDesk のコンテナ。キーや実行結果はイメージに入れない（.dockerignore 参照）。
#
# 2段構成のマルチステージビルド:
#   runtime … ui / tick / jobs / desk の各サービスが使う本番用イメージ（コードと依存だけ）
#   test    … runtime にテスト用の依存とテストコードを足したイメージ
# キー（.env）は Compose の env_file で実行時に渡し、state/ などの実行結果はボリュームで渡す。
FROM python:3.12.14-slim AS runtime

# .pyc を書かない（読み取り専用のファイルシステムで動かすため）、出力をバッファしない（docker logs にすぐ出すため）、
# pip のバージョン確認の通信をしない。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 依存はすべて requirements.lock で固定する
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.lock

# root では動かさない
RUN useradd --create-home --uid 1000 desk
WORKDIR /app
# コードとルール文書だけをコピーする。tests/ は本番イメージに入れない。
COPY hedgedesk ./hedgedesk
COPY DESK_RULES.md ./
# 画面のロゴ。public/ のほかのファイルは使わないので、この1枚だけをコピーする。
COPY public/PC.png ./public/PC.png
USER desk

# 既定のコマンドは状態の表示だけ。実際のコマンドは Compose の各サービスで指定する。
CMD ["python", "-m", "hedgedesk", "status"]


# テスト用。docker compose --profile test run --rm test で実行する。
FROM runtime AS test
# テスト用の依存を入れるために一時的に root に戻し、最後に desk に戻す。
USER root
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements-dev.txt
COPY tests ./tests
COPY pytest.ini ./
USER desk
# -p no:cacheprovider: 読み取り専用の環境でも動くよう、.pytest_cache を書かない。
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


# 既定のビルド対象（--target を指定しないビルド。Railway など）。中身は runtime と同じで、テストは入らない。
# Compose は各サービスで target を指定しているので、この段は使わない。
FROM runtime AS app
CMD ["python", "-m", "hedgedesk", "ui"]
