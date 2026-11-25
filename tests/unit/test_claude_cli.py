"""claude -p wrapper: output parsing, safe env, tool-less args."""

from __future__ import annotations

import json

import pytest

from poly_bot.llm.claude_cli import ClaudeCLI, ClaudeError, parse_cli_output

SAMPLE = {
    "is_error": False, "result": '{"a":1}', "structured_output": {"a": 1},
    "total_cost_usd": 0.0144, "duration_ms": 1120, "num_turns": 2,
    "usage": {"input_tokens": 2, "output_tokens": 104, "cache_creation_input_tokens": 3343,
              "cache_read_input_tokens": 0},
    "modelUsage": {"claude-sonnet-5-5": {}},
}


def test_parse_structured_output_and_usage():
    r = parse_cli_output(json.dumps(SAMPLE))
    assert r.output == {"a": 1}
    assert r.usage.total_tokens == 2 + 104 + 3343
    assert r.usage.cost_usd_equiv == pytest.approx(0.0144)
    assert r.usage.model == "claude-sonnet-5-5"


def test_parse_falls_back_to_result_text():
    r = parse_cli_output(json.dumps({**SAMPLE, "structured_output": None}))
    assert r.output == {"a": 1}


def test_parse_error_raises():
    with pytest.raises(ClaudeError, match="Credit balance"):
        parse_cli_output(json.dumps({**SAMPLE, "is_error": True, "result": "Credit balance is too low"}))
    with pytest.raises(ClaudeError):
        parse_cli_output("not json")


def test_env_never_contains_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    env = ClaudeCLI()._env()
    assert "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"


def test_args_are_toolless_and_schema_bound():
    args = ClaudeCLI(model="sonnet", max_budget_usd=0.5, binary="claude").build_args({"type": "object"}, "sys")
    assert args[:2] == ["claude", "-p"]
    i = args.index("--tools")
    assert args[i + 1] == ""
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--json-schema", "--system-prompt"):
        assert flag in args
    assert args[args.index("--max-budget-usd") + 1] == "0.5"
