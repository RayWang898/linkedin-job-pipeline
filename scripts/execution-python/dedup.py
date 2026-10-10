"""
Stage 3: dedup.
Rule 1: a jobId that already exists in jobs.db is skipped (not written again).
A non-empty recommended_date means the job was once on a shortlist (kept forever); empty means it was only seen (can be cleaned up later).
Rule 2: the same company (companyId first, company name if missing) with an identical normalized description counts as a duplicate;
       the first one seen is kept and the others are marked duplicate_of.
The comparison covers both the current fetch and everything in jobs.db.
"""

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)

SLIM_DIR = Path("data/2_slim")
DEDUPED_DIR = Path("data/3_deduped")
DB_PATH = Path("jobs.db")


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            company_key TEXT NOT NULL,
            description_hash TEXT NOT NULL,
            duplicate_of TEXT,
            first_seen_date TEXT NOT NULL,
            recommended_date TEXT
        )
        """
    )
    columns = [row[1] for row in conn.execute("PRAGMA table_info(jobs)")]
    if "recommended_date" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN recommended_date TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_company_desc ON jobs(company_key, description_hash)"
    )
    conn.commit()
    return conn


def normalize_description(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def description_hash(text: str) -> str:
    return hashlib.sha256(normalize_description(text).encode("utf-8")).hexdigest()


def company_key(job: dict) -> str:
    company_id = job.get("companyId")
    if company_id:
        return f"id:{company_id}"
    return f"name:{(job.get('company') or '').strip().lower()}"


def find_existing_match(conn: sqlite3.Connection, c_key: str, desc_hash: str) -> str | None:
    row = conn.execute(
        "SELECT job_id FROM jobs WHERE company_key = ? AND description_hash = ? "
        "AND duplicate_of IS NULL ORDER BY first_seen_date LIMIT 1",
        (c_key, desc_hash),
    ).fetchone()
    return row[0] if row else None


def dedup_batch(conn: sqlite3.Connection, jobs: list[dict], run_date: str | None = None, commit: bool = True) -> dict:
    """Mark new jobs as seen and return the kept ones. With commit=False the inserts stay in an open transaction for the caller to commit."""
    run_date = run_date or date.today().isoformat()

    kept = []
    skipped_seen_job_id = []
    skipped_duplicate_content = []

    for job in jobs:
        job_id = job["jobId"]

        already_seen = conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if already_seen:
            skipped_seen_job_id.append(job_id)
            continue

        c_key = company_key(job)
        d_hash = description_hash(job.get("description"))
        match_id = find_existing_match(conn, c_key, d_hash)

        if match_id:
            conn.execute(
                "INSERT INTO jobs (job_id, company_key, description_hash, duplicate_of, first_seen_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, c_key, d_hash, match_id, run_date),
            )
            skipped_duplicate_content.append((job_id, match_id))
        else:
            conn.execute(
                "INSERT INTO jobs (job_id, company_key, description_hash, duplicate_of, first_seen_date) "
                "VALUES (?, ?, ?, NULL, ?)",
                (job_id, c_key, d_hash, run_date),
            )
            kept.append(job)

    if commit:
        conn.commit()

    return {
        "kept": kept,
        "skipped_seen_job_id": skipped_seen_job_id,
        "skipped_duplicate_content": skipped_duplicate_content,
    }


def process_slim_file(conn: sqlite3.Connection, path: Path) -> None:
    with path.open(encoding="utf-8") as f:
        jobs = json.load(f)

    result = dedup_batch(conn, jobs, commit=False)  # committed only after the output file is written

    print(f"Read: {path}")
    print(f"Input jobs: {len(jobs)}")
    print(f"Kept (new jobs): {len(result['kept'])}")
    print(f"Skipped (jobId already seen): {len(result['skipped_seen_job_id'])}")
    print(f"Skipped (same company, same description): {len(result['skipped_duplicate_content'])}")
    for dup_id, kept_id in result["skipped_duplicate_content"]:
        print(f"    {dup_id} -> duplicate_of {kept_id}")

    DEDUPED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DEDUPED_DIR / path.name
    try:
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(result["kept"], f, ensure_ascii=False, indent=2)
        conn.commit()  # mark the jobs as seen only now: if anything above failed, a rerun finds them new again
    except Exception:
        conn.rollback()
        raise

    print(f"Saved to: {out_path}")
    print("-" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH, help="database path (use test.db when testing)")
    parser.add_argument("--only", help="process only this file name (used by the daily run, e.g. raw_20261004.json)")
    args = parser.parse_args()
    conn = init_db(args.db)
    slim_files = sorted(SLIM_DIR.glob("*.json"))
    if args.only:
        slim_files = [f for f in slim_files if f.name == args.only]
    if not slim_files:
        print(f"No .json files found in {SLIM_DIR}/. Run scripts/execution-python/fetch.py first.")
    for slim_path in slim_files:
        process_slim_file(conn, slim_path)
    conn.close()
