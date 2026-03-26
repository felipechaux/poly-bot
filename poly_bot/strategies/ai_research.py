"""
AI Research Strategy
--------------------
Uses Groq (Llama 3.3 70B, free tier) + DuckDuckGo search (no key needed)
to estimate the TRUE probability of each market resolving YES, then trades
when the estimate differs significantly from the current market price.

Flow (per market, every research_interval_min):
  1. Search DuckDuckGo for recent news about the market question (free, no key)
  2. Pass the top results to Llama 3.3 70B via Groq API
  3. Llama returns a structured estimate: {probability_yes, confidence, reasoning, key_factors}
  4. Compare vs market price → compute "edge"
  5. If edge > min_edge_pct and confidence >= min_confidence → generate signal

Free tier (Groq):
  - 14,400 requests/day on Llama 3.3 70B
  - No credit card required
  - Get key at: console.groq.com → API Keys → Create

Required env var: GROQ_API_KEY
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from groq import AsyncGroq

from poly_bot.execution.models import Fill
from poly_bot.observability.logging import get_logger
from poly_bot.strategies.base import Signal, Strategy, StrategyContext

log = get_logger(__name__)

Confidence = Literal["low", "medium", "high", "very_high"]
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2, "very_high": 3}

_SYSTEM_PROMPT = """\
You are an expert prediction market analyst. You will be given a market question
and recent web search results about it. Your job is to estimate the probability
that the event resolves YES based on the evidence provided.

Be calibrated. A 60% probability means genuinely more likely than not but far
from certain. Avoid overconfidence (90%+) unless evidence is overwhelming.

CRITICAL: Respond with ONLY a valid JSON object — no markdown, no code fences,
no explanation outside the JSON:
{
  "probability_yes": <float 0.01–0.99>,
  "confidence": "<low|medium|high|very_high>",
  "reasoning": "<1-2 sentence summary of why>",
  "key_factors": ["<factor1>", "<factor2>", "<factor3>"]
}"""


@dataclass
class ResearchResult:
    """Cached research output from Llama/Groq."""

    condition_id: str
    probability_yes: float
    confidence: Confidence
    reasoning: str
    key_factors: list[str]
    searched_at: datetime = field(default_factory=datetime.utcnow)

    def is_stale(self, ttl_minutes: float) -> bool:
        return (datetime.utcnow() - self.searched_at).total_seconds() > ttl_minutes * 60


class AIResearchStrategy(Strategy):
    """
    Research-based strategy using Groq (Llama 3.3 70B) + DuckDuckGo.

    Config params (under strategies.ai_research.params in default.yaml):
      min_edge_pct          : float = 0.10
      min_confidence        : str   = "high"
      position_size_usdc    : float = 150.0
      research_interval_min : float = 15.0
      max_markets_per_cycle : int   = 5
      min_research_liquidity: float = 5000.0
      search_results        : int   = 5
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._cache: dict[str, ResearchResult] = {}
        self._in_flight: set[str] = set()
        self._cycle_research_count = 0
        self._last_cycle_reset = datetime.utcnow()
        self._event_callback: Callable[[dict], None] | None = None

        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. "
                "Get a free key at console.groq.com → API Keys → Create."
            )
        self._client = AsyncGroq(api_key=api_key)

    def on_agent_event(self, callback: Callable[[dict], None]) -> None:
        """Register a callback to receive agent activity events."""
        self._event_callback = callback

    def _emit(self, event: dict) -> None:
        event.setdefault("occurred_at", datetime.utcnow().isoformat())
        event.setdefault("strategy", "ai_research")
        if self._event_callback:
            self._event_callback(event)

    @property
    def name(self) -> str:
        return "ai_research"

    async def on_market_update(self, ctx: StrategyContext) -> list[Signal]:
        mid = ctx.mid_price
        if mid is None:
            return []

        condition_id = ctx.market.condition_id
        min_edge: float = self._param("min_edge_pct", 0.10)
        min_conf: str = self._param("min_confidence", "high")
        size_usdc: float = self._param("position_size_usdc", 150.0)
        interval_min: float = self._param("research_interval_min", 15.0)
        max_per_cycle: int = self._param("max_markets_per_cycle", 5)

        # Reset per-cycle counter every hour
        if (datetime.utcnow() - self._last_cycle_reset).total_seconds() > 3600:
            self._cycle_research_count = 0
            self._last_cycle_reset = datetime.utcnow()

        cached = self._cache.get(condition_id)
        needs_research = cached is None or cached.is_stale(interval_min)

        if (
            needs_research
            and condition_id not in self._in_flight
            and self._cycle_research_count < max_per_cycle
            and ctx.market.liquidity >= self._param("min_research_liquidity", 5000.0)
        ):
            self._in_flight.add(condition_id)
            self._cycle_research_count += 1
            asyncio.create_task(self._research_market(ctx.market.question, condition_id))

        if not cached:
            return []

        if CONFIDENCE_ORDER.get(cached.confidence, 0) < CONFIDENCE_ORDER.get(min_conf, 2):
            self._emit({
                "event_type": "skip",
                "condition_id": condition_id,
                "question": ctx.market.question,
                "gemini_p": cached.probability_yes,
                "market_p": mid,
                "edge": cached.probability_yes - mid,
                "confidence": cached.confidence,
                "reasoning": cached.reasoning,
                "key_factors": cached.key_factors,
                "decision": "SKIP_CONF",
            })
            return []

        yes_token = ctx.market.yes_token
        no_token = ctx.market.no_token
        if not yes_token:
            return []

        ai_p = cached.probability_yes
        edge = ai_p - mid
        signals: list[Signal] = []
        position = ctx.position

        if edge > min_edge and position is None:
            ask = ctx.best_ask or mid
            signals.append(Signal(
                token_id=yes_token.token_id,
                side="BUY",
                price=ask,
                size_usdc=size_usdc,
                rationale=(
                    f"AI: P(YES)={ai_p:.1%} vs market {mid:.1%} "
                    f"→ edge={edge:+.1%} [{cached.confidence}] | {cached.reasoning[:80]}"
                ),
            ))
            self._emit({
                "event_type": "signal",
                "condition_id": condition_id,
                "question": ctx.market.question,
                "gemini_p": ai_p,
                "market_p": mid,
                "edge": edge,
                "confidence": cached.confidence,
                "reasoning": cached.reasoning,
                "key_factors": cached.key_factors,
                "decision": "BUY_YES",
            })

        elif edge < -min_edge and no_token and position is None:
            no_price = 1.0 - mid
            signals.append(Signal(
                token_id=no_token.token_id,
                side="BUY",
                price=no_price,
                size_usdc=size_usdc,
                rationale=(
                    f"AI: P(YES)={ai_p:.1%} vs market {mid:.1%} "
                    f"→ edge={edge:+.1%} [{cached.confidence}] | {cached.reasoning[:80]}"
                ),
            ))
            self._emit({
                "event_type": "signal",
                "condition_id": condition_id,
                "question": ctx.market.question,
                "gemini_p": ai_p,
                "market_p": mid,
                "edge": edge,
                "confidence": cached.confidence,
                "reasoning": cached.reasoning,
                "key_factors": cached.key_factors,
                "decision": "BUY_NO",
            })

        elif position is not None:
            from datetime import datetime, timezone
            stop_loss_pct: float = self._param("stop_loss_pct", 0.30)
            max_hold_days: float = self._param("max_hold_days", 7)
            bid = ctx.best_bid or mid
            current_value = bid * (position.total_cost_usdc / position.avg_cost_basis) if position.avg_cost_basis > 0 else 0.0
            loss_pct = (position.total_cost_usdc - current_value) / position.total_cost_usdc if position.total_cost_usdc > 0 else 0.0
            now = datetime.now(timezone.utc)
            opened = position.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            hold_days = (now - opened).total_seconds() / 86400

            exit_reason = None
            if loss_pct >= stop_loss_pct:
                exit_reason = f"Stop-loss: down {loss_pct:.0%}"
            elif hold_days >= max_hold_days:
                exit_reason = f"Max hold: {hold_days:.1f}d"
            elif position.token_id == yes_token.token_id and edge < min_edge / 2:
                exit_reason = f"Edge closed: P={ai_p:.1%}, market={mid:.1%}"

            if exit_reason:
                signals.append(Signal(
                    token_id=position.token_id,
                    side="SELL",
                    price=bid,
                    size_usdc=position.total_cost_usdc,
                    rationale=f"AI EXIT — {exit_reason}",
                ))
                self._emit({
                    "event_type": "signal",
                    "condition_id": condition_id,
                    "question": ctx.market.question,
                    "gemini_p": ai_p,
                    "market_p": mid,
                    "edge": edge,
                    "confidence": cached.confidence,
                    "reasoning": cached.reasoning,
                    "key_factors": cached.key_factors,
                    "decision": "EXIT",
                })

        else:
            self._emit({
                "event_type": "skip",
                "condition_id": condition_id,
                "question": ctx.market.question,
                "gemini_p": ai_p,
                "market_p": mid,
                "edge": edge,
                "confidence": cached.confidence,
                "reasoning": cached.reasoning,
                "key_factors": cached.key_factors,
                "decision": "SKIP_EDGE",
            })

        if signals:
            log.info(
                "ai_research.signal",
                condition_id=condition_id[:16],
                ai_p=f"{ai_p:.1%}",
                market_p=f"{mid:.1%}",
                edge=f"{edge:+.1%}",
                confidence=cached.confidence,
                signals=len(signals),
            )

        return signals

    async def _research_market(self, question: str, condition_id: str) -> None:
        try:
            log.info("ai_research.researching", question=question[:60], condition_id=condition_id[:16])
            result = await self._call_llm(question)
            self._cache[condition_id] = ResearchResult(
                condition_id=condition_id,
                probability_yes=result["probability_yes"],
                confidence=result["confidence"],
                reasoning=result["reasoning"],
                key_factors=result.get("key_factors", []),
            )
            self._emit({
                "event_type": "research",
                "condition_id": condition_id,
                "question": question,
                "gemini_p": result["probability_yes"],
                "market_p": None,
                "edge": None,
                "confidence": result["confidence"],
                "reasoning": result["reasoning"],
                "key_factors": result.get("key_factors", []),
                "decision": None,
            })
            log.info(
                "ai_research.result",
                question=question[:60],
                probability_yes=f"{result['probability_yes']:.1%}",
                confidence=result["confidence"],
                reasoning=result["reasoning"][:100],
            )
        except Exception as exc:
            log.error("ai_research.research_failed", question=question[:60], error=str(exc))
        finally:
            self._in_flight.discard(condition_id)

    async def _search_web(self, question: str) -> str:
        """Search DuckDuckGo for recent news — no API key needed."""
        try:
            from ddgs import DDGS
            max_results = self._param("search_results", 5)
            results = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: list(DDGS().text(question, max_results=max_results)),
            )
            if not results:
                return "No search results found."
            snippets = []
            for r in results:
                title = r.get("title", "")
                body = r.get("body", "")
                snippets.append(f"• {title}: {body}")
            return "\n".join(snippets)
        except Exception as exc:
            log.warning("ai_research.search_failed", error=str(exc))
            return "Web search unavailable."

    async def _call_llm(self, question: str) -> dict[str, Any]:
        """Search DuckDuckGo then ask Llama 3.3 70B via Groq to estimate probability."""
        search_results = await self._search_web(question)

        user_message = (
            f"Prediction market question: \"{question}\"\n\n"
            f"Recent web search results:\n{search_results}\n\n"
            f"Based on this information, estimate the probability this resolves YES. "
            f"Respond with ONLY the JSON object."
        )

        response = await self._client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
            max_tokens=512,
        )

        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise ValueError("Groq returned empty response")

        # Strip markdown code fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        parsed = json.loads(text)
        prob = float(parsed["probability_yes"])
        parsed["probability_yes"] = max(0.01, min(0.99, prob))

        if parsed.get("confidence") not in ("low", "medium", "high", "very_high"):
            parsed["confidence"] = "medium"

        return parsed

    async def on_fill(self, fill: Fill, ctx: StrategyContext) -> list[Signal]:
        return []

    async def on_stop(self) -> None:
        await self._client.close()
