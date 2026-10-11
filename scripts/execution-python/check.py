"""
Dry run: check the workspace and report what a daily run would do. Makes no network call, calls no model and writes nothing
(the database is opened read-only).

Usage (usually through run_daily.py):
  venv/bin/python scripts/execution-python/run_daily.py --dry-run [--json] [--db FILE] [--max-jobs N]

Exit code 0 when there is no error (warnings are fine), 1 when something must be fixed first. With --json the whole report is one JSON object.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)

ENGINE = Path(__file__).resolve().parents[2]
CATCH_UP_DAYS = 3  # an earlier day that failed after fetching is finished first, if it is at most this many days old
STALE_RUNNING_HOURS = 3  # a run still marked running after this long is treated as an earlier abnormal stop
LARGE_PROFILE_CHARS = 20000  # sent with every triage call, so very long profiles cost money on every job


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []
        self.plan: dict = {}
        self.state: dict = {}

    def add(self, status: str, name: str, detail: str) -> None:
        self.checks.append({"status": status, "name": name, "detail": detail})

    @property
    def ok(self) -> bool:
        return not any(c["status"] == "error" for c in self.checks)


def read_state(db_path: Path, today: str) -> dict:
    """What a run today would do, from daily_runs (opened read-only; nothing is created)."""
    state = {"db": str(db_path), "db_exists": db_path.exists(), "today": today, "today_status": None,
             "steps_done": [], "would": "start fresh", "catch_up": []}
    if not db_path.exists():
        return state
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'daily_runs'").fetchone():
            return state
        has_steps = "steps_done" in [r[1] for r in conn.execute("PRAGMA table_info(daily_runs)")]
        cols = "run_date, status, started_at" + (", steps_done" if has_steps else "")
        first = (date.fromisoformat(today) - timedelta(days=CATCH_UP_DAYS)).isoformat()
        rows = conn.execute(f"SELECT {cols} FROM daily_runs WHERE run_date >= ? ORDER BY run_date", (first,)).fetchall()
    finally:
        conn.close()
    for row in rows:
        day, status, started = row[0], row[1], row[2]
        steps = json.loads(row[3]) if has_steps and row[3] else []
        if day == today:
            state.update(today_status=status, steps_done=steps)
            fresh_running = status == "running" and datetime.now() - datetime.fromisoformat(started) < timedelta(hours=STALE_RUNNING_HOURS)
            if status == "success":
                state["would"] = "skip (today already succeeded; --force runs it again)"
            elif fresh_running:
                state["would"] = "skip (a run started less than %d hours ago is still marked running)" % STALE_RUNNING_HOURS
            elif steps:
                state["would"] = "resume after: " + ", ".join(steps)
        elif status != "success" and "fetch" in steps:
            state["catch_up"].append({"day": day, "status": status, "steps_done": steps})
    return state


def collect(db: str, max_jobs: int | None) -> Report:
    r = Report()
    ws = Path.cwd()
    r.plan["workspace"] = str(ws)

    config_path = Path("config.yaml")
    if not config_path.exists():
        r.add("error", "config", f"config.yaml not found in {ws}. Run init.py to create a workspace, or pass --workspace.")
        return r
    try:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        r.add("error", "config", f"config.yaml is not valid YAML: {e}")
        return r
    r.add("ok", "config", "config.yaml found and readable")

    # keys: only whether they are set, never their value
    for key, required in (("APIFY_TOKEN", True), ("ANTHROPIC_API_KEY", True), ("NTFY_TOPIC", False)):
        if os.environ.get(key):
            r.add("ok", f"env {key}", "set")
        elif required:
            r.add("error", f"env {key}", f"not set; put it in {ws / '.env'}")
        else:
            r.add("ok", f"env {key}", "not set (optional: no phone notification)")

    # the engine loads and validates tracks, visa, prompts.yaml and texts.yaml at import
    try:
        import score
    except SystemExit as e:
        r.add("error", "config values", str(e.code))
        score = None
    except ImportError as e:
        r.add("error", "dependencies", f"{e}. Install them in the engine folder: venv/bin/pip install -r requirements.txt")
        score = None

    if score is not None:
        tracks = score.TRACKS
        r.add("ok", "tracks", ", ".join(f"{t} {i['name']}" for t, i in tracks.items()))
        r.plan["tracks"] = list(tracks)
        r.plan["visa_enabled"] = score.VISA_ENABLED
        for name in ("prompts.yaml", "texts.yaml"):
            if Path(name).exists():
                r.add("ok", name, "found: overrides the built-in text")
        try:
            profiles = score.load_profiles()
        except SystemExit as e:
            r.add("error", "profiles", str(e.code))
            profiles = None
        if profiles:
            files = {"shared.md": profiles["shared"], **{score.TRACKS[t]["file"]: profiles["tracks"][t] for t in score.TRACKS}}
            total = sum(len(v) for v in files.values())
            r.add("ok", "profiles", f"{len(files)} files, {total} characters")
            if total > LARGE_PROFILE_CHARS:
                r.add("warn", "profiles size", f"{total} characters are sent with every job; shorter profiles cost less")
            for name, text in files.items():
                example = ENGINE / "profiles.example" / name
                if example.exists() and example.read_text(encoding="utf-8").strip() == text.strip():
                    r.add("warn", f"profile {name}", "identical to the template: replace the example text with your own details")
                marks = re.sub(r"<!--.*?-->", "", text, flags=re.S).count("(unconfirmed)")  # template comments mention the marker
                if marks:
                    r.add("warn", f"profile {name}", f"{marks} (unconfirmed) marker(s) left; confirm them and delete the markers")

    # fetch plan
    apify = cfg.get("apify") or {}
    locations, queries = apify.get("locations") or [], apify.get("queries") or []
    names = apify.get("query_names") or []
    if not locations:
        r.add("error", "apify.locations", "empty: nothing would be searched")
    if not queries:
        r.add("error", "apify.queries", "empty: nothing would be searched")
    if queries and len(names) != len(queries):
        r.add("warn", "apify.query_names", f"{len(names)} names for {len(queries)} queries; names are matched to queries by order")
    if not apify.get("fetch_details", True):
        r.add("error", "apify.fetch_details", "must be true: descriptions are needed by the filter and the scoring")
    if not cfg.get("location_whitelist"):
        r.add("error", "location_whitelist", "empty: the filter would exclude every job")
    if locations and queries:
        import fetch
        searches, upper = 0, 0
        for loc in locations:
            loc_cfg = {**apify, "locations": [loc]}
            for label in fetch.query_labels(apify):
                searches += 1
                upper += max_jobs if max_jobs else fetch.max_jobs_for(loc_cfg, label)
        cap = (cfg.get("daily") or {}).get("max_total_jobs")
        r.plan.update(searches=searches, queries=len(queries), locations=len(locations),
                      max_jobs_fetched=min(upper, cap) if cap else upper, max_total_jobs=cap)
        r.add("ok", "fetch plan", f"{searches} Apify searches ({len(queries)} queries per location, {len(locations)} location(s)), "
                                  f"at most {r.plan['max_jobs_fetched']} jobs after the daily cap")

    scoring = cfg.get("scoring") or {}
    r.plan["scoring"] = {k: scoring.get(k) for k in ("triage_model", "detail_model", "summary_model", "shortlist_min_score",
                                                      "shortlist_size", "detail_count", "detail_custom", "output_language")}
    visa = cfg.get("visa") or {}
    if visa.get("enabled") and not visa.get("exclude_patterns"):
        r.add("warn", "visa", "enabled but exclude_patterns is empty: only the scoring model will warn about jobs that refuse sponsorship (it does not exclude them)")
    tracker = cfg.get("tracker_path")
    if tracker and not Path(tracker).exists():
        r.add("warn", "tracker_path", f"{tracker} not found: the already-applied check will be skipped")

    r.state = read_state(Path(db), date.today().isoformat())
    return r


def render(r: Report) -> str:
    marks = {"ok": "ok   ", "warn": "WARN ", "error": "ERROR"}
    lines = [f"Workspace: {r.plan.get('workspace')}", ""]
    lines += [f"[{marks[c['status']]}] {c['name']}: {c['detail']}" for c in r.checks]
    p = r.plan
    if "searches" in p:
        s = p.get("scoring") or {}
        lines += ["", "A run now would:",
                  f"  - make {p['searches']} Apify searches and fetch at most {p['max_jobs_fetched']} jobs",
                  f"  - run the hard filters (free), then triage each remaining job with {s.get('triage_model')}",
                  f"  - write detailed advice for at most {s.get('detail_count')} jobs with {s.get('detail_model')}"
                  + (" (plus the needs-custom block)" if s.get("detail_custom") else "") + ", and a summary with " + str(s.get("summary_model")),
                  "  - cost: see the Cost section of README.md; the numbers above are the upper bounds"]
    st = r.state
    if st:
        lines += ["", f"Today ({st['today']}): {st['would']}"]
        for c in st["catch_up"]:
            lines.append(f"  - first finish {c['day']} (status {c['status']}, done: {', '.join(c['steps_done'])})")
        if not st["db_exists"]:
            lines.append(f"  (no database yet at {st['db']}; it is created by the first real run)")
    lines += ["", "Ready." if r.ok else "Not ready: fix the ERROR lines first."]
    if r.ok and any(c["status"] == "warn" for c in r.checks):
        lines[-1] = "Ready, with warnings."
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="(accepted so run_daily.py --dry-run can be forwarded as is)")
    parser.add_argument("--json", action="store_true", help="print the report as one JSON object")
    parser.add_argument("--db", default="jobs.db", help="database path (default jobs.db)")
    parser.add_argument("--max-jobs", type=int, help="the per-search cap you would pass to the run")
    args, _ = parser.parse_known_args(argv)
    report = collect(args.db, args.max_jobs)
    if args.json:
        print(json.dumps({"ok": report.ok, "checks": report.checks, "plan": report.plan, "state": report.state},
                         ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
