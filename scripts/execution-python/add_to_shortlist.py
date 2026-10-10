"""Add a single job to the shortlist of a given day. Three sources:

1. A LinkedIn job (for example one the filter wrongly excluded but you already applied to): give the jobId; the job is found in data/2_slim/ (it needs a description).
   It is ranked after the largest existing rank of that day, and recommended_date in the jobs table is set to that date.
2. A LinkedIn job outside the fetch scope (for example another city): give the jobId together with --file, --title and --company; the description comes from a text file.
   The properties have source: linkedin, a jobId and a rank, but no posted date, applicant count or scraped date; the jobId is written to the jobs table so a later fetch of it is stopped by dedup.
3. A non-LinkedIn job: give the description text file with --file, plus --title and --company. The properties have source: other,
   no jobId, rank or applicant count; the folder name has no rank prefix.

All three run triage, detailed review and SUMMARY (same as score.py, using the cache), then create the job folder and .md under data/7_shortlist/<date>/.
If the folder of a non-LinkedIn job already exists (for example you put a resume and cover letter there by hand) and its .md has no SUMMARY yet, that folder is reused:
only the .md is rewritten, keeping status, interest and user note, and nothing else in the folder is touched. In every other case an existing job folder is left alone.

Usage:
  venv/bin/python scripts/execution-python/add_to_shortlist.py <jobId> [--date YYYY-MM-DD] [--status applied]
  venv/bin/python scripts/execution-python/add_to_shortlist.py <jobId> --file jd.txt --title ... --company ... [--location ...]
  venv/bin/python scripts/execution-python/add_to_shortlist.py --file jd.txt --title "Technical Writer" \
      --company "Example Co" [--location ...] [--url ...] [--salary ...] [--date ...] [--status ...]
"""

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import anthropic

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)
import dedup
import score

SLIM_DIR = Path("data/2_slim")
LINKEDIN_JOB_URL = "https://www.linkedin.com/jobs/view/{}"


def find_job(job_id: str) -> tuple[dict, str]:
    """Return (job, scraped date). Search the slim files from newest to oldest; a job only counts if it has a description."""
    for path in sorted(SLIM_DIR.glob("*.json"), reverse=True):
        for job in json.loads(path.read_text(encoding="utf-8")):
            if str(job.get("jobId")) == job_id and job.get("description"):
                m = re.search(r"(\d{4})(\d{2})(\d{2})", path.stem)
                return job, "-".join(m.groups()) if m else ""
    sys.exit(f"jobId {job_id} not found in {SLIM_DIR}/ (or it has no description).")


def next_rank(day_dir: Path) -> int:
    ranks = [int(r) for md in day_dir.glob("*/*.md") if (r := score.read_frontmatter(md).get("rank", "")).isdigit()]
    return max(ranks, default=0) + 1


def manual_linkedin_job(args) -> dict:
    """A LinkedIn job outside the fetch scope: it has a jobId, but the data comes from the description file you give, with no posted date or applicant count."""
    if not (args.title and args.company):
        sys.exit("Adding a LinkedIn job by hand needs --title and --company (and the description in --file).")
    description = args.file.read_text(encoding="utf-8").strip()
    if not description:
        sys.exit(f"{args.file} is empty.")
    return {
        "jobId": args.job_id, "source": "linkedin", "title": args.title, "company": args.company,
        "location": args.location, "jobUrl": args.url or LINKEDIN_JOB_URL.format(args.job_id),
        "salaryDetails": args.salary, "description": description,
    }


def other_job(args) -> dict:
    """A non-LinkedIn job: the jobId is only a score-cache key (starts with other-) and is not written to the properties."""
    if not (args.file and args.title and args.company):
        sys.exit("A non-LinkedIn job needs --file, --title and --company.")
    description = args.file.read_text(encoding="utf-8").strip()
    if not description:
        sys.exit(f"{args.file} is empty.")
    return {
        "jobId": f"other-{score.slug(args.company)}-{score.slug(args.title)}",
        "source": "other", "title": args.title, "company": args.company,
        "location": args.location, "jobUrl": args.url, "salaryDetails": args.salary,
        "description": description,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id", nargs="?", help="LinkedIn jobId; leave out for a non-LinkedIn job and use --file instead")
    parser.add_argument("--date", default=date.today().isoformat(), help="which day's shortlist to add to (YYYY-MM-DD, default today)")
    parser.add_argument("--status", help="status property, e.g. applied; default backlog, or the existing value when an existing .md is rewritten")
    parser.add_argument("--db", type=Path, default=score.DB_PATH)
    parser.add_argument("--file", type=Path, help="non-LinkedIn job: text file with the job description")
    parser.add_argument("--title")
    parser.add_argument("--company")
    parser.add_argument("--location")
    parser.add_argument("--url")
    parser.add_argument("--salary")
    args = parser.parse_args()
    if not (args.job_id or args.file):
        sys.exit("Give a LinkedIn jobId, or add a non-LinkedIn job with --file (giving both means a LinkedIn job outside the fetch scope).")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not found. Set it in .env or export it as an environment variable.")

    day_dir = score.SHORTLIST_DIR / args.date
    day_dir.mkdir(parents=True, exist_ok=True)
    if args.job_id:
        if score.find_job_folder(day_dir, args.job_id):
            sys.exit(f"{day_dir} already has jobId {args.job_id}; not adding it again.")
        if args.file:
            job, scraped_date = manual_linkedin_job(args), None
        else:
            job, scraped_date = find_job(args.job_id)
        rank = next_rank(day_dir)
        folder = day_dir / f"{rank:02d}_{score.slug(job['company'])}_{score.slug(job['title'])}"
    else:
        job, scraped_date, rank = other_job(args), None, None
        folder = day_dir / f"{score.slug(job['company'])}_{score.slug(job['title'])}"
    md = folder / f"{folder.name}.md"
    reuse = not args.job_id and md.exists() and "## SUMMARY" not in md.read_text(encoding="utf-8")
    if folder.exists() and not reuse:
        sys.exit(f"{folder} already exists; not adding it again.")
    status = [args.status] if args.status else ((score.read_status(md) if reuse else None) or ["backlog"])
    user_note = score.read_user_note(md) if reuse else ""
    interest = score.read_interest(md) if reuse else ""

    profiles = score.load_profiles()
    config = score.load_config()
    client = anthropic.Anthropic()
    conn = score.init_db(args.db)
    score.init_score_tables(conn)
    usages = {"triage": score.Usage(), "detail": score.Usage(), "summary": score.Usage()}

    triage = score.triage_job(conn, client, config, profiles, usages["triage"], job)
    if triage is None:
        sys.exit("Triage failed; nothing written.")
    job = {**job, "triage": triage}
    if reuse and (custom_resume := score.read_frontmatter(md).get("resume")):
        job["resume_file"] = custom_resume  # a custom resume you already put in the folder is not overwritten by the track default
    job["detail"] = score.detail_job(conn, client, config, profiles, usages["detail"], job)
    job["summary"] = score.summarize_job(client, config, usages["summary"], job)

    folder.mkdir(exist_ok=True)
    md.write_text(score.format_job_note(rank, job, args.date, scraped_date, status, user_note, interest), encoding="utf-8")
    if args.job_id:
        conn.execute(
            "INSERT OR IGNORE INTO jobs (job_id, company_key, description_hash, first_seen_date) VALUES (?, ?, ?, ?)",
            (args.job_id, dedup.company_key(job), dedup.description_hash(job["description"]), args.date))
        conn.execute("UPDATE jobs SET recommended_date = ? WHERE job_id = ?", (args.date, args.job_id))
        conn.commit()
    conn.close()

    print(f"Written: {folder}/{folder.name}.md (rank {rank or 'none'}, best {triage['best_track']} {triage['best_score']})")
    for label, u in usages.items():
        print(f"{label} usage: {u.plain()}")
