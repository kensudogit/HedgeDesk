# HedgeDesk のコンテナ。キーや実行結果はイメージに入れない（.dockerignore 参照）。
FROM python:3.12.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 依存はすべて requirements.lock で固定する
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.lock

# root では動かさない
RUN useradd --create-home --uid 1000 desk
WORKDIR /app
COPY hedgedesk ./hedgedesk
COPY DESK_RULES.md ./
USER desk

CMD ["python", "-m", "hedgedesk", "status"]


# テスト用。docker compose --profile test run --rm test で実行する。
FROM runtime AS test
USER root
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements-dev.txt
COPY tests ./tests
COPY pytest.ini ./
USER desk
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
