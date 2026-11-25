"""
LLM analyst — one headless CLI call per day manages a small satellite sleeve.

Several instances can run side by side (e.g. `llm_claude` with `claude -p` and `llm_gemini` with
`agy -p`), each with its own sleeve, so their decisions can be compared on the same data.

Flow per daily cycle:
  1. Stop-loss on sleeve positions (no LLM needed).
  2. Today's analysis: reuse the cached one if it already ran today; skip the call on dry runs
     (so the dashboard's dry-run button never spends subscription usage); otherwise fetch
     headlines and call Claude with a strict JSON schema.
  3. Map ideas to signals inside hard limits: eligible tickers only (universe minus core),
     confidence threshold, max weight per position, sleeve cap, max positions / new entries.

Every BUY/SELL idea is journaled as a `recommendation` event so its 1/7/30-day outcome can be
scored later, executed or not.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from poly_bot.llm.agy_cli import AgyCLI
from poly_bot.llm.claude_cli import ClaudeCLI, ClaudeError, ClaudeResult
from poly_bot.llm.prompts import SYSTEM_PROMPT, build_prompt, build_schema
from poly_bot.news.rss import Headline, fetch_headlines
from poly_bot.observability.logging import get_logger
from poly_bot.strategies.base import DailyContext, Signal, Strategy

log = get_logger(__name__)

NewsFetcher = Callable[..., Awaitable[list[Headline]]]
EXIT_MIN_CONFIDENCE = 0.5  # exits are allowed on weaker conviction than entries
PROVIDER_LABELS = {"claude": "Claude", "agy": "Gemini"}


class LLMAnalystStrategy(Strategy):
    @property
    def name(self) -> str:
        return str(self._config.get("name") or "llm_analyst")

    def __init__(self, config: dict[str, Any], client: Any | None = None,
                 news_fetcher: NewsFetcher | None = None) -> None:
        super().__init__(config)
        self.p: dict[str, Any] = {
            "model": self._param("model", "sonnet"),
            "sleeve_pct": float(self._param("sleeve_pct", 0.15)),
            "max_position_pct": float(self._param("max_position_pct", 0.05)),
            "max_positions": int(self._param("max_positions", 5)),
            "max_new_per_day": int(self._param("max_new_per_day", 2)),
            "min_confidence": float(self._param("min_confidence", 0.7)),
            "stop_loss_pct": float(self._param("stop_loss_pct", 0.15)),
            "max_drawdown_pct": float(self._param("max_drawdown_pct", 0.10)),
        }
        sem = self._param("semaforo", {}) or {}
        self.semaforo_cfg: dict[str, float] = {"horizon_days": int(sem.get("horizon_days", 7)),
                             "min_n": int(sem.get("min_n", 20)),
                             "t_threshold": float(sem.get("t_threshold", 2.0))}
        self.feeds: list[dict[str, str]] = list(self._param("feeds", []) or [])
        self.news_max_items = int(self._param("news_max_items", 30))
        self.news_max_age_hours = float(self._param("news_max_age_hours", 48))
        self.provider = str(self._param("provider", "claude"))
        self.label = str(self._param("label", PROVIDER_LABELS.get(self.provider, self.provider)))
        timeout = float(self._param("timeout_seconds", 300))
        if client is not None:
            self.client = client
        elif self.provider == "claude":
            self.client = ClaudeCLI(model=self.p["model"], timeout_seconds=timeout,
                                    max_budget_usd=float(self._param("max_budget_usd", 1.0)))
        elif self.provider == "agy":
            self.client = AgyCLI(model=self.p["model"], timeout_seconds=timeout)
        else:
            raise ValueError(f"{self.name}: unknown provider '{self.provider}' (claude | agy)")
        self.news_fetcher: NewsFetcher = news_fetcher or fetch_headlines
        self.excluded: set[str] = set(self._param("exclude", []) or [])

    def eligible(self, ctx: DailyContext) -> list[str]:
        """Universe minus core targets minus tickers another strategy already holds."""
        others = {p.ticker for p in ctx.portfolio.positions if p.strategy != self.name}
        return [t for t in ctx.universe.tickers
                if t not in self.excluded and t not in others and t in ctx.quotes]

    # ------------------------------------------------------------------

    async def on_daily(self, ctx: DailyContext) -> list[Signal]:
        snap = ctx.portfolio
        sleeve = [p for p in snap.positions if p.strategy == self.name]
        signals: list[Signal] = []
        stopped: set[str] = set()

        for p in sleeve:
            if p.unrealized_pct <= -self.p["stop_loss_pct"]:
                signals.append(Signal(p.ticker, "SELL", quantity=p.quantity,
                                      rationale=f"stop-loss {p.unrealized_pct:.1%}"))
                stopped.add(p.ticker)

        analysis = await self._get_analysis(ctx, [p.ticker for p in sleeve])
        if analysis is None:
            return signals

        signals += self._ideas_to_signals(ctx, analysis, stopped)
        return signals

    async def _get_analysis(self, ctx: DailyContext, sleeve: list[str]) -> dict[str, Any] | None:
        cached = ctx.extra.get("cached_analysis")
        if cached:
            log.info("llm.cached_analysis")
            return dict(cached)
        if ctx.extra.get("dry_run"):
            ctx.notes.append(f"🤖 {self.label}: no se llama en dry run")
            return None

        eligible = self.eligible(ctx)
        headlines = await self.news_fetcher(self.feeds, max_items=self.news_max_items,
                                            max_age_hours=self.news_max_age_hours) if self.feeds else []
        prompt = build_prompt(ctx.today, ctx.portfolio, ctx.quotes, ctx.universe, eligible, sleeve,
                              headlines, self.p, track_record=str(ctx.extra.get("track_record", "")))
        schema = build_schema(eligible, self.p["max_position_pct"])
        try:
            result: ClaudeResult = await self.client.run(prompt, schema, SYSTEM_PROMPT)
        except ClaudeError as exc:
            log.error("llm.call_failed", error=str(exc))
            ctx.events.append({"event_type": "error", "strategy": self.name,
                               "reasoning": f"{self.provider}: {exc}"})
            ctx.notes.append(f"⚠️ {self.label} falló hoy: {str(exc)[:120]}")
            return None

        analysis = {**result.output, "usage": result.usage.as_dict(), "provider": self.provider,
                    "prompt_chars": len(prompt), "headlines": len(headlines)}
        ctx.events.append({"event_type": "analysis", "strategy": self.name,
                           "reasoning": str(result.output.get("resumen_mercado", ""))[:1000],
                           "payload": analysis})
        return analysis

    def _ideas_to_signals(self, ctx: DailyContext, analysis: dict[str, Any],
                          stopped: set[str]) -> list[Signal]:
        snap = ctx.portfolio
        total = snap.total_value()
        if total <= 0:
            return []
        eligible = set(self.eligible(ctx))
        held = {p.ticker for p in snap.positions if p.strategy == self.name} - stopped
        sleeve_value = sum(snap.value_of(t) for t in held)
        sleeve_cap = self.p["sleeve_pct"] * total
        n_positions = len(held)
        new_today = 0
        cached = bool(ctx.extra.get("cached_analysis"))
        guard = ctx.extra.get("guard")  # paused: exits only, no new buys

        ideas = [i for i in analysis.get("acciones", []) if isinstance(i, dict)]
        ideas.sort(key=lambda i: -float(i.get("confianza", 0)))
        signals: list[Signal] = []
        usage = analysis.get("usage", {})
        n_exec = 0

        for idea in ideas:
            t, action = str(idea.get("ticker", "")), str(idea.get("accion", "HOLD"))
            conf = float(idea.get("confianza", 0))
            weight = min(float(idea.get("peso_objetivo", 0)), self.p["max_position_pct"])
            quote = ctx.quotes.get(t)
            if action == "HOLD" or t not in eligible or quote is None:
                continue
            why = f"Claude {conf:.2f}: {str(idea.get('razon', ''))}"[:300]
            planned: Signal | None = None

            if action == "BUY" and conf >= self.p["min_confidence"] and t not in stopped and not guard:
                is_new = t not in held
                if is_new and (n_positions >= self.p["max_positions"]
                               or new_today >= self.p["max_new_per_day"]):
                    pass
                else:
                    diff_base = min(weight * total - snap.value_of(t), sleeve_cap - sleeve_value)
                    if diff_base > 0:
                        planned = Signal(t, "BUY", value=snap.to(diff_base, snap.base_currency,
                                                                 quote.currency), rationale=why)
                        sleeve_value += diff_base
                        if is_new:
                            n_positions += 1
                            new_today += 1
            elif action == "SELL" and t in held and conf >= EXIT_MIN_CONFIDENCE:
                pos = snap.get_position(t)
                if pos is not None:
                    current = snap.value_of(t)
                    keep = weight * total
                    qty = pos.quantity if keep <= 0 else pos.quantity * max(0.0, 1 - keep / current)
                    if qty > 0:
                        planned = Signal(t, "SELL", quantity=qty, rationale=why)

            if planned:
                signals.append(planned)
                n_exec += 1
            if not cached:  # journal each idea once (first run of the day)
                ctx.events.append({
                    "event_type": "recommendation", "strategy": self.name, "ticker": t,
                    "decision": action, "reasoning": str(idea.get("razon", ""))[:400],
                    "payload": {**idea, "price_at": quote.price, "currency": quote.currency,
                                "as_of": str(quote.as_of), "planned": planned is not None},
                })

        if guard:
            ctx.notes.append(f"⛔ {self.label} en pausa (solo ventas): {guard}")
        if not cached:
            tokens = int(usage.get("total_tokens", 0))
            ctx.notes.append(
                f"🤖 {self.label} ({usage.get('model') or self.p['model']}): {len(ideas)} ideas, "
                f"{n_exec} a ejecutar · {tokens / 1000:.1f}k tokens"
                + (f" (~US$ {cost:.3f} equiv., incluido en tu plan)"
                   if (cost := float(usage.get('cost_usd_equiv', 0) or 0)) else " (incluido en tu plan)"))
            resumen = str(analysis.get("resumen_mercado", "")).strip()
            if resumen:
                ctx.notes.append("   " + resumen[:220] + ("…" if len(resumen) > 220 else ""))
            for i in ideas[:4]:
                if i.get("accion") != "HOLD":
                    ctx.notes.append(f"   • {i.get('accion')} {i.get('ticker')} "
                                     f"{float(i.get('peso_objetivo', 0)):.0%} "
                                     f"(conf {float(i.get('confianza', 0)):.2f})")
        return signals
