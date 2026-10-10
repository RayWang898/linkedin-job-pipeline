"""Workspace resolution. Import this module before anything that reads a relative path.

The workspace is the user's data folder (config.yaml, profiles/, resumes/, data/, jobs.db, .env, logs/),
kept separate from the engine. It is resolved in this order:
  1. --workspace PATH on the command line
  2. the JOBSEARCH_HOME environment variable
  3. the current directory

Importing this module changes the working directory to the workspace and loads its .env, so the
cwd-relative paths used everywhere else (data/..., config.yaml, jobs.db) point into the workspace.
The choice is exported as JOBSEARCH_HOME, so child processes (run_daily -> fetch, score, ...) inherit it.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _pop_workspace_arg(argv: list[str]) -> str | None:
    """Remove --workspace (both '--workspace X' and '--workspace=X') from argv so argparse never sees it."""
    for i, arg in enumerate(argv):
        if arg == "--workspace":
            if i + 1 >= len(argv):
                sys.exit("--workspace needs a path.")
            value = argv[i + 1]
            del argv[i:i + 2]
            return value
        if arg.startswith("--workspace="):
            del argv[i]
            return arg.split("=", 1)[1]
    return None


def enter() -> Path:
    raw = _pop_workspace_arg(sys.argv) or os.environ.get("JOBSEARCH_HOME") or os.getcwd()
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        sys.exit(f"Workspace folder not found: {path}")
    os.chdir(path)
    os.environ["JOBSEARCH_HOME"] = str(path)
    load_dotenv(path / ".env")
    return path


WORKSPACE = enter()
