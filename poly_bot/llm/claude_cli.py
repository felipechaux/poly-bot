"""
Headless Claude Code CLI (`claude -p`) — uses the Claude subscription, never an API key.

Auth: on a server, set CLAUDE_CODE_OAUTH_TOKEN (generated on the Mac with `claude setup-token`).
ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are always stripped from the child environment so a
stray key can never turn this into pay-per-use API billing.

Each call is a single, tool-less session: no built-in tools, no MCP servers, no settings
files, no session persistence. Output is forced to a JSON schema (`--json-schema`).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from poly_bot.observability.logging import get_logger

log = get_logger(__name__)

STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


class ClaudeError(RuntimeError):
    pass


@dataclass
class ClaudeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd_equiv: float = 0.0  # what the same call would cost on the API (not billed)
    duration_ms: int = 0
    num_turns: int = 0
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_creation_tokens + self.cache_read_tokens

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "total_tokens": self.total_tokens}


@dataclass
class ClaudeResult:
    output: dict[str, Any]
    usage: ClaudeUsage = field(default_factory=ClaudeUsage)


def parse_cli_output(raw: str) -> ClaudeResult:
    """Parse `claude -p --output-format json` stdout."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"Invalid JSON from claude CLI: {raw[:200]!r}") from exc
    u = data.get("usage") or {}
    usage = ClaudeUsage(
        input_tokens=int(u.get("input_tokens") or 0),
        output_tokens=int(u.get("output_tokens") or 0),
        cache_creation_tokens=int(u.get("cache_creation_input_tokens") or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
        cost_usd_equiv=float(data.get("total_cost_usd") or 0.0),
        duration_ms=int(data.get("duration_ms") or 0),
        num_turns=int(data.get("num_turns") or 0),
        model=",".join((data.get("modelUsage") or {}).keys()),
    )
    if data.get("is_error"):
        raise ClaudeError(f"claude CLI error: {str(data.get('result'))[:300]}")
    output = data.get("structured_output")
    if output is None:  # fall back to parsing the text result
        try:
            output = json.loads(data.get("result") or "")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ClaudeError("claude CLI returned no structured output") from exc
    if not isinstance(output, dict):
        raise ClaudeError("structured output is not an object")
    return ClaudeResult(output=output, usage=usage)


class ClaudeCLI:
    def __init__(
        self,
        model: str = "sonnet",
        timeout_seconds: float = 300.0,
        max_budget_usd: float | None = 1.0,
        binary: str | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout_seconds
        self.max_budget_usd = max_budget_usd
        self.binary = binary or os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"

    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None or os.path.isfile(self.binary)

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
        env.setdefault("DISABLE_AUTOUPDATER", "1")
        return env

    def build_args(self, schema: dict[str, Any], system_prompt: str) -> list[str]:
        args = [
            self.binary, "-p",
            "--output-format", "json",
            "--model", self.model,
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
            "--system-prompt", system_prompt,
            "--json-schema", json.dumps(schema),
        ]
        if self.max_budget_usd:
            args += ["--max-budget-usd", str(self.max_budget_usd)]
        return args

    async def run(self, prompt: str, schema: dict[str, Any], system_prompt: str) -> ClaudeResult:
        if not self.available:
            raise ClaudeError(f"claude CLI not found ({self.binary})")
        proc = await asyncio.create_subprocess_exec(
            *self.build_args(schema, system_prompt),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(prompt.encode()), self.timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ClaudeError(f"claude CLI timed out after {self.timeout:.0f}s") from exc
        if proc.returncode != 0 and not stdout:
            raise ClaudeError(f"claude CLI exit {proc.returncode}: {stderr.decode()[-300:]}")
        result = parse_cli_output(stdout.decode())
        log.info("claude.call", model=result.usage.model, tokens=result.usage.total_tokens,
                 cost_usd_equiv=result.usage.cost_usd_equiv, duration_ms=result.usage.duration_ms)
        return result
