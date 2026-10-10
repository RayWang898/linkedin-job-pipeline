"""
Calibration helper: lists jobs that were nearly excluded or nearly shortlisted, so a few minutes a day is enough to check the rules for wrong calls.
Read-only; changes no data. Usage: python scripts/execution-python/calibrate.py [YYYYMMDD] [--margin 50]
"""

import argparse
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)

EXCLUDED_DIR = Path("data/5_excluded")
SCORED_DIR = Path("data/6_scored")
SHORTLIST_DIR = Path("data/7_shortlist")


def applicants(job: dict):
    d = job.get("applicantsDetails") or {}
    return d.get("count") or d.get("max") or d.get("min")


def shortlisted_ids(day: str) -> set[str]:
    folder = SHORTLIST_DIR / f"{day[:4]}-{day[4:6]}-{day[6:]}"
    ids = set()
    for md in folder.glob("*/*.md"):
        m = re.search(r"^jobId:\s*\"?(\d+)", md.read_text(encoding="utf-8"), re.M)
        if m:
            ids.add(m.group(1))
    return ids


def line(job: dict, extra: str = "") -> str:
    note = f" - {extra}" if extra else ""
    return f"  - {job['title']} @ {job['company']} ({job.get('location')}){note}\n    {job.get('jobUrl', '')}  [{job['jobId']}]"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("day", nargs="?", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--margin", type=int, default=50, help="how far over the applicant cap still counts as a near miss")
    args = parser.parse_args()

    cap = yaml.safe_load(open("config.yaml", encoding="utf-8"))["max_applicants"]
    excluded = json.load(open(EXCLUDED_DIR / f"raw_{args.day}.json", encoding="utf-8"))
    by_reason = defaultdict(list)
    for e in excluded:
        by_reason[e["reason"]].append(e["job"])

    print(f"===== {args.day} calibration list (cap {cap}, margin +{args.margin}) =====\n")

    near = [j for j in by_reason["max_applicants"] if (applicants(j) or 0) <= cap + args.margin]
    print(f"## Just over the applicant cap ({len(near)})")
    for j in sorted(near, key=applicants):
        print(line(j, f"{applicants(j)} applicants"))

    print(f"\n## Excluded by title ({len(by_reason['exclude_title'])}; check for wrong exclusions)")
    for j in by_reason["exclude_title"]:
        print(line(j))

    print(f"\n## Excluded by location ({len(by_reason['location'])})")
    for j in by_reason["location"]:
        print(line(j))

    print(f"\n## Excluded by required years ({len(by_reason['min_years'])})")
    for j in by_reason["min_years"]:
        print(line(j))

    scored = json.load(open(SCORED_DIR / f"raw_{args.day}.json", encoding="utf-8"))
    picked = shortlisted_ids(args.day)
    missed = [j for j in scored if j["jobId"] not in picked]
    print(f"\n## Passed triage but missed the shortlist ({len(missed)}; check for underscoring)")
    for j in sorted(missed, key=lambda j: -j["triage"]["best_score"]):
        t = j["triage"]
        print(line(j, f"{t['best_track']}={t['best_score']} {t['scores']}"))
        print(f"    {t['reason']}")


if __name__ == "__main__":
    main()
