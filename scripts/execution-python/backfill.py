"""
One-off backfill: fetch relevant jobs posted in the past N days and run Steps 1-6; the shortlist goes to its own folder data/7_shortlist/<run date>-backfill/.
It is separate from the daily run and does not write daily_runs.

How it works:
- Each query is fetched on its own (datePostedSeconds = N days, capped by --cap), several in parallel (--concurrency).
- Each query is saved to data/1_raw/backfill_<date>/<label>.json as soon as it finishes; after an interruption, rerun with the same --run-date and saved queries are skipped.
- When all are fetched they are merged into data/1_raw/raw_backfill_<date>.json, then slimming, dedup, filter and scoring run (the existing scripts).
- One location at a time: the first entry of locations in config by default; use --location for another (together with --folder-suffix and a new --run-date).

Usage:
  venv/bin/python scripts/execution-python/backfill.py --dry-run              # print the plan only, no API calls
  venv/bin/python scripts/execution-python/backfill.py                        # full backfill (fetching takes hours; run it in the background)
  venv/bin/python scripts/execution-python/backfill.py --queries B,C --cap 100   # small test
  venv/bin/python scripts/execution-python/backfill.py --fetch-only           # fetch and merge only, no scoring
  venv/bin/python scripts/execution-python/backfill.py --run-date 20261005    # resume an interrupted backfill (same batch)
  venv/bin/python scripts/execution-python/backfill.py --location "London, UK" --folder-suffix backfill-london --run-date 20261005lon   # London backfill
  venv/bin/python scripts/execution-python/backfill.py --queries A,E --run-date 20261005b   # another batch: a new --run-date keeps the files and summary of the previous batch
Needs APIFY_TOKEN and ANTHROPIC_API_KEY (can be set in .env); with scoring.provider claude-code, the claude command instead of the key.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)
import fetch  # noqa: E402
import providers  # noqa: E402

RAW_DIR = Path("data/1_raw")
SECONDS_PER_DAY = 86400
RUN_TIMEOUT_SECS = 8 * 3600  # run-time cap for one query; Apify's default timeout is too short for thousands of detail pages
SECS_PER_JOB = 6.4  # measured: fetching details takes about 6 seconds per job


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def part_path(run_date: str, label: str) -> Path:
    return RAW_DIR / f"backfill_{run_date}" / f"{label}.json"


def fetch_one(client, apify_cfg: dict, location: str, label: str, query: str, days: int, cap: int,
              run_date: str, backoff: list[int]) -> tuple[str, int]:
    """Fetch one query and save it; returns (label, count). A failure is retried with backoff and raises if it still fails."""
    primary_cfg = {**apify_cfg, "locations": [location]}
    run_input = fetch.build_run_input(primary_cfg, query, cap, label)
    run_input.pop("datePosted", None)
    run_input.update({"datePostedSeconds": days * SECONDS_PER_DAY, "maxJobsPerSearch": cap, "maxJobs": cap})
    last_error = None
    for wait in [0] + backoff:
        if wait:
            log(f"  Search {label}: waiting {wait}s before retrying")
            time.sleep(wait)
        try:
            log(f"Search {label} started (past {days} days, cap {cap})")
            run = client.actor(apify_cfg["actor"]).call(run_input=run_input, run_timeout=timedelta(seconds=RUN_TIMEOUT_SECS))
            if run is None or run.status not in ("SUCCEEDED", "TIMED-OUT"):
                raise RuntimeError(f"Apify run did not succeed (status: {getattr(run, 'status', 'unknown')})")
            items = list(client.dataset(run.default_dataset_id).iterate_items())
            if run.status == "TIMED-OUT":
                log(f"  ⚠️ Search {label} timed out; keeping the {len(items)} jobs fetched so far")
            path = part_path(run_date, label)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            return label, len(items)
        except Exception as e:
            last_error = e
            log(f"  ⚠️ Search {label} failed: {str(e)[:150]}")
    raise RuntimeError(f"Search {label} given up: {last_error}")


def run_script(script: str, args: list[str]) -> None:
    cmd = [sys.executable, str(SCRIPTS_DIR / script), *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    for line in proc.stdout:
        print(f"  | {line.rstrip()}", flush=True)
    if proc.wait() != 0:
        raise RuntimeError(f"{script} exited with code {proc.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14, help="how many days back to fetch (default 14)")
    parser.add_argument("--cap", type=int, default=1000, help="job cap per query (a LinkedIn search returns about 1,000 at most)")
    parser.add_argument("--concurrency", type=int, default=3, help="how many queries to run at once (limited by your Apify account memory)")
    parser.add_argument("--queries", help="fetch only these query labels, comma-separated, e.g. B,C (default: all)")
    parser.add_argument("--location", help="location to backfill (default: the first location in config), e.g. \"London, UK\"")
    parser.add_argument("--folder-suffix", default="backfill", help="suffix of the shortlist folder (default backfill; use e.g. backfill-london for another location)")
    parser.add_argument("--run-date", default=date.today().strftime("%Y%m%d"), help="date of the backfill batch (reuse the original one when resuming)")
    parser.add_argument("--fetch-only", action="store_true", help="fetch and merge only; skip the steps after slimming")
    parser.add_argument("--shortlist-size", type=int, default=40, help="cap of the main list in the backfill shortlist")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only; no API calls")
    args = parser.parse_args()

    config = fetch.load_config()
    apify_cfg = config["apify"]
    labels = fetch.query_labels(apify_cfg)
    queries = dict(zip(labels, apify_cfg["queries"]))
    wanted = [l.strip().upper() for l in args.queries.split(",")] if args.queries else labels
    unknown = [l for l in wanted if l not in queries]
    if unknown:
        sys.exit(f"No such queries: {', '.join(unknown)} (available: {', '.join(labels)})")
    done = [l for l in wanted if part_path(args.run_date, l).exists()]
    todo = [l for l in wanted if l not in done]
    worst_secs = max((args.cap * SECS_PER_JOB for _ in todo), default=0)
    total_secs = len(todo) * args.cap * SECS_PER_JOB / max(1, args.concurrency)

    location = args.location or apify_cfg["locations"][0]
    log(f"Backfill batch {args.run_date}: location {location}, past {args.days} days, cap {args.cap} per query")
    log(f"Done (skipped): {', '.join(done) or 'none'}; to fetch: {', '.join(todo) or 'none'}, concurrency {args.concurrency}")
    log(f"Fetch time estimate: longest query about {worst_secs / 3600:.1f} h, all about {total_secs / 3600:.1f} h (worst case; queries may not fill their cap)")
    if args.dry_run:
        log("dry-run: no API calls made, exiting.")
        return

    token = os.environ.get("APIFY_TOKEN")
    if todo and not token:
        sys.exit("APIFY_TOKEN not found. Set it in .env or export it as an environment variable.")
    if not args.fetch_only:
        try:
            problem = providers.missing(providers.provider(config))
        except ValueError as e:
            problem = str(e)
        if problem:
            sys.exit(problem)

    failed = []
    if todo:
        from apify_client import ApifyClient

        client = ApifyClient(token)
        backoff = config["daily"]["retry_backoff_seconds"]
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            futures = {pool.submit(fetch_one, client, apify_cfg, location, l, queries[l], args.days, args.cap,
                                   args.run_date, backoff): l for l in todo}
            for fut in as_completed(futures):
                label = futures[fut]
                try:
                    _, n = fut.result()
                    log(f"Search {label} done: {n} jobs (saved to {part_path(args.run_date, label)})")
                except Exception as e:
                    failed.append(label)
                    log(f"❌ {e}")
    if failed:
        log(f"Queries given up on: {', '.join(failed)}. Rerun with the same --run-date {args.run_date} to fetch them again (finished ones are skipped).")

    results = {l: json.loads(part_path(args.run_date, l).read_text(encoding="utf-8"))
               for l in labels if part_path(args.run_date, l).exists()}
    if not results:
        sys.exit("No query returned any data; no file written.")
    items = fetch.merge_results(results)
    log(f"After merge and dedup: {len(items)} jobs (sum over queries: {sum(len(v) for v in results.values())}, queries: {', '.join(results)})")
    raw_path = fetch.save_raw(items, f"backfill_{args.run_date}")
    fetch.process_raw_file(raw_path)
    if args.fetch_only:
        log("fetch-only: fetching and slimming done; dedup, filter and scoring were not run.")
        return

    name = raw_path.name
    run_script("dedup.py", ["--only", name])
    run_script("filter.py", ["--only", name])
    run_script("score.py", ["--only", name, "--folder-suffix", args.folder_suffix, "--shortlist-size", str(args.shortlist_size),
                            "--summary-name", f"_summary_backfill_{args.run_date}.md"])
    log(f"Backfill finished. The shortlist is in data/7_shortlist/{date.today().isoformat()}-{args.folder_suffix}/")


if __name__ == "__main__":
    main()
