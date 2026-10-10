"""
Daily run: run the six steps in order, retry on failure, and notify your phone through ntfy at the end.

It only coordinates; the logic of each step lives in fetch.py, dedup.py, filter.py and score.py.

Usage:
  venv/bin/python scripts/execution-python/run_daily.py                 # normal daily run (skipped if today already succeeded)
  venv/bin/python scripts/execution-python/run_daily.py --force         # rerun even if today already succeeded
  venv/bin/python scripts/execution-python/run_daily.py --no-notify     # no notification (for testing)
  venv/bin/python scripts/execution-python/run_daily.py --max-jobs 5 --db test.db   # testing: a few jobs and a test database
Needs the environment variables APIFY_TOKEN, ANTHROPIC_API_KEY and NTFY_TOPIC (can be set in .env).
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)
import fetch  # noqa: E402
import score  # noqa: E402
from dedup import init_db  # noqa: E402

TX, fill = score.TX, score.fill

LOG_DIR = Path("logs")
DB_PATH = Path("jobs.db")
CONFIG_PATH = Path("config.yaml")
STALE_RUNNING_HOURS = 3  # a run still marked running after this long is treated as an earlier abnormal stop

_log_file = None


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if _log_file:
        _log_file.write(line + "\n")
        _log_file.flush()


class StepFailed(Exception):
    def __init__(self, step: str, detail: str):
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


# ---------- daily_runs table ----------

def init_runs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_runs (
            run_date TEXT PRIMARY KEY,
            status TEXT NOT NULL,          -- running / success / failed
            started_at TEXT NOT NULL,
            finished_at TEXT,
            raw_count INTEGER,
            failed_queries TEXT,           -- queries given up on, comma-separated
            main_count INTEGER,
            custom_count INTEGER,
            by_track TEXT,                 -- JSON: shortlist counts grouped by best track
            tokens TEXT,                   -- JSON: total token usage of triage and detailed review
            error TEXT
        )
        """
    )
    conn.commit()


def run_status(conn: sqlite3.Connection, run_date: str) -> tuple[str, str] | None:
    return conn.execute(
        "SELECT status, started_at FROM daily_runs WHERE run_date = ?", (run_date,)
    ).fetchone()


def days_since_last_success(conn: sqlite3.Connection, run_date: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(run_date) FROM daily_runs WHERE status = 'success' AND run_date < ?", (run_date,)
    ).fetchone()
    if not row or not row[0]:
        return None
    return (date.fromisoformat(run_date) - date.fromisoformat(row[0])).days


def start_run(conn: sqlite3.Connection, run_date: str) -> None:
    conn.execute("DELETE FROM daily_runs WHERE run_date = ?", (run_date,))
    conn.execute(
        "INSERT INTO daily_runs (run_date, status, started_at) VALUES (?, 'running', ?)",
        (run_date, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_date: str, status: str, **fields) -> None:
    fields = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
    fields.update(status=status, finished_at=datetime.now().isoformat(timespec="seconds"))
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE daily_runs SET {cols} WHERE run_date = ?", [*fields.values(), run_date])
    conn.commit()


# ---------- running steps with retries ----------

def with_retries(step: str, backoff: list[int], fn):
    """If fn fails (raises), wait per backoff and retry; raise StepFailed if it still fails."""
    waits = [0] + list(backoff)
    last = None
    for attempt, wait in enumerate(waits, 1):
        if wait:
            log(f"{step}: waiting {wait}s before retry {attempt - 1}")
            time.sleep(wait)
        try:
            return fn()
        except Exception as e:
            last = e
            log(f"{step}: attempt {attempt} failed: {e}")
    raise StepFailed(step, f"still failing after {len(backoff)} retries ({last})")


def run_script(script: str, args: list[str]) -> None:
    cmd = [sys.executable, str(SCRIPTS_DIR / script), *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    for line in proc.stdout:
        log(f"  | {line.rstrip()}")
    if proc.wait() != 0:
        raise RuntimeError(f"{script} exited with code {proc.returncode}")


# ---------- calibration list ----------

def write_calibration(run_date: str) -> None:
    """Run calibrate.py and save the output to feedback/calibrate_<date>.md. A failure is only logged and does not affect the run result."""
    day = run_date.replace("-", "")
    out = Path("feedback") / f"calibrate_{day}.md"
    try:
        out.parent.mkdir(exist_ok=True)
        proc = subprocess.run([sys.executable, str(SCRIPTS_DIR / "calibrate.py"), day],
                              capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"exit code {proc.returncode}")
        out.write_text(proc.stdout, encoding="utf-8")
        log(f"Calibration list saved to {out}")
    except Exception as e:
        log(f"⚠️ Could not produce the calibration list (does not affect the result): {e}")


# ---------- notification ----------

def fmt_tokens(tokens: dict) -> str:
    inp = tokens.get("input_tokens", 0) + tokens.get("cache_creation_input_tokens", 0) + tokens.get("cache_read_input_tokens", 0)
    return fill(TX["notify_tokens_value"], input=f"{inp / 1000:.1f}", output=f"{tokens.get('output_tokens', 0) / 1000:.1f}")


def success_message(stats: dict, failed_queries: list[str], gap: int | None) -> str:
    total = stats["main"] + stats["custom"]
    tracks = TX["notify_tracks_separator"].join(f"{t} {stats['by_track'].get(t, 0)}" for t in score.TRACKS)
    parts = [fill(TX["notify_shortlist"], n=total), tracks]
    if stats["custom"]:
        parts.append(fill(TX["notify_custom"], n=stats["custom"]))
    parts.append(fill(TX["notify_tokens"], tokens=fmt_tokens(stats["tokens"])))
    if stats.get("failures"):
        parts.append(fill(TX["notify_scoring_failures"], n=stats["failures"]))
    if failed_queries:
        parts.append(fill(TX["notify_fetch_incomplete"], queries=TX["notify_queries_separator"].join(failed_queries)))
    if gap and gap > 1:
        parts.append(fill(TX["notify_gap"], n=gap))
    return TX["notify_separator"].join(parts)


def notify(topic: str | None, message: str, failure: bool = False) -> None:
    """A failed notification is only logged and does not affect the run result. The message holds numbers only, no company names or job titles."""
    if not topic:
        log("⚠️ NTFY_TOPIC is not set; skipping the notification.")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": "Job Search FAILED" if failure else "Job Search", "Tags": "x" if failure else "briefcase"}
    if failure:
        headers["Priority"] = "high"
    try:
        r = requests.post(f"{server}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=15)
        r.raise_for_status()
        log(f"Notified: {message}")
    except Exception as e:
        log(f"⚠️ Notification failed (does not affect the result): {e}")


# ---------- main flow ----------

def pipeline(args, cfg: dict, token: str, run_date: str) -> dict:
    daily = cfg["daily"]
    backoff = daily["retry_backoff_seconds"]
    name = f"raw_{run_date.replace('-', '')}.json"
    db_args = ["--db", str(args.db)]

    # Steps 1-2: fetch retries only the query that failed, never ones that already succeeded
    def do_scrape():
        return fetch.scrape(token, cfg["apify"], args.max_jobs, backoff)

    items, failed = with_retries("Step 1 fetch", [], do_scrape)
    if not items:
        if failed:
            raise StepFailed("Step 1 fetch", f"all queries failed ({', '.join(failed)})")
        log("Apify returned no jobs; no new data today.")
        return {"raw_count": 0, "failed_queries": [], "stats": {"main": 0, "custom": 0, "by_track": {}, "tokens": {}, "failures": 0}}
    cap = daily["max_total_jobs"]
    if len(items) > cap:
        log(f"⚠️ Fetched {len(items)} jobs, over the overall cap of {cap}; keeping the first {cap}.")
        items = items[:cap]
    raw_path = fetch.save_raw(items, run_date.replace("-", ""))
    with_retries("Step 2 slim", backoff, lambda: fetch.process_raw_file(raw_path))

    stats_path = LOG_DIR / f"stats_{run_date}.json"
    stats_path.unlink(missing_ok=True)
    with_retries("Step 3 dedup", backoff, lambda: run_script("dedup.py", ["--only", name, *db_args]))
    with_retries("Step 4 filter", backoff, lambda: run_script("filter.py", ["--only", name, *db_args]))
    with_retries("Steps 5-6 scoring and output", backoff,
                 lambda: run_script("score.py", ["--only", name, "--stats-json", str(stats_path), *db_args]))
    if not stats_path.exists():
        raise StepFailed("Steps 5-6 scoring and output", "no stats file was produced (no matching input file?)")
    return {
        "raw_count": len(items),
        "failed_queries": failed,
        "stats": json.loads(stats_path.read_text(encoding="utf-8")),
    }


def cleanup_logs(keep_days: int) -> None:
    cutoff = time.time() - keep_days * 86400
    for f in LOG_DIR.glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()


def main() -> int:
    global _log_file
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="rerun even if today already succeeded")
    parser.add_argument("--no-notify", action="store_true", help="no notification (for testing)")
    parser.add_argument("--max-jobs", type=int, help="testing: at most this many jobs per query")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="database path (use test.db when testing)")
    args = parser.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    run_date = date.today().isoformat()
    LOG_DIR.mkdir(exist_ok=True)
    _log_file = (LOG_DIR / f"{run_date}.log").open("a", encoding="utf-8")
    cleanup_logs(cfg["daily"]["log_keep_days"])
    topic = None if args.no_notify else os.environ.get("NTFY_TOPIC")

    conn = init_db(args.db)
    init_runs_table(conn)
    prior = run_status(conn, run_date)
    if prior and not args.force:
        status, started = prior
        if status == "success":
            log(f"{run_date} already succeeded; skipping (add --force to rerun).")
            return 0
        age = datetime.now() - datetime.fromisoformat(started)
        if status == "running" and age < timedelta(hours=STALE_RUNNING_HOURS):
            log(f"{run_date} already has a run in progress (started {started}); skipping.")
            return 0

    log(f"===== Daily run {run_date} started =====")
    gap = days_since_last_success(conn, run_date)
    start_run(conn, run_date)
    try:
        for key in ("APIFY_TOKEN", "ANTHROPIC_API_KEY"):
            if not os.environ.get(key):
                raise StepFailed("environment", f"{key} not found")
        result = pipeline(args, cfg, os.environ["APIFY_TOKEN"], run_date)
    except StepFailed as e:
        log(f"❌ Failed: {e}")
        finish_run(conn, run_date, "failed", error=str(e))
        if not args.no_notify:
            notify(topic, fill(TX["notify_failed"], step=e.step, detail=e.detail), failure=True)
        return 1
    except Exception as e:  # unexpected errors are notified too, otherwise you would only see silence
        log(f"❌ Unexpected error: {e!r}")
        finish_run(conn, run_date, "failed", error=repr(e))
        notify(topic, fill(TX["notify_unexpected"], error=repr(e)), failure=True)
        return 1

    stats = result["stats"]
    finish_run(conn, run_date, "success", raw_count=result["raw_count"],
               failed_queries=",".join(result["failed_queries"]), main_count=stats["main"],
               custom_count=stats["custom"], by_track=stats["by_track"], tokens=stats["tokens"])
    if result["raw_count"]:
        write_calibration(run_date)
    message = success_message(stats, result["failed_queries"], gap)
    log(f"===== Done: {message} =====")
    notify(topic, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
