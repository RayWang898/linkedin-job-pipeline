"""Scoring providers: who answers the model calls (config `scoring.provider`).

  anthropic     the Anthropic API through the SDK; needs ANTHROPIC_API_KEY (the default)
  claude-code   the `claude` command (Claude Code) in print mode, on the Claude subscription it is logged in with; no API key

Kept free of engine imports so run_daily.py and check.py can check a provider without loading score.py.
"""

import json
import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

PROVIDERS = ("anthropic", "claude-code")
DEFAULT = "anthropic"
CLAUDE_TIMEOUT = 300  # seconds for one `claude -p` call


def provider(config: dict) -> str:
    """The configured provider. Raises ValueError for an unknown one."""
    name = (config.get("scoring") or {}).get("provider") or DEFAULT
    if name not in PROVIDERS:
        raise ValueError(f"config.yaml: scoring.provider must be one of {', '.join(PROVIDERS)} (got {name!r}).")
    return name


def missing(name: str) -> str | None:
    """What the provider needs and does not have, as a message to show; None when it is ready."""
    if name == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY not found. Set it in .env or export it as an environment variable."
    if name == "claude-code" and not shutil.which("claude"):
        return ("The claude command (Claude Code) was not found on PATH, and scoring.provider is claude-code. "
                "Install Claude Code and log in, or set scoring.provider: anthropic.")
    return None


def make_client(name: str):
    """The SDK client for anthropic; claude-code needs none (each call starts its own `claude` process)."""
    if name == "anthropic":
        import anthropic
        return anthropic.Anthropic()
    return None


def claude_code_call(model: str, system: list[dict], user: str, tool: dict) -> tuple[dict, SimpleNamespace]:
    """One `claude -p` call answered as structured output that fits the tool's input schema. Returns (answer, usage).

    ANTHROPIC_API_KEY is removed from the process environment: with it set, claude bills the API key instead of the
    subscription. claude runs in an empty temporary folder with no tools, settings, MCP servers or saved session, so it
    reads nothing from the workspace. Any failure raises, and the caller's retry applies.
    """
    cmd = ["claude", "-p", "--model", model, "--system-prompt", "".join(block["text"] for block in system),
           "--json-schema", json.dumps(tool["input_schema"], ensure_ascii=False), "--output-format", "json",
           "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence"]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with tempfile.TemporaryDirectory(prefix="jobsearch-claude-") as folder:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8", env=env, cwd=folder,
                              timeout=CLAUDE_TIMEOUT)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude exited with code {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}")
    if proc.returncode != 0 or data.get("is_error"):
        raise RuntimeError(f"claude: {str(data.get('result') or proc.stderr).strip()[:200]}")
    answer = data.get("structured_output")
    if answer is None:  # older claude versions put the JSON in result
        answer = json.loads(data.get("result") or "")
    if not isinstance(answer, dict):
        raise ValueError("claude returned no structured answer")
    used = data.get("usage") or {}
    usage = SimpleNamespace(**{k: used.get(k, 0) for k in
                               ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")})
    return answer, usage
