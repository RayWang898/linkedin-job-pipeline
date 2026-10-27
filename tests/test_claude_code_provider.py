"""The claude-code scoring provider (config `scoring.provider: "claude-code"`): every model call runs `claude -p` with the tool's
input schema as structured output, so scoring uses the Claude subscription the `claude` command is logged in with instead of
an Anthropic API key. The default provider stays "anthropic", and with it nothing changes.

Requirements from the maintainer: run the daily pipeline without API credit; ANTHROPIC_API_KEY must never reach the `claude`
process (with it set, claude bills the API key instead of the subscription); claude runs without tools, settings, MCP servers
or a saved session, outside the workspace; failures follow the same retry-once-then-skip path as the API.
"""

import json
import os

import pytest

DAY = "2026-10-09"
NO_KEY = {"ANTHROPIC_API_KEY": ""}


@pytest.fixture
def cc(ws):
    ws.update_config({"scoring": {"provider": "claude-code"}})
    return ws


def option(argv, name):
    return argv[argv.index(name) + 1]


# ---- a day runs on the subscription ----
def test_a_daily_run_scores_through_claude_without_an_api_key(cc):
    proc = cc.daily(env=NO_KEY)
    assert proc.returncode == 0, proc.all_output
    assert cc.runs()[DAY][0] == "success"
    assert cc.shortlist(DAY), "the fake model shortlists Coordinator jobs, as with the API"
    assert cc.calls("claude-code", tool="submit_triage")
    assert cc.calls("claude-code", tool="submit_detail") and cc.calls("claude-code", tool="submit_summary")
    assert cc.calls("anthropic") == []


def test_the_api_key_never_reaches_the_claude_process(cc):
    proc = cc.daily()  # the test environment sets ANTHROPIC_API_KEY, as a .env with a key would
    assert proc.returncode == 0, proc.all_output
    calls = cc.calls("claude-code")
    assert calls and not any(c["api_key_seen"] for c in calls)


def test_claude_gets_the_schema_and_the_prompts_and_runs_bare_outside_the_workspace(cc):
    cc.daily(env=NO_KEY)
    call = cc.calls("claude-code", tool="submit_triage")[0]
    argv = call["argv"]
    assert argv[0] == "-p"
    assert option(argv, "--output-format") == "json"
    assert option(argv, "--model") == cc.read_config()["scoring"]["triage_model"]
    assert set(json.loads(option(argv, "--json-schema"))["properties"]["scores"]["properties"]) == {"A", "B"}
    assert option(argv, "--tools") == ""
    assert option(argv, "--setting-sources") == ""
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert "<profiles>" in call["system"]          # the system prompt goes in --system-prompt
    assert "<job>" in call["user"]                 # the job goes in on stdin
    assert not os.path.realpath(call["cwd"]).startswith(os.path.realpath(cc.path))


def test_token_usage_from_claude_is_counted_in_the_summary(cc):
    cc.daily(env=NO_KEY)
    n = len(cc.calls("claude-code", tool="submit_triage"))
    summary = cc.read(f"data/7_shortlist/{DAY}/_summary.md")
    assert f"{n} calls" in summary and f"input {100 * n}" in summary


# ---- failures take the API's path ----
def test_a_malformed_answer_is_retried_once(cc):
    proc = cc.daily(env={**NO_KEY, "JOBSEARCH_TEST_CLAUDE_BAD_OUTPUT": "1"})
    assert proc.returncode == 0, proc.all_output
    triage = cc.calls("claude-code", tool="submit_triage")
    first_job = triage[0]["user"]
    assert sum(c["user"] == first_job for c in triage) == 2
    assert cc.shortlist(DAY)


def test_a_failing_claude_skips_jobs_instead_of_crashing(cc):
    proc = cc.daily(env={**NO_KEY, "JOBSEARCH_TEST_MODEL_ERRORS": "1"})
    assert proc.returncode == 0
    assert "Skipping" in proc.all_output
    assert cc.shortlist(DAY) == []
    assert cc.runs()[DAY][0] == "success"


# ---- setup errors say what to fix ----
def test_without_the_claude_command_the_day_fails_and_names_it(cc):
    proc = cc.daily(env={**NO_KEY, "PATH": os.path.dirname(os.path.realpath(os.__file__))})
    assert cc.runs()[DAY][0] == "failed"
    assert "claude" in proc.all_output and "ANTHROPIC_API_KEY" not in proc.all_output


@pytest.mark.parametrize("script,args", [
    ("score.py", []),
    ("add_to_shortlist.py", ["--file", "missing.txt"]),       # stops at the missing file, after the key check
    ("replay_triage.py", ["--file", "missing.json"]),
    ("backfill.py", ["--days", "1", "--run-date", "20261009"]),
])
def test_no_script_asks_for_the_api_key_with_claude_code(cc, script, args):
    proc = cc.run(script, *args, env=NO_KEY, today=DAY)
    assert "ANTHROPIC_API_KEY not found" not in proc.all_output, proc.all_output


def test_dry_run_checks_the_claude_command_instead_of_the_api_key(cc):
    proc = cc.run("run_daily.py", "--dry-run", "--json", env=NO_KEY, today=DAY)
    rep = json.loads(proc.stdout)
    names = {c["name"]: c["status"] for c in rep["checks"]}
    assert "env ANTHROPIC_API_KEY" not in names
    assert names.get("claude command") == "ok"


def test_dry_run_reports_a_missing_claude_command(cc):
    path = os.path.dirname(os.path.realpath(os.__file__))
    proc = cc.run("run_daily.py", "--dry-run", "--json", env={**NO_KEY, "PATH": path}, today=DAY)
    rep = json.loads(proc.stdout)
    assert {c["name"]: c["status"] for c in rep["checks"]}.get("claude command") == "error"
    assert proc.returncode == 1


def test_an_unknown_provider_is_a_config_error(ws):
    ws.update_config({"scoring": {"provider": "openai"}})
    proc = ws.run("score.py", env=NO_KEY)
    assert proc.returncode != 0
    assert "scoring.provider" in proc.all_output and "claude-code" in proc.all_output


# ---- the default is untouched ----
def test_the_default_provider_still_uses_the_api_and_never_runs_claude(ws):
    proc = ws.daily()
    assert proc.returncode == 0, proc.all_output
    assert ws.calls("anthropic") and ws.calls("claude-code") == []


def test_the_default_provider_still_requires_the_api_key(ws):
    proc = ws.daily(env=NO_KEY)
    assert ws.runs()[DAY][0] == "failed"
    assert "ANTHROPIC_API_KEY" in proc.all_output
