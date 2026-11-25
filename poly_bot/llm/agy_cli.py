"""
Headless Antigravity CLI (`agy -p`) — uses the Google account login, never an API key.

Auth: log in once interactively (`agy` → sign in); the session is cached under ~/.gemini.
GEMINI_API_KEY / GOOGLE_API_KEY are stripped from the child environment so a stray key can never
turn this into pay-per-use API billing.

Unlike `claude -p`, agy cannot run without tools, so every call:
  - runs in a fresh empty working directory with a minimal environment (no bot secrets),
  - uses plan mode + `--sandbox`,
  - passes per-run settings (`--settings`, never saved) that deny every permissioned action
    (file read/write, shell, URLs, browser, MCP) and set `toolPermission: strict`.
A denied tool call fails inside the run and the model answers from the prompt alone.
agy has no system-prompt flag, so the system prompt is prepended to the user prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from typing import Any

from poly_bot.llm.claude_cli import ClaudeError, ClaudeResult, ClaudeUsage
from poly_bot.observability.logging import get_logger

log = get_logger(__name__)

# Only these variables reach the agy process (HOME locates the cached login).
PASSED_ENV = ("HOME", "PATH", "LANG", "LC_ALL", "TZ", "USER", "TMPDIR", "SSL_CERT_FILE")

DENY_ALL_SETTINGS: dict[str, Any] = {
    "agentMode": "plan",
    "toolPermission": "strict",
    "allowNonWorkspaceAccess": False,
    "permissions": {
        "allow": [],
        "ask": [],
        "deny": ["read_file(*)", "write_file(*)", "command(*)", "read_url(*)",
                 "execute_url(*)", "download_url(*)", "mcp(*)"],
    },
}


def parse_agy_output(raw: str) -> ClaudeResult:
    """Parse `agy -p --output-format json` stdout into the shared result type."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"Invalid JSON from agy CLI: {raw[:200]!r}") from exc
    if data.get("status") != "SUCCESS":
        raise ClaudeError(f"agy CLI status {data.get('status')}: {str(data.get('response') or data)[:300]}")
    u = data.get("usage") or {}
    cache_read = int(u.get("cache_read_tokens") or 0)
    usage = ClaudeUsage(
        # agy reports input_tokens including cached ones; keep the split comparable with Claude
        input_tokens=max(0, int(u.get("input_tokens") or 0) - cache_read),
        output_tokens=int(u.get("output_tokens") or 0),
        cache_read_tokens=cache_read,
        duration_ms=int(float(data.get("duration_seconds") or 0) * 1000),
        num_turns=int(data.get("num_turns") or 0),
    )
    output = data.get("structured_output")
    if output is None:
        try:
            output = json.loads(data.get("response") or "")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ClaudeError("agy CLI returned no structured output") from exc
    if not isinstance(output, dict):
        raise ClaudeError("structured output is not an object")
    return ClaudeResult(output=output, usage=usage)


class AgyCLI:
    def __init__(self, model: str = "gemini-4-neon-high", timeout_seconds: float = 300.0,
                 binary: str | None = None) -> None:
        self.model = model
        self.timeout = timeout_seconds
        self.binary = binary or os.environ.get("AGY_BIN") or shutil.which("agy") or "agy"

    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None or os.path.isfile(self.binary)

    def _env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k in PASSED_ENV}

    def build_args(self, prompt: str, schema: dict[str, Any]) -> list[str]:
        return [
            self.binary, "-p", prompt,
            "--model", self.model,
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--mode", "plan",
            "--sandbox",
            "--settings", json.dumps(DENY_ALL_SETTINGS),
            "--print-timeout", f"{int(self.timeout)}s",
        ]

    async def run(self, prompt: str, schema: dict[str, Any], system_prompt: str) -> ClaudeResult:
        if not self.available:
            raise ClaudeError(f"agy CLI not found ({self.binary})")
        full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"
        with tempfile.TemporaryDirectory(prefix="agy-") as cwd:
            proc = await asyncio.create_subprocess_exec(
                *self.build_args(full_prompt, schema),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
                cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), self.timeout + 30)
            except asyncio.TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise ClaudeError(f"agy CLI timed out after {self.timeout:.0f}s") from exc
        if proc.returncode != 0 and not stdout.strip():
            raise ClaudeError(f"agy CLI exit {proc.returncode}: {stderr.decode()[-300:]}")
        result = parse_agy_output(stdout.decode())
        result.usage.model = self.model
        log.info("agy.call", model=self.model, tokens=result.usage.total_tokens,
                 duration_ms=result.usage.duration_ms)
        return result
