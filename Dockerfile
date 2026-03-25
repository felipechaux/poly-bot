# ---- Build stage ----
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
COPY poly_bot/ ./poly_bot/

# Install dependencies into a virtual env
RUN uv sync --no-dev --frozen

# ---- Runtime stage ----
FROM python:3.12-slim AS runtime

WORKDIR /app

# Non-root user for security
RUN useradd -m -u 1000 botuser

# Copy venv and app from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/poly_bot /app/poly_bot
COPY config/ ./config/

# Data directory for SQLite
RUN mkdir -p /app/data && chown -R botuser:botuser /app

USER botuser

# Activate venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV LOG_FORMAT=json

# Default: paper trading mode
ENV POLY_MODE=paper
ENV ENABLE_LIVE_TRADING=false

VOLUME ["/app/data"]

ENTRYPOINT ["poly-bot"]
CMD ["run"]
