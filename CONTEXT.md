# CONTEXT.md

## What this project is
A daily pipeline for LinkedIn job search. It fetches new public postings, filters them with hard rules, scores each one against the user's background with Claude, and writes a ranked shortlist as Markdown notes (one folder per job). It only reads public job pages. It does not log in and does not apply for the user.

This repository is the **public, shareable version** of a pipeline the maintainer built for their own job search. The goal is a tool any job seeker can use, not only international students.

## Pipeline
```
Step 1 fetch -> Step 2 slim -> Step 3 dedup -> Step 4 filter -> Step 5 AI scoring -> Step 6 output
```
- **Fetch:** an Apify actor scrapes public LinkedIn job pages for each Boolean query and location.
- **Slim:** keeps the needed fields.
- **Dedup:** SQLite (`jobs.db`) remembers seen jobs by job ID.
- **Filter:** hard rules from `config.yaml` (location whitelist, title patterns, applicant count, required years, unpaid roles, staffing agencies, optional visa phrases).
- **AI scoring:** a small model (triage) scores every job 0 to 5 for each track; a larger model (detail) writes advice for the top jobs; a small model summarizes them.
- **Output:** shortlist folders and a daily summary.
- `run_daily.py` chains the six steps with retries and an optional ntfy phone notification. Side tools: `backfill.py`, `calibrate.py`, `replay_triage.py`, `add_to_shortlist.py`.

## Positioning
- The daily run is a plain script (cron or launchd). It calls the Anthropic API itself and needs no agent at runtime.
- An AI coding agent is the **default way to set up and tune** it: interview the user, write their profiles and config, adjust rules, calibrate scoring. Manual setup (copy the example files and edit) stays available as a fallback.
- Intended users are comfortable with a terminal. They need an Apify token and an Anthropic API key and pay for their own usage.
- Planned agent-facing files (P2): `AGENTS.md` for the user's agent, and a `/setup` skill.

## Architecture decision (read ADR 0002)
One engine codebase plus a user **workspace**. The engine (code, templates, docs) lives in this repo. Each user's data lives in their own workspace folder. The maintainer's own data will become just another workspace. Rejected: a long-lived `public-release` branch and a second copy of the code, because the maintainer's daily scheduled run uses the old folder and two copies drift apart.

## Glossary
- **Job posting:** one scraped LinkedIn job, identified by its job ID for deduplication across runs.
- **Track:** a job direction the user wants (for example Marketing or Project Management), with one profile file and one resume, identified by a single letter. The user chooses how many tracks, at least one. Triage scores every job once per track.
- **Query label:** each Boolean search in `config.yaml` is labeled A, B, C by its order. This is separate from track ids; a query only decides which jobs are fetched, a track decides how they are scored.
- **Best score:** the highest triage score (0 to 5) across a job's tracks. It alone decides whether the job enters the shortlist.
- **Shortlist:** the jobs picked on a run date. Only jobs at or above the shortlist floor are kept; the list is not padded.
- **needs_custom:** a job type worth a customized resume because no base resume covers it well.
- **Daily run:** one full pass of the six steps for a date. At most one successful run per day.
- **Workspace:** the user's own folder holding `config.yaml`, `profiles/`, `resumes/`, `data/`, `jobs.db`, `.env`, `logs/`. It contains no engine code. The same engine can run against any workspace.
- **Engine:** the code, example templates and docs in this repository.

## Current state (update this section as work lands)
- **P0 is done and pushed** to a private GitHub repo (commit `b861ed4`): the engine copied from the maintainer's private repo, personal data removed, MIT license, `config.example.yaml`, `profiles.example/`, `.env.example`, a launchd template, a draft README.
- **Workspace and tracks are done (P1 tasks 6 and 7):** every script resolves a workspace (`--workspace`, then `JOBSEARCH_HOME`, then the current directory) through `scripts/execution-python/workspace.py`, and the track list comes from `tracks:` in `config.yaml`, so the engine can now run the maintainer's four-track setup once their data is moved into a workspace. Still open in P1: English prompts and text, and the README.
- **Prompts, notes, summaries, error messages and code comments are still in Chinese.**
- **The visa feature is optional (P1 task 8):** `visa.enabled` in `config.yaml`, default false. Off means no visa filter, no `visa_blocked` field and no visa text in the prompts. On behaves as before. The wording is still written for US visas (OPT) and is translated in task 9. An old config with top-level `visa_skip_locations` or `exclude_visa_patterns` stops with a message to move them under `visa:`.
- **Two copies of the engine exist for now:** this repo and the maintainer's old private folder, which still runs their daily schedule. Until the migration at the end of P3, engine changes are made here first, and the old folder is frozen.

## Deliberately left out of the first public release
The maintainer's cover-letter skill, Claude hooks and git hooks, the Chinese architecture docs and pipeline canvas (the ADRs in `docs/adr/` are kept but are still Chinese until translated, see roadmap task 9), the Obsidian dashboard, and third-party skills. They are personal workflow or need rewriting. They can be added later.
