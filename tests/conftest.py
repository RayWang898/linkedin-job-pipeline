"""Shared test tools.

The engine finds its workspace and reads config.yaml at import time, so tests that import engine modules (filter, dedup) share one
boot workspace created below, and everything that needs another configuration runs the scripts as subprocesses, the way a user does.
Subprocesses get tests/fakes on PYTHONPATH, which swaps the Anthropic SDK, the Apify client, requests, time.sleep and (optionally)
today's date. No test uses the network.
"""

import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ENGINE = Path(__file__).resolve().parents[1]
SCRIPTS = ENGINE / "scripts" / "execution-python"
FAKES = ENGINE / "tests" / "fakes"
FIXTURES = ENGINE / "tests" / "fixtures"
JOBS_FIXTURE = FIXTURES / "jobs.json"

# environment variables that would leak the developer's own setup into a test
_SCRUB = ("JOBSEARCH_HOME", "APIFY_TOKEN", "ANTHROPIC_API_KEY", "NTFY_TOPIC", "NTFY_SERVER")


def _clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB and not k.startswith("JOBSEARCH_TEST_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


def run_init(target: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "init.py"), str(target), *args], capture_output=True, text=True,
                          encoding="utf-8", env=_clean_env())


# ---- boot workspace for in-process imports of engine modules ----
_BOOT = Path(tempfile.mkdtemp(prefix="jobsearch-boot-"))
run_init(_BOOT)
os.environ["JOBSEARCH_HOME"] = str(_BOOT)
sys.path.insert(0, str(SCRIPTS))


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def seed_profiles(target: Path) -> None:
    """init.py leaves profiles/ empty; most tests need the template profiles the example tracks point at."""
    shutil.copytree(ENGINE / "profiles.example", target / "profiles", dirs_exist_ok=True)


class Workspace:
    """A throw-away workspace made by init.py (so init.py is exercised by every test that uses it)."""

    def __init__(self, path: Path):
        self.path = path

    # --- files ---
    def read_config(self) -> dict:
        return yaml.safe_load((self.path / "config.yaml").read_text(encoding="utf-8"))

    def update_config(self, patch: dict) -> dict:
        config = deep_merge(self.read_config(), patch)
        (self.path / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return config

    def write(self, relative: str, text: str) -> Path:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def read(self, relative: str) -> str:
        return (self.path / relative).read_text(encoding="utf-8")

    def exists(self, relative: str) -> bool:
        return (self.path / relative).exists()

    def use_tracker(self) -> None:
        shutil.copy(FIXTURES / "tracker.txt", self.path / "tracker.txt")
        self.update_config({"tracker_path": "tracker.txt"})

    def use_jobs(self, jobs: list[dict]) -> Path:
        path = self.write("fake_jobs.json", json.dumps(jobs))
        self.jobs_file = path
        return path

    jobs_file: Path | None = None

    # --- running ---
    def env(self, today: str | None = None, extra: dict | None = None) -> dict:
        env = _clean_env({
            "PYTHONPATH": str(FAKES), "JOBSEARCH_TEST_FAKES": "1",
            "PATH": f"{FAKES / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",  # the fake `claude` command shadows a real one
            "JOBSEARCH_TEST_LOG": str(self.path / "fake_calls.jsonl"),
            "JOBSEARCH_TEST_JOBS": str(self.jobs_file or JOBS_FIXTURE),
            "JOBSEARCH_TEST_STATE": str(self.path),
            "APIFY_TOKEN": "test-apify-token", "ANTHROPIC_API_KEY": "test-anthropic-key",
        })
        if today:
            env["JOBSEARCH_TEST_TODAY"] = today
        env.update(extra or {})
        return env

    def run(self, script: str, *args: str, today: str | None = None, env: dict | None = None,
            workspace_arg: bool = True) -> subprocess.CompletedProcess:
        cmd = [sys.executable, str(SCRIPTS / script)]
        if workspace_arg:
            cmd += ["--workspace", str(self.path)]
        proc = subprocess.run(cmd + list(args), capture_output=True, text=True, encoding="utf-8",
                              env=self.env(today, env), cwd=str(self.path.parent))
        proc.all_output = proc.stdout + proc.stderr  # type: ignore[attr-defined]
        return proc

    def daily(self, *args: str, today: str = "2026-10-09", env: dict | None = None) -> subprocess.CompletedProcess:
        return self.run("run_daily.py", "--no-notify", *args, today=today, env=env)

    # --- observing ---
    def calls(self, kind: str | None = None, tool: str | None = None) -> list[dict]:
        path = self.path / "fake_calls.jsonl"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        return [e for e in entries if (kind is None or e["kind"] == kind) and (tool is None or e.get("tool") == tool)]

    def db(self, name: str = "jobs.db") -> sqlite3.Connection:
        return sqlite3.connect(self.path / name)

    def runs(self) -> dict:
        """daily_runs as {run_date: (status, steps_done list)}."""
        rows = self.db().execute("SELECT run_date, status, steps_done FROM daily_runs ORDER BY run_date").fetchall()
        return {d: (s, json.loads(steps) if steps else []) for d, s, steps in rows}

    def shortlist(self, day: str) -> list[str]:
        folder = self.path / "data" / "7_shortlist" / day
        return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.exists() else []

    def note(self, day: str, folder: str) -> str:
        return (self.path / "data" / "7_shortlist" / day / folder / f"{folder}.md").read_text(encoding="utf-8")


@pytest.fixture
def ws(tmp_path) -> Workspace:
    target = tmp_path / "ws"
    proc = run_init(target)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    seed_profiles(target)
    workspace = Workspace(target)
    # the template profiles are fine for the engine; give keys a harmless default so --dry-run style checks are not noise
    return workspace


@pytest.fixture
def make_ws(tmp_path):
    """Another independent workspace in the same test (for comparing a clean run with a resumed one)."""
    def make(name: str = "ws2", language: str | None = None) -> Workspace:
        target = tmp_path / name
        assert run_init(target, *(["--language", language] if language else [])).returncode == 0
        seed_profiles(target)
        return Workspace(target)
    return make


@pytest.fixture
def jobs() -> list[dict]:
    return json.loads(JOBS_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def job_by_id(jobs):
    return {j["jobId"]: j for j in jobs}
