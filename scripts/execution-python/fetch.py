"""
Stages 1 and 2: fetch and slim.
Step 1: call an Apify actor to fetch public LinkedIn jobs (no login); the raw data is saved to data/1_raw/raw_<YYYYMMDD>.json.
Step 2: keep only the needed fields and save to data/2_slim/<same file name>.json.

Usage:
  venv/bin/python scripts/execution-python/fetch.py                      # fetch from Apify, then slim
  venv/bin/python scripts/execution-python/fetch.py --max-jobs 5         # testing: at most 5 jobs per query
  venv/bin/python scripts/execution-python/fetch.py --from-samples       # no Apify call; slim samples/*.json instead
Needs the environment variable APIFY_TOKEN (can be set in .env).
"""

import argparse
import json
import os
import string
import sys
import time
from datetime import date
from pathlib import Path

import yaml

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)

SAMPLES_DIR = Path("samples")
RAW_DIR = Path("data/1_raw")
SLIM_DIR = Path("data/2_slim")
CONFIG_PATH = Path("config.yaml")

ESSENTIAL_FIELDS = [
    "jobId",
    "title",
    "company",
    "companyId",
    "location",
    "postedDate",
    "description",
    "applicantsDetails",
    "applicationMethod",
    "jobUrl",
    "salaryDetails",
    "recruiter",
    "matched_queries",  # added by this program in Step 1: the search queries (A, B, ...) that found this job
]


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------- Step 1: fetch ----------

def query_labels(apify_cfg: dict) -> list[str]:
    """Each query is labeled A, B, C ... in order, matching the order of queries in config.yaml."""
    return list(string.ascii_uppercase[: len(apify_cfg["queries"])])


def max_jobs_for(apify_cfg: dict, label: str | None) -> int:
    """Job cap for one query: max_jobs_by_query if the query is listed there, else max_jobs_per_search.
    For a single-location search (apify_cfg["locations"] has one entry) the smaller of that and max_jobs_by_location is used."""
    cap = apify_cfg.get("max_jobs_by_query", {}).get(label, apify_cfg["max_jobs_per_search"])
    locations = apify_cfg["locations"]
    if len(locations) == 1:
        loc_cap = apify_cfg.get("max_jobs_by_location", {}).get(locations[0])
        if loc_cap:
            cap = min(cap, loc_cap)
    return cap


def location_tag(apify_cfg: dict, location: str) -> str:
    """The first location gets no marker (matches older records); other locations add @city after the label, used in failure lists and logs."""
    return "" if location == apify_cfg["locations"][0] else f"@{location.split(',')[0]}"


def build_run_input(apify_cfg: dict, query: str, max_jobs: int | None = None, label: str | None = None) -> dict:
    """Build the actor input for one search. maxJobs defaults to the per-query cap times the number of locations; otherwise the actor returns only 25 jobs."""
    per_search = max_jobs_for(apify_cfg, label)
    return {
        "queries": [query],
        "locations": apify_cfg["locations"],
        "datePosted": apify_cfg["date_posted"],
        "sortBy": apify_cfg["sort_by"],
        "fetchDetails": apify_cfg["fetch_details"],
        "includeSimilar": False,  # similar jobs are dropped in Step 2, so turning this on only costs money
        "maxJobsPerSearch": per_search,
        "maxJobs": max_jobs if max_jobs else per_search * len(apify_cfg["locations"]),
    }


def merge_results(results: dict[str, list[dict]]) -> list[dict]:
    """Merge results by jobId; a job found by several queries records all of them in matched_queries."""
    merged: dict[str, dict] = {}
    for label, items in results.items():
        for item in items:
            job = merged.setdefault(item["jobId"], {**item, "matched_queries": []})
            if label not in job["matched_queries"]:
                job["matched_queries"].append(label)
    return list(merged.values())


def run_query(client, apify_cfg: dict, name: str, query: str, max_jobs: int | None,
              label: str | None = None) -> list[dict]:
    """name is for log display (may carry a location marker); label is the query label used to look up the cap (A, B, ...); without label, name is used."""
    label = label or name
    run_input = build_run_input(apify_cfg, query, max_jobs, label)
    print(f"Calling Apify actor: {apify_cfg['actor']} (search {name}, maxJobs={run_input['maxJobs']})")
    run = client.actor(apify_cfg["actor"]).call(run_input=run_input)
    if run is None or run.status != "SUCCEEDED":
        status = getattr(run, "status", "unknown")
        raise RuntimeError(f"Apify run did not succeed (search {name}, status: {status})")
    items = list(client.dataset(run.default_dataset_id).iterate_items())
    print(f"  Search {name} returned {len(items)} jobs")
    return items


def scrape(token: str, apify_cfg: dict, max_jobs: int | None = None,
           backoff: list[int] | None = None) -> tuple[list[dict], list[str]]:
    """Fetch search by search; returns (merged jobs, queries given up on).

    backoff is the seconds to wait before each retry (its length is the retry count); only the failed query is retried, never ones that already succeeded.
    Without backoff a failed query is given up on right away.
    """
    from apify_client import ApifyClient  # only needed when really fetching

    client = ApifyClient(token)
    results = {}
    failed = []
    # each location is called separately (each has its own cap); results for one query across locations merge under one label
    for location in apify_cfg["locations"]:
        loc_cfg = {**apify_cfg, "locations": [location]}
        tag = location_tag(apify_cfg, location)
        for label, query in zip(query_labels(apify_cfg), apify_cfg["queries"]):
            name = f"{label}{tag}"
            waits = [0] + list(backoff or [])
            for attempt, wait in enumerate(waits, 1):
                if wait:
                    print(f"  Search {name}: waiting {wait}s before retry {attempt - 1}")
                    time.sleep(wait)
                try:
                    results.setdefault(label, []).extend(run_query(client, loc_cfg, name, query, max_jobs, label))
                    break
                except Exception as e:
                    print(f"  ⚠️ Search {name} failed: {e}")
            else:
                failed.append(name)
    items = merge_results(results)
    print(f"After merge and dedup: {len(items)} (sum over queries: {sum(len(v) for v in results.values())})")
    if failed:
        print(f"⚠️ Queries given up on: {', '.join(failed)}")
    return items, failed


def save_raw(items: list[dict], run_date: str | None = None) -> Path:
    run_date = run_date or date.today().strftime("%Y%m%d")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"raw_{run_date}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"Raw data saved to: {path}")
    return path


# ---------- Step 2: slim ----------

def slim_job(raw_job: dict) -> dict:
    return {field: raw_job.get(field) for field in ESSENTIAL_FIELDS}


def field_completeness(records: list[dict]) -> dict[str, int]:
    return {
        field: sum(1 for r in records if r.get(field) is not None)
        for field in ESSENTIAL_FIELDS
    }


def process_raw_file(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        raw_jobs = json.load(f)

    slimmed = [slim_job(job) for job in raw_jobs]

    total = len(raw_jobs)
    counts = field_completeness(slimmed)

    print(f"Read: {path}")
    print(f"Raw jobs: {total}")
    print(f"Jobs after slimming: {len(slimmed)}", end="")
    if len(slimmed) < total:
        print("  ⚠️ Fewer jobs than the raw data; data may have been lost!")
    else:
        print()
    print()
    print("Field completeness:")
    for field, count in counts.items():
        pct = (count / total * 100) if total else 0
        print(f"  {field:<18} {count}/{total} ({pct:.0f}%)")

    SLIM_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SLIM_DIR / path.name
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(slimmed, f, ensure_ascii=False, indent=2)

    print()
    print(f"Saved to: {out_path}")
    print("-" * 50)

    return slimmed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-samples", action="store_true", help="no Apify call; slim samples/*.json instead")
    parser.add_argument("--max-jobs", type=int, help="testing: at most this many jobs per query")
    args = parser.parse_args()

    if args.from_samples:
        sample_files = sorted(SAMPLES_DIR.glob("*.json"))
        if not sample_files:
            print(f"No .json files found in {SAMPLES_DIR}/.")
        for sample_path in sample_files:
            process_raw_file(sample_path)
        sys.exit(0)

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("APIFY_TOKEN not found. Set it in .env or export it as an environment variable.")

    config = load_config()
    items, failed = scrape(token, config["apify"], args.max_jobs)
    if not items:
        sys.exit("Apify returned no jobs; no file written.")

    process_raw_file(save_raw(items))
