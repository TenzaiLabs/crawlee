FROM python:3.14-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv

COPY pyproject.toml uv.lock /app/

RUN uv sync --frozen --no-dev --no-install-project

ENV PATH="/app/.venv/bin:$PATH"

RUN .venv/bin/playwright install --with-deps chromium

ARG KATANA_VERSION=1.6.1
ARG KATANA_SHA256=503754f1bd370c3ef287df6998e317baed2dd75bdd13ea64034f09b80ca393f3
ARG PROXIFY_VERSION=0.0.16

RUN curl -fsSL -o /tmp/katana.zip \
      "https://github.com/projectdiscovery/katana/releases/download/v${KATANA_VERSION}/katana_${KATANA_VERSION}_linux_amd64.zip" \
    && echo "${KATANA_SHA256}  /tmp/katana.zip" | sha256sum -c - \
    && unzip -q /tmp/katana.zip katana -d /usr/local/bin \
    && rm /tmp/katana.zip

RUN curl -fsSL -o /tmp/proxify.zip \
      "https://github.com/projectdiscovery/proxify/releases/download/v${PROXIFY_VERSION}/proxify_${PROXIFY_VERSION}_linux_amd64.zip" \
    && unzip -q /tmp/proxify.zip proxify replay mitmrelay -d /usr/local/bin \
    && rm /tmp/proxify.zip

COPY app /app/app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
