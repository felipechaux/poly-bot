# Poly Bot

A Polymarket trading bot with AI-powered research, paper trading simulation, and a real-time web dashboard.

**Paper trading by default** — simulates trades against live order books with zero real money at risk.

---

## Features

- **AI Research Strategy** — Llama 3.3 70B (via Groq, free) + DuckDuckGo search estimates true probabilities and trades when it finds edge vs the market price
- **Mean Reversion Strategy** — buys extreme markets (< 7% or > 93%) expecting reversion
- **Paper trading engine** — fills simulated against real live order books, resting order support
- **Real-time dashboard** — equity curve, agent activity log, strategy P&L breakdown, open positions
- **Risk manager** — position size limits, exposure caps, price sanity checks, order debounce
- **SQLite persistence** — all fills, portfolio snapshots, and agent decisions saved to disk
- **Pluggable strategies** — add your own by implementing the `Strategy` ABC

---

## Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| Package manager | [uv](https://github.com/astral-sh/uv) |
| Market data | Polymarket CLOB API + Gamma API |
| AI / LLM | [Groq](https://console.groq.com) — Llama 3.3 70B (free tier) |
| Web search | DuckDuckGo (no API key needed) |
| Web dashboard | FastAPI + WebSocket + Chart.js |
| Database | SQLite via aiosqlite |
| Config | YAML + pydantic-settings |
| Logging | structlog |

---

## Quick Start

### 1. Install dependencies

```bash
pip install uv
uv sync
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and add your Groq API key (free at [console.groq.com](https://console.groq.com)):

```env
GROQ_API_KEY=gsk_...
```

### 3. Enable strategies

Edit `config/default.yaml`:

```yaml
strategies:
  mean_reversion:
    enabled: true   # on by default

  ai_research:
    enabled: true   # requires GROQ_API_KEY
```

### 4. Run

```bash
make run
```

Open **http://localhost:8080** for the dashboard.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GROQ_API_KEY` | For AI strategy | Free at [console.groq.com](https://console.groq.com) |
| `POLY_MODE` | No | `paper` (default) or `live` |
| `POLY_PAPER_INITIAL_BALANCE` | No | Starting paper balance (default: `5000.0`) |
| `ENABLE_LIVE_TRADING` | Live only | Must be `true` to enable real trades |
| `POLY_PRIVATE_KEY` | Live only | Ethereum private key for on-chain orders |

---

## Configuration

All non-secret settings live in `config/default.yaml`:

```yaml
strategies:
  ai_research:
    enabled: true
    params:
      min_edge_pct: 0.10           # 10% edge required to trade
      min_confidence: "high"       # low | medium | high | very_high
      position_size_usdc: 150.0    # USDC per trade
      research_interval_min: 15.0  # re-research each market every 15 min
      max_markets_per_cycle: 3     # max Groq calls per feed cycle

  mean_reversion:
    enabled: true
    params:
      low_threshold: 0.07          # buy YES below 7%
      high_threshold: 0.93         # buy NO above 93%
      position_size_usdc: 100.0
```

---

## Dashboard

The web dashboard at `http://localhost:8080` shows:

- **Equity curve** — portfolio value over time loaded from DB history
- **KPIs** — total value, P&L, ROI%, win rate, max drawdown, Sharpe ratio
- **Agent Activity** — every research decision the AI made: what it searched, estimated probability, market price, edge, and whether it traded or skipped
- **Strategy Performance** — per-strategy realized P&L, win rate, volume
- **Open Positions** — current holdings with cost basis
- **Recent Trades** — fill history with realized P&L per trade

---

## How the AI Strategy Works

```
Every 15 min per market:
  1. Search DuckDuckGo for latest news on the market question
  2. Llama 3.3 70B reads results → estimates P(YES)
  3. Compare vs current market price → edge = AI estimate − market price
  4. If |edge| > 10% AND confidence >= "high":
       edge > 0  → BUY YES (market underpricing YES)
       edge < 0  → BUY NO  (market overpricing YES)
  5. Exit when edge closes to < 5%
```

All decisions (research, signal, skip) are logged to the dashboard in real time.

---

## Deployment

### Local background service (macOS)

```bash
make install-service   # installs launchd plist, starts on boot
make uninstall-service # remove
```

### Docker

```bash
docker compose up -d
```

### Fly.io (free cloud)

```bash
brew install flyctl
fly auth login
fly launch --name poly-bot
fly secrets set GROQ_API_KEY=gsk_...
fly deploy
fly open
```

---

## Project Structure

```
poly_bot/
├── bot.py                  # Main orchestrator
├── config/settings.py      # Pydantic settings + YAML loader
├── market_data/            # Polymarket CLOB + Gamma API clients
├── execution/              # Paper + live order executors
├── portfolio/              # Position tracker, P&L accounting
├── risk/manager.py         # Pre-trade risk checks
├── strategies/
│   ├── base.py             # Strategy ABC
│   ├── ai_research.py      # Groq + DuckDuckGo strategy
│   ├── mean_reversion.py   # Mean reversion strategy
│   ├── momentum.py         # Momentum strategy (disabled)
│   └── registry.py         # Strategy loader
├── store/                  # SQLite persistence
└── web/                    # FastAPI dashboard + WebSocket
```

---

## Adding a Strategy

```python
from poly_bot.strategies.base import Signal, Strategy, StrategyContext

class MyStrategy(Strategy):
    @property
    def name(self) -> str:
        return "my_strategy"

    async def on_market_update(self, ctx: StrategyContext) -> list[Signal]:
        mid = ctx.mid_price
        if mid is None:
            return []
        # your logic here
        return [Signal(token_id=..., side="BUY", price=mid, size_usdc=100.0)]
```

Register in `poly_bot/strategies/registry.py` and enable in `config/default.yaml`.

---

## Tests

```bash
uv run pytest
```

22 unit tests covering order book analysis, paper executor, and risk manager.

---

## License

MIT
