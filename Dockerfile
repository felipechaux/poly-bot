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

# ---- Antigravity CLI (agy; pinned URL + SHA-512 from the official release manifest) ----
# Update: curl https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json
FROM python:3.12-slim AS agy
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    base=https://storage.googleapis.com/antigravity-public/antigravity-cli/1.5.16-5250855658717184; \
    case "${TARGETARCH:-amd64}" in \
      amd64) url=$base/linux-x64/cli_linux_x64.tar.gz; \
             sum=3dee9f26e298156db673f6ce2a0f5dfed5b30006e04ceb9809c68070e7b717587b33a3da0f8bc2eda45af81a50bc8b14c82b28ae5832bcfe3e7fd49397117b31 ;; \
      arm64) url=$base/linux-arm/cli_linux_arm64.tar.gz; \
             sum=23c193151499a4986fd5fa0d8132665cc8ffbad6cc0b3ff4fd53a2cb68abb76b149a992b836d53063443cd89add335f14d8134fb02abdcecbf6e1eaef6c53d33 ;; \
      *) exit 1 ;; \
    esac; \
    curl -fsSL -o /agy.tar.gz "$url"; \
    echo "$sum  /agy.tar.gz" | sha512sum -c -; \
    tar -xzf /agy.tar.gz -C / antigravity; \
    mv /antigravity /agy; chmod 755 /agy

# ---- Runtime stage ----
FROM python:3.12-slim AS runtime
WORKDIR /app
RUN useradd -m -u 1000 botuser
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/poly_bot /app/poly_bot
COPY --from=claude /claude /usr/local/bin/claude
COPY --from=agy /agy /usr/local/bin/agy
COPY config/ ./config/
RUN mkdir -p /app/data /home/botuser/.cache/py-yfinance /home/botuser/.claude /home/botuser/.gemini \
 && chown -R botuser:botuser /app /home/botuser
USER botuser

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    LOG_FORMAT=json \
    TZ=America/Bogota \
    POLY_MODE=paper \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_BIN=/usr/local/bin/claude \
    AGY_BIN=/usr/local/bin/agy

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=4)"

ENTRYPOINT ["poly-bot"]
CMD ["run", "--port", "8080"]
