# ---- Build stage ----
FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY poly_bot/ ./poly_bot/
RUN uv sync --no-dev --frozen

# ---- Claude Code CLI (native binary, pinned + checksum-verified) ----
FROM python:3.12-slim AS claude
ARG CLAUDE_VERSION=2.1.331
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in amd64) p=linux-x64 ;; arm64) p=linux-arm64 ;; *) exit 1 ;; esac; \
    base=https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}; \
    sum=$(curl -fsSL "$base/manifest.json" | python3 -c "import json,sys; print(json.load(sys.stdin)['platforms']['$p']['checksum'])"); \
    curl -fsSL -o /claude "$base/$p/claude"; \
    echo "$sum  /claude" | sha256sum -c -; \
    chmod 755 /claude

# ---- Runtime stage ----
FROM python:3.12-slim AS runtime
WORKDIR /app
RUN useradd -m -u 1000 botuser
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/poly_bot /app/poly_bot
COPY --from=claude /claude /usr/local/bin/claude
COPY config/ ./config/
RUN mkdir -p /app/data /home/botuser/.cache/py-yfinance /home/botuser/.claude \
 && chown -R botuser:botuser /app /home/botuser
USER botuser

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    LOG_FORMAT=json \
    TZ=America/Bogota \
    POLY_MODE=paper \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_BIN=/usr/local/bin/claude

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=4)"

ENTRYPOINT ["poly-bot"]
CMD ["run", "--port", "8080"]
