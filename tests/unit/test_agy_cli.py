"""agy -p wrapper: output parsing, minimal env, deny-all tool settings."""

from __future__ import annotations

import json

import pytest

from poly_bot.llm.agy_cli import DENY_ALL_SETTINGS, AgyCLI, parse_agy_output
from poly_bot.llm.claude_cli import ClaudeError

SAMPLE = {
    "conversation_id": "x", "status": "SUCCESS", "response": '{"a": 1}\n',
    "duration_seconds": 6.5, "num_turns": 1, "structured_output": {"a": 1},
    "usage": {"input_tokens": 18291, "output_tokens": 574, "thinking_tokens": 524,
              "cache_read_tokens": 13817, "total_tokens": 18865},
}


def test_parse_success_and_usage():
    r = parse_agy_output(json.dumps(SAMPLE))
    assert r.output == {"a": 1}
    assert r.usage.total_tokens == 18865  # input split into fresh + cached, same total
    assert r.usage.cache_read_tokens == 13817 and r.usage.duration_ms == 6500
    assert r.usage.cost_usd_equiv == 0.0


def test_parse_falls_back_to_response_and_raises_on_error():
    assert parse_agy_output(json.dumps({**SAMPLE, "structured_output": None})).output == {"a": 1}
    with pytest.raises(ClaudeError, match="ERROR"):
        parse_agy_output(json.dumps({**SAMPLE, "status": "ERROR", "response": "authentication required"}))
    with pytest.raises(ClaudeError):
        parse_agy_output("nope")


def test_env_is_minimal(monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ADMIN_TOKEN", "NTFY_TOPIC", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(k, "secret")
    env = AgyCLI()._env()
    assert "HOME" in env
    assert not {"GEMINI_API_KEY", "GOOGLE_API_KEY", "ADMIN_TOKEN", "NTFY_TOPIC",
                "CLAUDE_CODE_OAUTH_TOKEN"} & set(env)


def test_args_deny_all_tools():
    args = AgyCLI(model="gemini-4-neon-high", binary="agy").build_args("hola", {"type": "object"})
    assert args[:3] == ["agy", "-p", "hola"]
    assert "--dangerously-skip-permissions" not in args
    assert "--sandbox" in args and args[args.index("--mode") + 1] == "plan"
    settings = json.loads(args[args.index("--settings") + 1])
    assert settings == DENY_ALL_SETTINGS
    for rule in ("read_file(*)", "write_file(*)", "command(*)", "read_url(*)", "mcp(*)"):
        assert rule in settings["permissions"]["deny"]
