FROM golang:1.25.7-bookworm AS katana-builder

ARG KATANA_SOURCE_COMMIT=0265c675d03de83b1a1f1935ffb9c8ca9e4c17aa

RUN git clone --filter=blob:none --no-checkout \
      https://github.com/projectdiscovery/katana.git /src/katana \
    && git -C /src/katana checkout --detach "${KATANA_SOURCE_COMMIT}"

COPY third_party/katana/tenzai-v1.6.1.patch /tmp/tenzai-katana.patch

RUN git -C /src/katana apply --check /tmp/tenzai-katana.patch \
    && git -C /src/katana apply /tmp/tenzai-katana.patch \
    && cd /src/katana \
    && gofmt -w \
      cmd/katana/main.go \
      internal/runner/banner.go \
      internal/runner/executer.go \
      internal/runner/runner.go \
      internal/runner/terminal_test.go \
      pkg/engine/engine.go \
      pkg/engine/headless/browser/browser.go \
      pkg/engine/headless/browser/browser_test.go \
      pkg/engine/headless/crawler/crawler.go \
      pkg/engine/headless/headless.go \
      pkg/engine/parser/parser_generic.go \
      pkg/engine/parser/parser_generic_test.go \
      pkg/types/options.go \
    && CGO_ENABLED=0 go build -buildvcs=false -trimpath -o /out/katana ./cmd/katana

FROM python:3.14-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv

COPY pyproject.toml uv.lock /app/

RUN uv sync --frozen --no-dev --no-install-project

ENV PATH="/app/.venv/bin:$PATH"

RUN .venv/bin/playwright install --with-deps chromium

COPY --from=katana-builder /out/katana /usr/local/bin/katana

ARG DIT_MODEL_URL=https://huggingface.co/datasets/happyhackingspace/dit/resolve/main/model.json
ARG DIT_MODEL_SHA256=eba238b3093ff7aa4772ce17536bc313cb955428a6aa87dae41695a2dede6e59

RUN mkdir -p /root/.dit \
    && curl -fsSL -o /root/.dit/model.json "${DIT_MODEL_URL}" \
    && echo "${DIT_MODEL_SHA256}  /root/.dit/model.json" | sha256sum -c -

COPY app /app/app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
