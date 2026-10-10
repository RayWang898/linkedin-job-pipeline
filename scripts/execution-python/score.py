"""
Stages 5 and 6: AI scoring and the shortlist output.
Step 5 triage: read data/4_filtered/*.json and give each track a 0-5 score with Haiku (profiles/ provides the background).
Step 6 output: pick the shortlist from the triage, run the detailed review (Sonnet) on the chosen jobs, and write each job to data/7_shortlist/<run date>/<rank>_<company>_<title>/<same name>.md,
and fill recommended_date in jobs.db.
Triage results are cached in the triage_scores table of jobs.db (reused only when jobId, profiles hash and model all match);
detailed-review results are cached in the detail_reviews table.
See docs/adr/ for the design decisions.

Usage: venv/bin/python scripts/execution-python/score.py [--db test.db] [--limit N] [--no-detail]
Needs the environment variable ANTHROPIC_API_KEY (can be set in .env).
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import anthropic
import yaml

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)
import prompts
import texts
from dedup import init_db
from filter import effective_applicant_count, tracker_labels, visa_settings

DATA_DIR = Path("data")
FILTERED_DIR = DATA_DIR / "4_filtered"
SCORED_DIR = DATA_DIR / "6_scored"
SHORTLIST_DIR = DATA_DIR / "7_shortlist"
PROFILES_DIR = Path("profiles")
DB_PATH = Path("jobs.db")
CONFIG_PATH = Path("config.yaml")

TRACK_FIELDS = ("id", "name", "tag", "profile", "resume")


def load_tracks(path: Path = CONFIG_PATH) -> dict:
    """Read the `tracks:` list from config.yaml. Returns {id: {name, tag, file, resume}} in config order."""
    if not path.exists():
        sys.exit(f"{path} not found in {Path.cwd()}. Copy config.example.yaml there, or pass --workspace.")
    with path.open(encoding="utf-8") as f:
        entries = (yaml.safe_load(f) or {}).get("tracks")
    if not isinstance(entries, list) or not entries:
        sys.exit(f"{path}: `tracks:` must be a list with at least one track (see config.example.yaml).")
    tracks = {}
    for n, entry in enumerate(entries, 1):
        missing = [k for k in TRACK_FIELDS if not isinstance(entry, dict) or not str(entry.get(k) or "").strip()]
        if missing:
            sys.exit(f"{path}: tracks entry {n} is missing {', '.join(missing)}.")
        tid = str(entry["id"]).strip()
        if not re.fullmatch(r"[A-Z]", tid):
            sys.exit(f"{path}: track id {tid!r} must be a single uppercase letter.")
        if tid in tracks:
            sys.exit(f"{path}: track id {tid} is used twice.")
        tracks[tid] = {"name": str(entry["name"]).strip(), "tag": str(entry["tag"]).strip(),
                       "file": str(entry["profile"]).strip(), "resume": str(entry["resume"]).strip()}
    return tracks


TRACKS = load_tracks()
with CONFIG_PATH.open(encoding="utf-8") as _f:
    _config = yaml.safe_load(_f)
VISA_ENABLED = visa_settings(_config)["enabled"]
TRACKER_LABELS = tracker_labels(_config)
OUTPUT_LANGUAGE = (_config.get("scoring") or {}).get("output_language") or "English"
P = prompts.load(OUTPUT_LANGUAGE)  # all model-facing text; see prompts.py
TX = texts.load()  # display text for notes, summary file and notification; see texts.py
fill = texts.fill
LEGACY_USAGE_PREFIX = "API 用量："  # summary files written before texts.py used this label; still recognized when re-running a day
SHARED_FILE = "shared.md"
UNKNOWN_APPLICANTS = 10**9  # unknown applicant count sorts last

TRIAGE_TOOL = {
    "name": "submit_triage",
    "description": P["triage_tool_description"],
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "object",
                "properties": {t: {"type": "integer", "minimum": 0, "maximum": 5} for t in TRACKS},
                "required": list(TRACKS),
            },
            "custom_family": {"type": "boolean", "description": P["custom_family_description"]},
            "reason": {"type": "string", "description": P["reason_description"]},
        },
        "required": ["scores", "custom_family", "reason"],
    },
}
if VISA_ENABLED:  # the visa feature adds one field; with it off, the schema has no visa_blocked
    _props = {
        "scores": TRIAGE_TOOL["input_schema"]["properties"]["scores"],
        "custom_family": TRIAGE_TOOL["input_schema"]["properties"]["custom_family"],
        "visa_blocked": {"type": "boolean", "description": P["visa_blocked_description"]},
        "reason": {"type": "string", "description": P["reason_description_visa"]},
    }
    TRIAGE_TOOL["input_schema"]["properties"] = _props
    TRIAGE_TOOL["input_schema"]["required"] = ["scores", "custom_family", "visa_blocked", "reason"]

DETAIL_TOOL = {
    "name": "submit_detail",
    "description": P["detail_tool_description"],
    "input_schema": {
        "type": "object",
        "properties": {
            "resume": {"type": "string", "enum": list(TRACKS), "description": P["resume_description"]},
            "emphasize": {"type": "array", "items": {"type": "string"}, "maxItems": 3, "description": P["emphasize_description"]},
            "gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 3, "description": P["gaps_description"]},
        },
        "required": ["resume", "emphasize", "gaps"],
    },
}

SUMMARY_TOOL = {
    "name": "submit_summary",
    "description": P["summary_tool_description"],
    "input_schema": {
        "type": "object",
        "properties": {"summary": {"type": "string", "description": P["summary_field_description"]}},
        "required": ["summary"],
    },
}

SUMMARY_INSTRUCTIONS = P["summary_instructions"]
RUBRIC = P["rubric"] + ("\n\n" + P["visa_rubric"] if VISA_ENABLED else "")
DETAIL_INSTRUCTIONS = P["detail_instructions"]


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    visa_settings(config)  # fail early on an outdated config
    return config


# ---------- profiles ----------

def load_profiles(directory: Path = PROFILES_DIR) -> dict:
    """Return {'shared': str, 'tracks': {'A': str, ...}, 'hash': str}. triage_hash and detail_hash each cover the profiles plus that stage's rule text (triage rules, detail rules and tool definitions)."""
    needed = {SHARED_FILE: "shared profile", **{info["file"]: f"profile for track {t}" for t, info in TRACKS.items()}}
    for name, what in needed.items():
        if not (directory / name).is_file():
            sys.exit(f"Missing {what}: {directory / name} (in workspace {Path.cwd()}).")
    shared = (directory / SHARED_FILE).read_text(encoding="utf-8")
    tracks = {t: (directory / info["file"]).read_text(encoding="utf-8") for t, info in TRACKS.items()}
    base = json.dumps(TRACKS, ensure_ascii=False, sort_keys=True) + shared + "".join(tracks[t] for t in TRACKS)

    def digest(extra: str) -> str:
        return hashlib.sha256((extra + base).encode("utf-8")).hexdigest()

    return {
        "shared": shared,
        "tracks": tracks,
        "triage_hash": digest(RUBRIC + json.dumps(TRIAGE_TOOL, ensure_ascii=False, sort_keys=True)),
        "detail_hash": digest(DETAIL_INSTRUCTIONS + json.dumps(DETAIL_TOOL, ensure_ascii=False, sort_keys=True)),
    }


def triage_system(profiles: dict) -> list[dict]:
    text = f"{RUBRIC}\n\n<profiles>\n## shared.md\n{profiles['shared']}\n"
    for t, info in TRACKS.items():
        text += f"\n{prompts.heading(P['profiles_track_heading'], id=t, name=info['name'])}\n{profiles['tracks'][t]}\n"
    text += "</profiles>"
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def detail_system(profiles: dict, track: str) -> list[dict]:
    info = TRACKS[track]
    text = (
        f"{DETAIL_INSTRUCTIONS}\n\n<profiles>\n## shared.md\n{profiles['shared']}\n"
        f"\n{prompts.heading(P['detail_track_heading'], id=track, name=info['name'], resume=info['resume'])}\n{profiles['tracks'][track]}\n</profiles>"
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# ---------- model calls (all in one place; to change model or provider, change only this and the config) ----------

class Usage:
    FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

    def __init__(self) -> None:
        self.calls = 0
        self.failures: list[str] = []
        self.tokens = dict.fromkeys(self.FIELDS, 0)

    def add(self, usage) -> None:
        self.calls += 1
        for f in self.FIELDS:
            self.tokens[f] += getattr(usage, f, 0) or 0

    def plain(self) -> str:
        """Fixed English summary for the console and reports; line() is the display text that follows texts.yaml."""
        t = self.tokens
        failed = f"; {len(self.failures)} failed" if self.failures else ""
        return (f"{self.calls} calls{failed}; input {t['input_tokens']}, output {t['output_tokens']}, "
                f"cache write {t['cache_creation_input_tokens']}, cache read {t['cache_read_input_tokens']} tokens")

    def line(self) -> str:
        t = self.tokens
        retries = fill(TX["usage_retries"], n=len(self.failures)) if self.failures else ""
        return fill(TX["usage_line"], calls=self.calls, retries=retries, input=t["input_tokens"], output=t["output_tokens"],
                    cache_write=t["cache_creation_input_tokens"], cache_read=t["cache_read_input_tokens"])


def call_model(client, provider: str, model: str, system: list[dict], user: str,
               tool: dict, validate, usage: Usage, max_tokens: int = 1024,
               force_tool: bool = True) -> dict | None:
    """Call the model and return the tool input; a failure (including a malformed answer) is retried once, then None is returned.

    With force_tool=False the model gets tool_choice auto and calls the tool because the prompt asks it to (some models cannot be forced).
    """
    if provider != "anthropic":
        raise NotImplementedError(f"Provider not implemented: {provider}")
    last_error = None
    for _ in range(2):
        try:
            response = client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]} if force_tool else {"type": "auto"},
                messages=[{"role": "user", "content": user}],
            )
            usage.add(response.usage)
            for block in response.content:
                if block.type == "tool_use":
                    validate(block.input)
                    return block.input
            raise ValueError("The model returned no tool result")
        except Exception as e:  # network, API and format errors are all retried once
            last_error = e
            usage.failures.append(str(e)[:80])
            print(f"  ⚠️ Attempt failed: {str(e)[:120]}")
    print(f"  ⚠️ Call failed (retried once): {last_error}")
    return None


def validate_triage(data: dict) -> None:
    scores = data["scores"]
    for t in TRACKS:
        if not isinstance(scores[t], int) or not 0 <= scores[t] <= 5:
            raise ValueError(f"Invalid score for track {t}: {scores[t]!r}")
    if (not isinstance(data["custom_family"], bool) or not str(data["reason"]).strip()
            or (VISA_ENABLED and not isinstance(data["visa_blocked"], bool))):
        raise ValueError("custom_family, visa_blocked or reason is invalid")


def validate_detail(data: dict) -> None:
    if data["resume"] not in TRACKS:
        raise ValueError(f"resume is not a track id: {data['resume']!r}")
    for key in ("emphasize", "gaps"):
        if not isinstance(data[key], list) or len(data[key]) > 3:
            raise ValueError(f"{key} must be a list of at most 3 items")


def validate_summary(data: dict) -> None:
    if not isinstance(data["summary"], str) or not data["summary"].strip():
        raise ValueError("summary must not be empty")


# ---------- cache tables ----------

def init_score_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS triage_scores (
            job_id TEXT PRIMARY KEY,
            profiles_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            result_json TEXT NOT NULL,
            scored_date TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS detail_reviews (
            job_id TEXT PRIMARY KEY,
            profiles_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            result_json TEXT NOT NULL,
            reviewed_date TEXT NOT NULL
        )
        """
    )
    conn.commit()


def get_cached(conn: sqlite3.Connection, table: str, job_id: str, profiles_hash: str, model: str) -> dict | None:
    row = conn.execute(
        f"SELECT result_json FROM {table} WHERE job_id = ? AND profiles_hash = ? AND model = ?",
        (job_id, profiles_hash, model),
    ).fetchone()
    return json.loads(row[0]) if row else None


def save_cached(conn: sqlite3.Connection, table: str, job_id: str, profiles_hash: str, model: str, result: dict) -> None:
    date_col = "scored_date" if table == "triage_scores" else "reviewed_date"
    conn.execute(
        f"INSERT OR REPLACE INTO {table} (job_id, profiles_hash, model, result_json, {date_col}) VALUES (?, ?, ?, ?, ?)",
        (job_id, profiles_hash, model, json.dumps(result, ensure_ascii=False), date.today().isoformat()),
    )
    conn.commit()


# ---------- Step 5: triage ----------

VISA_HINT = re.compile(
    r"sponsor|\bvisas?\b|work authori[sz]ation|authori[sz]ed to work|legally (?:able|authori[sz]ed)|"
    r"right to work|immigration|\bOPT\b|\bCPT\b|citizen|permanent resident", re.I)


def visa_excerpts(description: str, max_items: int = 3, before: int = 150, after: int = 250) -> list[str]:
    """Extract visa-related passages from the full description (overlapping windows merged), so a triage that only sees the start still reads a visa statement at the end."""
    text = re.sub(r"\s+", " ", description or "")
    windows: list[list[int]] = []
    for m in VISA_HINT.finditer(text):
        start, end = max(0, m.start() - before), min(len(text), m.end() + after)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    return [text[a:b].strip() for a, b in windows[:max_items]]


def triage_user_prompt(job: dict, chars: int, visa_skip_locations: list[str] | None = None) -> str:
    fields = {
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "salary": job.get("salaryDetails"),
        "description": (job.get("description") or "")[:chars],
    }
    location = (job.get("location") or "").lower()
    skip_visa = any(loc.lower() in location for loc in (visa_skip_locations or []))  # e.g. locations in the candidate's home country need no sponsorship
    excerpts = [] if skip_visa or not VISA_ENABLED else visa_excerpts(job.get("description") or "")
    visa_block = ""
    if excerpts:
        visa_block = "\n\n<visa_excerpts>\n" + "\n".join(f"- {e}" for e in excerpts) + "\n</visa_excerpts>"
    return f"<job>\n{json.dumps(fields, ensure_ascii=False, indent=2)}\n</job>{visa_block}\n\n{P['triage_request']}"


def finalize_triage(raw: dict) -> dict:
    """The program derives the best track and needs_custom (ties go to the earlier track in TRACKS).

    When visa_blocked is true every track's score is capped at 1 (the raw scores are kept in scores_raw for later inspection or recovery) and the job does not count as needs-custom.
    """
    visa_blocked = VISA_ENABLED and bool(raw.get("visa_blocked"))
    scores = {t: min(raw["scores"][t], 1) for t in TRACKS} if visa_blocked else raw["scores"]
    best_track = max(TRACKS, key=lambda t: (scores[t], -list(TRACKS).index(t)))
    best_score = scores[best_track]
    result = {
        "scores": scores,
        "best_track": best_track,
        "best_score": best_score,
        "custom_family": raw["custom_family"],
        "needs_custom": raw["custom_family"] and best_score < 4 and not visa_blocked,
        "visa_blocked": visa_blocked,
        "reason": raw["reason"].strip(),
    }
    if visa_blocked:
        result["scores_raw"] = raw["scores"]
    return result


def triage_job(conn, client, config, profiles, usage, job: dict) -> dict | None:
    cfg = config["scoring"]
    model = cfg["triage_model"]
    cached = get_cached(conn, "triage_scores", job["jobId"], profiles["triage_hash"], model)
    if cached:
        return cached
    raw = call_model(client, cfg.get("provider", "anthropic"), model, triage_system(profiles),
                     triage_user_prompt(job, cfg["triage_description_chars"], visa_settings(config)["skip_locations"]),
                     TRIAGE_TOOL, validate_triage, usage)
    if raw is None:
        return None
    result = finalize_triage(raw)
    save_cached(conn, "triage_scores", job["jobId"], profiles["triage_hash"], model, result)
    return result


# ---------- Step 6: pick the shortlist ----------

def sort_key(job: dict) -> tuple:
    applicants = effective_applicant_count(job.get("applicantsDetails"))
    return (-job["triage"]["best_score"], applicants if applicants is not None else UNKNOWN_APPLICANTS)


def select_shortlist(scored: list[dict], size: int, min_score: int = 4, custom_min_score: int = 3) -> dict:
    """Return {'main': main list, 'custom': needs-custom-resume block}.

    Main list: only a best score >= min_score gets in (the shortlist threshold), at most size jobs, never padded with lower scores;
    ties go to the job with fewer applicants. needs_custom jobs go to their own block: they are not held to the main threshold and do not use up main slots,
    but their best score must still be >= custom_min_score (too low a score means only the job type overlaps, which is not worth customizing for).
    """
    custom = sorted((j for j in scored if j["triage"]["needs_custom"]
                     and j["triage"]["best_score"] >= custom_min_score), key=sort_key)
    rest = [j for j in scored if not j["triage"]["needs_custom"]]
    main = sorted((j for j in rest if j["triage"]["best_score"] >= min_score), key=sort_key)[:size]
    return {"main": main, "custom": custom}


def detail_job(conn, client, config, profiles, usage, job: dict) -> dict | None:
    cfg = config["scoring"]
    model = cfg["detail_model"]
    cached = get_cached(conn, "detail_reviews", job["jobId"], profiles["detail_hash"], model)
    if cached:
        return cached
    triage = job["triage"]
    full = {k: job.get(k) for k in ("title", "company", "location", "salaryDetails", "description")}
    user = (
        f"<job>\n{json.dumps(full, ensure_ascii=False, indent=2)}\n</job>\n\n"
        f"<triage>\n{json.dumps(triage, ensure_ascii=False, indent=2)}\n</triage>\n\n"
        + (P["detail_custom_note"] + "\n\n"
           if triage["needs_custom"] else "")
        + P["detail_request"]
    )
    result = call_model(client, cfg.get("provider", "anthropic"), model,
                        detail_system(profiles, triage["best_track"]), user,
                        DETAIL_TOOL, validate_detail, usage, max_tokens=1500,
                        force_tool=False)  # Sonnet 5.5 cannot be forced to use a tool, so the prompt asks it to call the tool instead
    if result is not None:
        save_cached(conn, "detail_reviews", job["jobId"], profiles["detail_hash"], model, result)
    return result


def add_details(conn, client, config, profiles, usage, selection: dict) -> None:
    cfg = config["scoring"]
    targets = selection["main"][: cfg["detail_count"]]
    if cfg.get("detail_custom", True):
        targets = targets + selection["custom"]
    for job in targets:
        job["detail"] = detail_job(conn, client, config, profiles, usage, job)


def summarize_job(client, config, usage, job: dict) -> str | None:
    cfg = config["scoring"]
    full = {k: job.get(k) for k in ("title", "company", "location", "description")}
    user = f"<job>\n{json.dumps(full, ensure_ascii=False, indent=2)}\n</job>\n\n{P['summary_request']}"
    system = [{"type": "text", "text": SUMMARY_INSTRUCTIONS}]
    result = call_model(client, cfg.get("provider", "anthropic"), cfg["summary_model"], system, user,
                        SUMMARY_TOOL, validate_summary, usage, max_tokens=500)
    return result["summary"].strip() if result else None


def add_summaries(client, config, usage, selection: dict, day_dir: Path) -> None:
    """Add a SUMMARY to each shortlisted job. A SUMMARY already in the existing .md is kept as it is (including your edits) and the model is not called; on failure nothing is shown and the next rerun fills it in."""
    for job in selection["main"] + selection["custom"]:
        folder = find_job_folder(day_dir, job["jobId"]) if day_dir.exists() else None
        md = folder / f"{folder.name}.md" if folder else None
        job["summary"] = (read_summary(md) if md and md.exists() else "") or summarize_job(client, config, usage, job)


def mark_recommended(conn: sqlite3.Connection, picks: list[dict]) -> None:
    conn.executemany(
        "UPDATE jobs SET recommended_date = ? WHERE job_id = ?",
        [(date.today().isoformat(), j["jobId"]) for j in picks],
    )
    conn.commit()


# ---------- Step 6: output ----------

def _count(path: Path) -> int | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return len(json.load(f))


PRE_STAGES = (("stage_fetched", "2_slim"), ("stage_deduped", "3_deduped"), ("stage_filtered", "4_filtered"))


def pipeline_counts(name: str, data_dir: Path = DATA_DIR) -> list[tuple[str, int]]:
    counts = ((TX[key], _count(data_dir / folder / name)) for key, folder in PRE_STAGES)
    return [(label, n) for label, n in counts if n is not None]


def pipeline_stats(name: str, data_dir: Path = DATA_DIR) -> list[str]:
    """Per-day pipeline counts computed from each step's output file of the same name; steps with no file are skipped."""
    lines = []
    for label, n in pipeline_counts(name, data_dir):
        lines.append(fill(TX["stat_line"], label=label, n=n))
    excluded_path = data_dir / "5_excluded" / name
    if excluded_path.exists():
        with excluded_path.open(encoding="utf-8") as f:
            reasons = Counter(r["reason"] for r in json.load(f))
        if reasons:
            items = TX["stat_excluded_separator"].join(fill(TX["stat_excluded_item"], reason=k, n=v) for k, v in reasons.most_common())
            lines.append(fill(TX["stat_excluded"], items=items))
    return lines


def salary_text(salary) -> str:
    if isinstance(salary, dict):
        return salary.get("text") or TX["salary_undisclosed"]
    return salary or TX["salary_undisclosed"]


def slug(text: str, limit: int = 60) -> str:
    text = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", text or "")
    return re.sub(r"\s+", "-", text).strip("-")[:limit].strip("-")


def yaml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)  # a JSON string is also valid YAML


def read_frontmatter(path: Path) -> dict:
    """Read the simple key: value pairs of the --- block at the top of a .md; only used to recover jobId and the user-owned status."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    props = {}
    for line in text[4:].split("\n---", 1)[0].splitlines():
        key, sep, val = line.partition(":")
        if sep and not line.startswith(" "):
            props[key.strip()] = val.strip().strip('"')
    return props


def read_list_property(path: Path, key: str) -> list[str]:
    """Return all values of a list property. Obsidian stores it as a list (multitext in types.json) while hand-written ones are often one string; both are read and returned as a list."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return []
    lines = text[4:].split("\n---", 1)[0].splitlines()
    for n, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            inline = line.partition(":")[2].strip().strip('"')
            if inline:
                return [inline]
            items = []
            for item in lines[n + 1:]:
                if not item.startswith("  - "):
                    break
                items.append(item[4:].strip().strip('"'))
            return items
    return []


def read_status(path: Path) -> list[str]:
    return read_list_property(path, "status")


def read_interest(path: Path) -> str:
    """Return the raw text after the colon on the interest (checkbox) line (blank, true or false), put back unchanged when the .md is rewritten."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return ""
    for line in text[4:].split("\n---", 1)[0].splitlines():
        if line.startswith("interest:"):
            return line.partition(":")[2].strip()
    return ""


def list_lines(key: str, values: list[str] | str) -> list[str]:
    """List properties are always written as lists, matching Obsidian's multitext format, so edits made in Obsidian do not change the format. An empty list is written as just `key:`."""
    items = [values] if isinstance(values, str) else values
    return [f"{key}:"] + [f"  - {s}" if re.fullmatch(r"[\w][\w /-]*", s) else f"  - {yaml_value(s)}" for s in items]


def status_lines(status: list[str] | str) -> list[str]:
    return list_lines("status", status)


def read_user_note(path: Path) -> str:
    """Return the raw text after the colon on the user note line (quotes included), put back unchanged when the .md is rewritten so nothing you wrote is lost."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return ""
    for line in text[4:].split("\n---", 1)[0].splitlines():
        if line.startswith("user note:"):
            return line.partition(":")[2].strip()
    return ""


def read_summary(path: Path) -> str:
    """Return the text between "## SUMMARY" and the next ## heading in a .md, put back unchanged when the .md is rewritten."""
    m = re.search(r"^## SUMMARY\n\n(.*?)(?=\n## |\Z)", path.read_text(encoding="utf-8"), re.S | re.M)
    return m.group(1).strip() if m else ""


def find_job_folder(day_dir: Path, job_id: str) -> Path | None:
    for md in day_dir.glob("*/*.md"):
        if read_frontmatter(md).get("jobId") == job_id:
            return md.parent
    return None


def job_frontmatter(rank: int | None, job: dict, run_date: str, scraped_date: str, status: list[str] | str,
                    user_note: str = "", interest: str = "") -> list[str]:
    tr = job["triage"]
    detail = job.get("detail")
    tags = [f"track/{tr['best_track']}-{TRACKS[tr['best_track']]['tag']}"]
    if tr["needs_custom"]:
        tags.append("needs-custom")
    posted = job.get("postedDate")
    source = job.get("source", "linkedin")  # non-LinkedIn jobs have no jobId, applicant count or daily rank
    props = [
        ("title", job["title"]),
        ("company", job["company"]),
        ("source", source),
        ("rank", rank),
        ("date_recommended", run_date),
        ("date_posted", posted),
        ("date_scraped", scraped_date),
        ("best_track", tr["best_track"]),
        ("best_score", tr["best_score"]),
        *((f"score_{t}", tr["scores"][t]) for t in TRACKS),
        ("resume", job.get("resume_file") or (TRACKS[detail["resume"]]["resume"] if detail else None)),  # resume_file: a custom resume set by hand
        ("location", job.get("location")),
        ("salary", salary_text(job.get("salaryDetails"))),
        ("applicants", (job.get("applicantsDetails") or {}).get("text", TX["applicants_unknown"]) if source == "linkedin" else None),
        ("url", job.get("jobUrl")),
        ("jobId", job["jobId"] if source == "linkedin" else None),
    ]
    lines = ["---"]
    for key, val in props:
        if val is None:
            continue
        # dates must be written as unquoted YYYY-MM-DD for Obsidian to treat them as date fields
        lines.append(f"{key}: {val if key.startswith('date_') else yaml_value(val)}")
    lines += status_lines(status)
    lines.append(f"interest: {interest}".rstrip())  # ticked by the user (checkbox: ticked = interested, blank = no opinion); even when empty the field must exist so Obsidian can show it
    lines.append("tags:")
    lines += [f"  - {t}" for t in tags]
    lines.append(f"user note: {user_note}".rstrip())  # filled in by the user; even when empty the field must exist so Obsidian can show it
    lines.append("---")
    return lines


def format_job_note(rank: int | None, job: dict, run_date: str, scraped_date: str, status: list[str] | str,
                    user_note: str = "", interest: str = "") -> str:
    tr = job["triage"]
    best = TRACKS[tr["best_track"]]
    score_line = TX["scores_separator"].join(f"{t} {tr['scores'][t]}" for t in TRACKS)
    lines = job_frontmatter(rank, job, run_date, scraped_date, status, user_note, interest)
    lines += [
        "",
        f"# {job['title']} — {job['company']}",
        "",
        fill(TX["note_best"], track=tr["best_track"], name=best["name"], score=tr["best_score"], scores=score_line),
        fill(TX["note_link"], url=job.get("jobUrl")) if job.get("jobUrl") else TX["note_no_link"],
        fill(TX["note_reason"], reason=tr["reason"]),
    ]
    for e in job.get("tracker_company_match") or []:
        label = TRACKER_LABELS[e["status"]]
        lines.append(fill(TX["note_same_company"], title=e["title"], label=label))
    if job.get("summary"):
        lines += ["", "## SUMMARY", "", job["summary"]]
    detail = job.get("detail")
    if detail:
        lines += [
            "",
            TX["note_detail_heading"],
            "",
            fill(TX["note_detail_resume"], track=detail["resume"], name=TRACKS[detail["resume"]]["name"], resume=TRACKS[detail["resume"]]["resume"]),
            fill(TX["note_detail_emphasize"], items=TX["list_separator"].join(detail["emphasize"])),
            fill(TX["note_detail_gaps"], items=TX["list_separator"].join(detail["gaps"])),
        ]
    lines += ["", TX["note_description_heading"], "", job.get("description") or TX["note_no_description"], ""]
    return "\n".join(lines)


def track_table(scored: list[dict], selection: dict, pre_counts: list[tuple[str, int]]) -> list[str]:
    """Per-track counts at each stage. Fetch to filter are not scored yet, so they only get a total."""
    picks = selection["main"] + selection["custom"]
    # (label, test, jobs, add_up): "track score >= N" can count a job under several tracks, so it has no total
    stages = [
        (TX["table_triage_best"], lambda j, t: j["triage"]["best_track"] == t, scored, True),
        (TX["table_min3"], lambda j, t: j["triage"]["scores"][t] >= 3, scored, False),
        (TX["table_min4"], lambda j, t: j["triage"]["scores"][t] >= 4, scored, False),
        (TX["table_shortlist_best"], lambda j, t: j["triage"]["best_track"] == t, picks, True),
        (TX["table_detail_best"], lambda j, t: j["triage"]["best_track"] == t, [j for j in picks if j.get("detail")], True),
        (TX["table_detail_resume"], lambda j, t: j["detail"]["resume"] == t, [j for j in picks if j.get("detail")], True),
    ]
    lines = [f"| {TX['table_stage']} | " + " | ".join(f"{t} {TRACKS[t]['name']}" for t in TRACKS) + f" | {TX['table_total']} |",
             "| --- |" + " --- |" * (len(TRACKS) + 1)]
    for label, n in pre_counts:
        lines.append(f"| {label} |" + " — |" * len(TRACKS) + f" {n} |")
    for label, test, jobs, add_up in stages:
        counts = [sum(1 for j in jobs if test(j, t)) for t in TRACKS]
        total = len(jobs) if add_up else "—"
        lines.append(f"| {label} | " + " | ".join(map(str, counts)) + f" | {total} |")
    return lines


def read_stage_jobs(name: str, folder: str, data_dir: Path = DATA_DIR) -> list[dict]:
    path = data_dir / folder / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def query_table(name: str, scored: list[dict], selection: dict, query_names: list[str]) -> list[str]:
    """Jobs found by each search query at each stage. A job found by several queries counts once per query, so rows add up to more than the total."""
    slim = read_stage_jobs(name, "2_slim")
    if not any(j.get("matched_queries") for j in slim):
        return []  # older data did not record which query found a job
    labels = sorted({q for j in slim for q in j["matched_queries"]})
    stages = [
        (TX["query_stage_fetched"], slim),
        (TX["query_stage_deduped"], read_stage_jobs(name, "3_deduped")),
        (TX["query_stage_filtered"], read_stage_jobs(name, "4_filtered")),
        (TX["query_stage_triage4"], [j for j in scored if j["triage"]["best_score"] >= 4]),
        (TX["query_stage_shortlist"], selection["main"] + selection["custom"]),
    ]
    def hit(job, label):
        return label in (job.get("matched_queries") or [])
    lines = [f"| {TX['query_table_query']} | " + " | ".join([TX["query_table_unique"]] + [s for s, _ in stages]) + " |",
             "| --- |" + " --- |" * (len(stages) + 1)]
    for label in labels:
        i = ord(label) - ord("A")
        title = f"{label} {query_names[i]}" if i < len(query_names) else label
        unique = sum(1 for j in slim if j["matched_queries"] == [label])
        lines.append(f"| {title} | {unique} | " + " | ".join(
            str(sum(1 for j in jobs if hit(j, label))) for _, jobs in stages) + " |")
    lines.append(f"| {TX['query_table_all']} | — | " + " | ".join(str(len(jobs)) for _, jobs in stages) + " |")
    return lines


def format_summary(run_date: str, stats: list[str], usages: dict[str, Usage], selection: dict,
                   scored: list[dict], pre_counts: list[tuple[str, int]],
                   queries_table: list[str], previous_usage_lines: list[str] | None = None) -> str:
    lines = [fill(TX["summary_title"], date=run_date), ""]
    lines += [fill(TX["summary_shortlist"], main=len(selection["main"]), custom=len(selection["custom"]))]
    lines += [f"- {s}" for s in stats]
    blocked = [j for j in scored if j["triage"].get("visa_blocked")]
    if blocked:  # the visa judgment lowers scores silently, so list the jobs to make wrong calls easy to spot
        lines += [fill(TX["summary_visa_blocked"], n=len(blocked))]
        lines += [fill(TX["summary_visa_item"], company=j["company"], title=j["title"], reason=j["triage"]["reason"]) for j in blocked[:30]]
    lines += ["", TX["summary_tracks_heading"], ""] + track_table(scored, selection, pre_counts) + [""]
    if queries_table:
        lines += [TX["summary_queries_heading"], ""] + queries_table + [""]
    if previous_usage_lines and not any(u.calls for u in usages.values()):
        lines += previous_usage_lines  # a rerun served entirely from cache keeps the usage the day really cost
    else:
        lines += [fill(TX["usage_item"], label=TX[f"usage_{key}"], prefix=TX["usage_prefix"], usage=u.line()) for key, u in usages.items()]
    lines.append("")
    return "\n".join(lines)


def write_shortlist(selection: dict, scored: list[dict], run_date: str, scraped_date: str,
                    stats: list[str], usages: dict[str, Usage],
                    pre_counts: list[tuple[str, int]], queries_table: list[str],
                    root: Path = SHORTLIST_DIR, folder_name: str | None = None,
                    summary_name: str = "_summary.md") -> Path:
    """One folder per job. If a folder for the same jobId exists only its .md is rewritten; it is not renamed and the files in it are not touched."""
    day_dir = root / (folder_name or run_date)
    day_dir.mkdir(parents=True, exist_ok=True)
    for rank, job in enumerate(selection["main"] + selection["custom"], 1):
        folder = find_job_folder(day_dir, job["jobId"])
        status = "backlog"
        user_note = ""
        interest = ""
        if folder:
            md = folder / f"{folder.name}.md"
            if md.exists():
                status = read_status(md) or status
                user_note = read_user_note(md)
                interest = read_interest(md)
        else:
            name = f"{rank:02d}_{slug(job['company'])}_{slug(job['title'])}"
            folder = day_dir / name
            if folder.exists():  # a different job with the same company and title
                folder = day_dir / f"{name}_{job['jobId']}"
            folder.mkdir()
        (folder / f"{folder.name}.md").write_text(
            format_job_note(rank, job, run_date, scraped_date, status, user_note, interest), encoding="utf-8")
    summary_path = day_dir / summary_name
    previous = []
    if summary_path.exists():
        previous = [l for l in summary_path.read_text(encoding="utf-8").splitlines() if TX["usage_prefix"] in l or LEGACY_USAGE_PREFIX in l]
    summary_path.write_text(
        format_summary(run_date, stats, usages, selection, scored, pre_counts, queries_table, previous), encoding="utf-8")
    return day_dir


# ---------- main flow ----------

def process_filtered_file(conn, client, config, profiles, path: Path, limit: int | None, detail: bool,
                           stats_path: Path | None = None, folder_suffix: str = "",
                           shortlist_size: int | None = None, summary_name: str = "_summary.md") -> None:
    """folder_suffix: a suffix on the shortlist folder name (e.g. backfill -> 2026-10-05-backfill); date_recommended stays the run date."""
    with path.open(encoding="utf-8") as f:
        jobs = json.load(f)
    if limit:
        jobs = jobs[:limit]

    usages = {"triage": Usage(), "detail": Usage(), "summary": Usage()}
    scored = []
    for job in jobs:
        triage = triage_job(conn, client, config, profiles, usages["triage"], job)
        if triage is None:
            print(f"  ⚠️ Skipping {job['jobId']} \"{job.get('title')}\" (triage failed; it will be rescored next time)")
            continue
        scored.append({**job, "triage": triage})

    selection = select_shortlist(scored, shortlist_size or config["scoring"]["shortlist_size"],
                                 config["scoring"].get("shortlist_min_score", 4),
                                 config["scoring"].get("custom_min_score", 3))
    folder_name = date.today().isoformat() + (f"-{folder_suffix}" if folder_suffix else "")
    if detail:
        add_details(conn, client, config, profiles, usages["detail"], selection)
    add_summaries(client, config, usages["summary"], selection, SHORTLIST_DIR / folder_name)

    SCORED_DIR.mkdir(parents=True, exist_ok=True)
    SHORTLIST_DIR.mkdir(parents=True, exist_ok=True)
    with (SCORED_DIR / path.name).open("w", encoding="utf-8") as f:
        json.dump(scored, f, ensure_ascii=False, indent=2)
    run_date = date.today().isoformat()
    m = re.search(r"(\d{4})(\d{2})(\d{2})", path.stem)
    scraped_date = "-".join(m.groups()) if m else run_date
    shortlist_path = write_shortlist(
        selection, scored, run_date, scraped_date, pipeline_stats(path.name), usages,
        pipeline_counts(path.name),
        query_table(path.name, scored, selection, config["apify"].get("query_names", [])),
        folder_name=folder_name, summary_name=summary_name)
    mark_recommended(conn, selection["main"] + selection["custom"])

    if stats_path:
        picks = selection["main"] + selection["custom"]
        stats = {
            "main": len(selection["main"]),
            "custom": len(selection["custom"]),
            "by_track": dict(Counter(j["triage"]["best_track"] for j in picks)),
            "tokens": {f: sum(u.tokens[f] for u in usages.values()) for f in Usage.FIELDS},
            "failures": sum(len(u.failures) for u in usages.values()),
        }
        stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Read: {path}")
    print(f"Input jobs: {len(jobs)}, triaged: {len(scored)}")
    print(f"Shortlist: main {len(selection['main'])}, needs custom resume {len(selection['custom'])}")
    for label, u in usages.items():
        print(f"{label} usage: {u.plain()}")
    print(f"Saved to: {SCORED_DIR}/{path.name}, {shortlist_path}")
    print("-" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH, help="database path (use test.db when testing)")
    parser.add_argument("--limit", type=int, help="score at most this many jobs per file (for testing)")
    parser.add_argument("--no-detail", action="store_true", help="skip the detailed review (saves tokens)")
    parser.add_argument("--stats-json", type=Path, help="write the shortlist stats (counts, per track, token usage) as JSON, used by the daily run")
    parser.add_argument("--only", help="process only this file name (used by the daily run, e.g. raw_20261004.json)")
    parser.add_argument("--folder-suffix", default="", help="suffix of the shortlist folder, e.g. backfill (for backfills)")
    parser.add_argument("--shortlist-size", type=int, help="cap of the main list (overrides config; for backfills)")
    parser.add_argument("--summary-name", default="_summary.md", help="file name of the stats file (each backfill batch uses a different one so none is overwritten)")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not found. Set it in .env or export it as an environment variable.")
    try:
        profiles = load_profiles()
    except FileNotFoundError as e:
        sys.exit(f"Profile file not found: {e.filename}. Create profiles/ first.")

    config = load_config()
    client = anthropic.Anthropic()
    conn = init_db(args.db)
    init_score_tables(conn)

    files = sorted(FILTERED_DIR.glob("*.json"))
    if args.only:
        files = [f for f in files if f.name == args.only]
    if not files:
        print(f"No .json files found in {FILTERED_DIR}/. Run scripts/execution-python/filter.py first.")
    for filtered_path in files:
        process_filtered_file(conn, client, config, profiles, filtered_path, args.limit, not args.no_detail,
                              args.stats_json, args.folder_suffix, args.shortlist_size, args.summary_name)
    conn.close()
