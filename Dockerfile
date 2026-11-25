# ---- Build stage ----
FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY poly_bot/ ./poly_bot/
RUN uv sync --no-dev --frozen

# ---- Runtime stage ----
FROM python:3.12-slim AS runtime
WORKDIR /app
RUN useradd -m -u 1000 botuser
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/poly_bot /app/poly_bot
COPY config/ ./config/
RUN mkdir -p /app/data /home/botuser/.cache/py-yfinance && chown -R botuser:botuser /app /home/botuser/.cache
USER botuser

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    LOG_FORMAT=json \
    TZ=America/Bogota \
    POLY_MODE=paper

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=4)"

ENTRYPOINT ["poly-bot"]
CMD ["run", "--port", "8080"]
