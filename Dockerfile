FROM python:3.14-rc-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv

COPY pyproject.toml /app/pyproject.toml

RUN uv pip install --system fastapi uvicorn aiosqlite playwright llm

RUN python -m playwright install --with-deps chromium

ARG KATANA_VERSION=1.1.0
ARG PROXIFY_VERSION=1.0.0

RUN curl -fsSL -o /tmp/katana.zip \
      "https://github.com/projectdiscovery/katana/releases/download/v${KATANA_VERSION}/katana_${KATANA_VERSION}_linux_amd64.zip" \
    && unzip /tmp/katana.zip -d /usr/local/bin \
    && rm /tmp/katana.zip

RUN curl -fsSL -o /tmp/proxify.zip \
      "https://github.com/projectdiscovery/proxify/releases/download/v${PROXIFY_VERSION}/proxify_${PROXIFY_VERSION}_linux_amd64.zip" \
    && unzip /tmp/proxify.zip -d /usr/local/bin \
    && rm /tmp/proxify.zip

COPY app /app/app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
