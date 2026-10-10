"""
Stage 3: filter. Hard rules only, no AI.
Applied in order: location whitelist -> applicant cap -> title exclusion -> visa exclusion -> required years -> excluded keywords -> staffing agencies -> tracker duplicate check.
A job that fails any condition is excluded and the reason is recorded.
For jobs outside the location whitelist, Nominatim also computes the distance from the center point; it is printed as a diagnostic only and does not change the result.
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import requests
import yaml

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)

DEDUPED_DIR = Path("data/3_deduped")
FILTERED_DIR = Path("data/4_filtered")
EXCLUDED_DIR = Path("data/5_excluded")
DB_PATH = Path("jobs.db")
CONFIG_PATH = Path("config.yaml")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "linkedin-job-search-tool (personal use)"


def visa_settings(config: dict) -> dict:
    """Normalized `visa:` section: {enabled, skip_locations, exclude_patterns}. Off unless `visa.enabled` is true."""
    old = [k for k in ("visa_skip_locations", "exclude_visa_patterns") if k in config]
    if old:
        sys.exit(f"config.yaml: {', '.join(old)} moved under `visa:` (as skip_locations / exclude_patterns). "
                 "Add `visa:` with `enabled: true` to keep the visa rules; see config.example.yaml.")
    section = config.get("visa") or {}
    return {
        "enabled": bool(section.get("enabled", False)),
        "skip_locations": section.get("skip_locations") or [],
        "exclude_patterns": section.get("exclude_patterns") or [],
    }


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    visa_settings(config)  # fail early on an outdated config
    return config


def init_geocode_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geocode_cache (
            address TEXT PRIMARY KEY,
            lat REAL,
            lon REAL
        )
        """
    )
    conn.commit()


def geocode(address: str, conn: sqlite3.Connection) -> tuple[float, float] | None:
    row = conn.execute(
        "SELECT lat, lon FROM geocode_cache WHERE address = ?", (address,)
    ).fetchone()
    if row:
        return (row[0], row[1]) if row[0] is not None else None

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        time.sleep(1)  # Nominatim usage policy: at most 1 request per second
    except Exception as e:
        print(f"  ⚠️ Could not geocode \"{address}\": {e}")
        conn.execute(
            "INSERT INTO geocode_cache (address, lat, lon) VALUES (?, NULL, NULL)",
            (address,),
        )
        conn.commit()
        return None

    if not results:
        conn.execute(
            "INSERT INTO geocode_cache (address, lat, lon) VALUES (?, NULL, NULL)",
            (address,),
        )
        conn.commit()
        return None

    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    conn.execute(
        "INSERT INTO geocode_cache (address, lat, lon) VALUES (?, ?, ?)",
        (address, lat, lon),
    )
    conn.commit()
    return (lat, lon)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8  # Earth radius (miles)
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def passes_location(job: dict, config: dict) -> bool:
    location = (job.get("location") or "").lower()
    return any(entry.lower() in location for entry in config["location_whitelist"])


def effective_applicant_count(applicants_details: dict | None) -> int | None:
    if not applicants_details:
        return None
    kind = applicants_details.get("kind")
    if kind == "exact":
        return applicants_details.get("count")
    if kind == "upper_bound":
        return applicants_details.get("max")
    if kind == "lower_bound":
        return applicants_details.get("min")
    return None


def passes_applicant_cap(job: dict, config: dict) -> bool:
    count = effective_applicant_count(job.get("applicantsDetails"))
    if count is None:
        return True  # missing data does not exclude a job here, but the report points it out
    return count <= config["max_applicants"]


def passes_keywords(job: dict, config: dict) -> bool:
    haystack = f"{job.get('title', '')} {job.get('description', '')}".lower()
    return not any(kw.lower() in haystack for kw in config["exclude_keywords"])


def passes_staffing_agency(job: dict, config: dict) -> bool:
    if not config.get("exclude_staffing_agencies"):
        return True
    company = (job.get("company") or "").lower()
    return not any(agency.lower() in company for agency in config["staffing_agency_blacklist"])


def passes_title(job: dict, config: dict) -> bool:
    # the part in parentheses is optional: "(Senior) Product Builder" is open to senior and non-senior candidates, so it is not a level requirement
    title = re.sub(r"\([^)]*\)", " ", job.get("title") or "")
    for pattern in config.get("exclude_title_patterns", []):
        if re.search(rf"\b{re.escape(pattern)}\b", title, re.IGNORECASE):
            return False
    return True


def passes_visa(job: dict, config: dict) -> bool:
    visa = visa_settings(config)
    if not visa["enabled"]:
        return True
    location = (job.get("location") or "").lower()
    if any(loc.lower() in location for loc in visa["skip_locations"]):
        return True  # the visa rules target US OPT; users do not need sponsorship in these locations
    description = re.sub(r"\s+", " ", job.get("description") or "").lower()
    return not any(p.lower() in description for p in visa["exclude_patterns"])


def passes_unpaid(job: dict, config: dict) -> bool:
    """Unpaid / volunteer jobs: the title contains a keyword (whole-word match, parentheses included), or the description matches exclude_unpaid_patterns (regular expressions)."""
    title = job.get("title") or ""
    for word in config.get("exclude_unpaid_title_words", []):
        if re.search(rf"\b{re.escape(word)}\b", title, re.IGNORECASE):
            return False
    description = job.get("description") or ""
    return not any(re.search(p, description, re.IGNORECASE) for p in config.get("exclude_unpaid_patterns", []))


NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_NUM = r"(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")"
# "5+ years", "3-5 years", "five to seven years", "at least 5 years" and "5 years or more" all take the first number.
YEARS_RE = re.compile(
    _NUM + r"(?:\s*\+|\s*(?:-|–|to)\s*" + _NUM + r")?\s*(?:or more |plus )?years?\b",
    re.IGNORECASE,
)
YEARS_CONTEXT_RE = re.compile(r"experience|exp\b", re.IGNORECASE)
# A phrase like "8+ years across ..." has no "experience" nearby but has a "+", so it counts as a requirement unless company-history wording is around it
YEARS_HISTORY_RE = re.compile(
    r"history|legacy|in business|since|founded|serving|of service|of innovation|of excellence|old\b|anniversary|established|heritage",
    re.IGNORECASE,
)
YEARS_AFTER_WINDOW = 100  # "experience" often ends the same requirement, e.g. "5-7 years' Medical Marketing experience"
YEARS_SOFT_RE = re.compile(r"preferred|nice to have|a plus|bonus|desired", re.IGNORECASE)
MAX_PLAUSIBLE_YEARS = 15  # anything above is treated as company history (e.g. "50 years of experience in the industry"), not a requirement


def _to_int(token: str) -> int:
    return int(token) if token.isdigit() else NUMBER_WORDS[token.lower()]


def required_years(description: str) -> int | None:
    """Return the minimum years explicitly required in the description, or None if there is none.

    When several appear, take the smallest: postings often write alternatives or separate requirements such as "2+ years ... OR 6+ years",
    and taking the largest would exclude wrongly; taking the smallest at worst lets a job through, which the "level and seniority" aspect of Step 5 then judges.
    """
    text = re.sub(r"\s+", " ", description or "")
    found = []
    for m in YEARS_RE.finditer(text):
        before = text[max(0, m.start() - 40):m.start()]
        after = text[m.end():m.end() + YEARS_AFTER_WINDOW]
        has_context = YEARS_CONTEXT_RE.search(before) or YEARS_CONTEXT_RE.search(after)
        has_plus = "+" in m.group(0) and not YEARS_HISTORY_RE.search(before + after[:50])
        if not (has_context or has_plus):
            continue  # no "experience" nearby (and not "N+ years"), so this is not a years requirement
        if YEARS_SOFT_RE.search(re.split(r"[.;]", after)[0][:40]):
            continue  # "5+ years preferred" is only a bonus
        years = _to_int(m.group(1))
        if years <= MAX_PLAUSIBLE_YEARS:
            found.append(years)
    return min(found) if found else None


def passes_min_years(job: dict, config: dict) -> bool:
    limit = config.get("min_years_exclude")
    if not limit:
        return True
    years = required_years(job.get("description"))
    return years is None or years < limit


COMPANY_SUFFIXES ={"inc", "llc", "ltd", "corp", "co", "company"}
DEFAULT_TRACKER_LABELS = {"applied": "Applied", "evaluated_skip": "Evaluated, skipped"}


def tracker_labels(config: dict) -> dict:
    """{status: the word that starts a line of the tracker file}. Defaults are English; config.yaml `tracker_labels` overrides them."""
    labels = {**DEFAULT_TRACKER_LABELS, **(config.get("tracker_labels") or {})}
    bad = [k for k in labels if k not in DEFAULT_TRACKER_LABELS]
    if bad or not all(isinstance(v, str) and v.strip() for v in labels.values()):
        sys.exit(f"config.yaml: tracker_labels may only set {', '.join(DEFAULT_TRACKER_LABELS)} to non-empty text.")
    return labels


def normalize_text(text: str, drop_company_suffix: bool = False) -> str:
    text = re.sub(r"\([^)]*\)", " ", (text or "").lower())
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    if drop_company_suffix:
        words = [w for w in words if w not in COMPANY_SUFFIXES]
    return " ".join(words)


def load_tracker(path: Path, labels: dict) -> list[dict]:
    if not path.exists():
        print(f"  ⚠️ Tracker file {path} not found; skipping the tracker duplicate check.")
        return []
    status_by_label = {word.strip(): status for status, word in labels.items()}
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        label, sep, body = re.split(r"([:：])", line, maxsplit=1) if re.search(r"[:：]", line) else (line, "", "")
        status = status_by_label.get(label.strip())
        if not status:
            continue
        for item in re.split(r"[|｜]", body):  # ASCII or full-width bar
            if " · " not in item:
                continue
            title, company = item.rsplit(" · ", 1)
            entries.append({
                "title": title.strip(),
                "company": company.strip(),
                "title_key": normalize_text(title),
                "company_key": normalize_text(company, drop_company_suffix=True),
                "status": status,
            })
    return entries


def match_tracker(job: dict, tracker: list[dict]) -> tuple[dict | None, list[dict]]:
    """Return (the entry with the same company and title, the list of entries with only the same company)."""
    company_key = normalize_text(job.get("company"), drop_company_suffix=True)
    title_key = normalize_text(job.get("title"))
    same_company = [e for e in tracker if e["company_key"] == company_key]
    exact = next((e for e in same_company if e["title_key"] == title_key), None)
    others = [e for e in same_company if e is not exact]
    return exact, others


def filter_job(job: dict, config: dict) -> str | None:
    """Return None if the job passes, otherwise the exclusion reason."""
    if not passes_location(job, config):
        return "location"
    if not passes_applicant_cap(job, config):
        return "max_applicants"
    if not passes_title(job, config):
        return "exclude_title"
    if not passes_visa(job, config):
        return "exclude_visa"
    if not passes_unpaid(job, config):
        return "exclude_unpaid"
    if not passes_min_years(job, config):
        return "min_years"
    if not passes_keywords(job, config):
        return "exclude_keywords"
    if not passes_staffing_agency(job, config):
        return "staffing_agency"
    return None


def filter_batch(jobs: list[dict], config: dict, conn: sqlite3.Connection) -> dict:
    kept = []
    excluded_by_reason: dict[str, list[dict]] = {}

    center = geocode(config["location_center_address"], conn)
    tracker = load_tracker(Path(config["tracker_path"]), tracker_labels(config)) if config.get("tracker_path") else []

    for job in jobs:
        reason = filter_job(job, config)
        if reason is None:
            exact, others = match_tracker(job, tracker)
            if exact:
                reason = "already_in_tracker"
                job = {**job, "tracker_match": {"title": exact["title"], "status": exact["status"]}}
            else:
                if others:
                    job = {**job, "tracker_company_match": [
                        {"title": e["title"], "status": e["status"]} for e in others
                    ]}
                kept.append(job)
                continue

        excluded_by_reason.setdefault(reason, []).append(job)

        if reason == "location" and center:
            job_coords = geocode(job.get("location", ""), conn)
            if job_coords:
                dist = haversine_miles(*center, *job_coords)
                print(
                    f"  ⚠️ Excluded \"{job.get('title')}\" @ {job.get('location')} "
                    f"(about {dist:.0f} miles from the center, not in the whitelist)"
                )

    return {"kept": kept, "excluded_by_reason": excluded_by_reason}


def process_deduped_file(conn: sqlite3.Connection, config: dict, path: Path) -> None:
    with path.open(encoding="utf-8") as f:
        jobs = json.load(f)

    result = filter_batch(jobs, config, conn)

    print(f"Read: {path}")
    print(f"Input jobs: {len(jobs)}")
    print(f"Passed the filter: {len(result['kept'])}")
    for reason, items in result["excluded_by_reason"].items():
        print(f"Excluded ({reason}): {len(items)}")

    FILTERED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = FILTERED_DIR / path.name
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result["kept"], f, ensure_ascii=False, indent=2)

    print(f"Saved to: {out_path}")

    EXCLUDED_DIR.mkdir(parents=True, exist_ok=True)
    excluded_path = EXCLUDED_DIR / path.name
    excluded_records = [
        {"reason": reason, "job": job}
        for reason, items in result["excluded_by_reason"].items()
        for job in items
    ]
    with excluded_path.open("w", encoding="utf-8") as f:
        json.dump(excluded_records, f, ensure_ascii=False, indent=2)
    print(f"Excluded jobs saved to: {excluded_path}")
    print("-" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH, help="database path (use test.db when testing)")
    parser.add_argument("--only", help="process only this file name (used by the daily run, e.g. raw_20261004.json)")
    args = parser.parse_args()
    config = load_config()
    conn = sqlite3.connect(args.db)
    init_geocode_cache(conn)

    deduped_files = sorted(DEDUPED_DIR.glob("*.json"))
    if args.only:
        deduped_files = [f for f in deduped_files if f.name == args.only]
    if not deduped_files:
        print(f"No .json files found in {DEDUPED_DIR}/. Run scripts/execution-python/dedup.py first.")
    for deduped_path in deduped_files:
        process_deduped_file(conn, config, deduped_path)

    conn.close()
