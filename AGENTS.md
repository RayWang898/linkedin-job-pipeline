# AGENTS.md

For an AI coding agent that is helping a **user** install, run and tune this job-search pipeline. (If you are changing the engine's own code, read `CLAUDE.md` instead.)

Read `README.md` first for what the tool does, what it costs and its limits. This file tells you how to work with it safely.

## The two folders

- **The engine** is this repository: the scripts in `scripts/execution-python/`, example files (`config.example.yaml`, `profiles.example/`, `.env.example`, `prompts.example/`, `texts.example/`) and docs. Do not put the user's data here and do not edit the scripts to change how a user's setup behaves.
- **The workspace** is a separate folder that belongs to the user. Everything personal and everything you tune lives there:

| Path in the workspace | What it is |
| --- | --- |
| `config.yaml` | Locations, search queries, filter rules, tracks, scoring settings, visa and tracker options |
| `profiles/shared.md`, `profiles/<track>.md` | The user's background and what they want per track |
| `.env` | `APIFY_TOKEN`, `ANTHROPIC_API_KEY`, optional `NTFY_TOPIC` |
| `prompts.yaml`, `texts.yaml` | Optional overrides of the model prompts and of the note and summary wording |
| `resumes/` | The user's resumes (the program only shows their file names) |
| `data/` | `1_raw` to `7_shortlist`: every stage's output. `data/7_shortlist/<date>/` is what the user reads |
| `jobs.db` | SQLite: jobs already seen, cached scores, one row per daily run |
| `logs/`, `feedback/` | Daily logs and the calibration lists |

Every script finds the workspace in this order: `--workspace PATH`, the `JOBSEARCH_HOME` environment variable, the current directory. Run commands from inside the workspace or pass `--workspace`. The engine needs its dependencies installed once (`python3 -m venv venv && venv/bin/pip install -r requirements.txt` in the engine folder, Python 3.11+).

## Rules

1. **Anything that calls Apify or Anthropic costs the user money. Ask first.** That is `run_daily.py`, `fetch.py`, `backfill.py`, `score.py`, `replay_triage.py` and `add_to_shortlist.py`. State what you are about to run and roughly what it costs (see the Cost section of `README.md`) and wait for a yes. `filter.py`, `dedup.py` and `calibrate.py` read local files only.
2. **Test small and in isolation.** First runs use `--max-jobs 5 --db test.db --no-notify`. The test database keeps the real `jobs.db` free of test rows (a job seen in a test would otherwise be skipped later as "already seen").
3. **Never print, copy or commit the contents of `.env`.** Check that keys are set without showing them. Keep the workspace out of any git repository you may commit from, or make sure `.env`, `config.yaml`, `profiles/`, `resumes/`, `data/` and `*.db` are ignored.
4. **Edit the workspace, not the engine.** Behavior is changed through `config.yaml`, `profiles/`, `prompts.yaml` and `texts.yaml`. If something cannot be done that way, tell the user instead of patching the scripts.
5. **Show the user the change before writing it** when it touches their profiles, their queries or their filter rules. These decide what they will see, and only they know what they want.
6. **Do not guess the user's background.** Profiles hold facts about a person (employers, numbers, work authorization). Ask, or use only what they wrote or gave you. Never invent experience.
7. **Do not apply to jobs, message recruiters or log in to LinkedIn for the user.** The tool only reads public pages.

## Commands

All of them run with the engine's Python, for example `"$ENGINE/venv/bin/python" "$ENGINE/scripts/execution-python/run_daily.py" ...` from the workspace.

| Command | What it does | Spends money |
| --- | --- | --- |
| `run_daily.py [--force] [--no-catch-up] [--no-notify] [--max-jobs N] [--db FILE]` | The whole pipeline for today. A day that already succeeded is skipped; a day that failed is resumed; earlier failed days (up to 3 back) are finished first. `--force` runs today again with a new fetch after moving today's earlier files to `data/_replaced/`. Reads keys from the workspace `.env` | yes |
| `fetch.py [--max-jobs N] [--from-samples]` | Step 1 and 2 only | yes (unless `--from-samples`) |
| `dedup.py`, `filter.py`, `score.py` | Steps 3, 4 and 5-6 on today's files (`--only raw_YYYYMMDD.json`, `--db FILE`). `score.py` also has `--limit N`, `--no-detail` and `--run-date YYYY-MM-DD` | `score.py` only |
| `calibrate.py [YYYYMMDD] [--margin N]` | Lists jobs that were nearly excluded or nearly shortlisted | no |
| `replay_triage.py --file raw_<name>.json` | Rescores a sample of already-scored jobs to measure agreement; writes to `feedback/` | yes |
| `backfill.py [--days N] [--dry-run]` | One-off fetch of the past N days into its own shortlist folder | yes (`--dry-run` does not) |
| `add_to_shortlist.py <jobId> \| --file jd.txt --title ... --company ...` | Scores and files a single posting | yes |

Re-running `score.py` on a day whose jobs are already scored costs nothing: scores are cached in `jobs.db`, and a note's `status`, `interest` and `user note` fields are kept.

## Setting up a new user

Do this in order and stop to confirm at each numbered step.

1. **Environment.** Python 3.11+, the engine's dependencies installed, a workspace folder created, `config.example.yaml` copied to `config.yaml`, `profiles.example/` copied to `profiles/`, `.env.example` copied to `.env`. Ask the user to paste their `APIFY_TOKEN` and `ANTHROPIC_API_KEY` into `.env` themselves (see rule 3).
2. **Interview, then write `profiles/`.** The scoring is only as good as these files. Ask about: work authorization and location; education; years and kinds of experience; two to six accomplishments with a number each; tools and skills they can defend in an interview; what kinds of jobs they want (these become the tracks); what they would never do; which job types are a good fit but need a custom resume. Put the shared facts in `shared.md` (keep its section "Job types worth a custom resume (needs_custom)") and one file per track. Use `profiles.example/` as the template. Show each file to the user before saving it.
3. **Tracks.** One entry per direction in `tracks:` in `config.yaml`: a unique capital letter `id`, a `name`, a `tag`, the `profile` file name and the `resume` file name. At least one. Two to four is typical; more tracks make every triage call longer.
4. **Search and filters.** `apify.locations`, `apify.queries` (Boolean strings: quoted phrases, `OR` inside parentheses, `AND` for a context word) with matching `apify.query_names`, `location_whitelist`, `max_applicants`, `exclude_title_patterns`, `min_years_exclude`, `staffing_agency_blacklist`. Ask what seniority they want; the title and years filters are how it is enforced. Query labels (A, B, C ...) follow the order of `queries` and are separate from track ids.
5. **Visa.** Only if the user needs an employer to sponsor a work visa: `visa.enabled: true` and review `visa.exclude_patterns`. Leave it off otherwise.
6. **Small test run** with the flags from rule 2. Open `data/7_shortlist/<today>/_summary.md` and a note. If nothing was shortlisted that can be normal with 5 jobs per query; look at `data/4_filtered`, `data/5_excluded` and `data/6_scored` to see where jobs dropped out.
7. **Full run, then schedule.** After the user approves the cost, run once without `--max-jobs`, then set up a daily schedule (the README has a macOS launchd template and mentions cron). Ask whether they want the phone notification (`NTFY_TOPIC`).

## Tuning an existing setup

Start from evidence, not from guesses.

- **Too many weak jobs on the shortlist**, or jobs the user keeps skipping: ask for a few examples and the reason they skipped each (level, years, role, location, company). Then change the matching lever: tighten `exclude_title_patterns` or `min_years_exclude` (hard rules, no model cost); sharpen "what I do not want" in the profiles; raise `scoring.shortlist_min_score`; lower `scoring.shortlist_size`.
- **Good jobs missing**: run `calibrate.py` for the day and read "just over the applicant cap", "excluded by title" and "passed triage but missed the shortlist". Loosen the rule that removed them, or the threshold.
- **Cost too high**: see the table in the README (fewer fetched jobs, fewer or shorter profiles, `scoring.detail_count`, a cheaper `scoring.triage_model`).
- **Changing a prompt, a profile or the triage model changes scores.** Before relying on the change, use `replay_triage.py` on a day that is already scored and compare against a rerun of the old setup, not against the stored scores. In the project's own test, scoring the same jobs twice with the same model gave the same best score for about 84% of them, so differences of that size are noise.
- **Language.** `scoring.output_language` sets the language of the model's reasons and advice. `prompts.yaml` replaces the instructions; `texts.yaml` replaces the wording of notes, the summary file and the notification. Examples in `prompts.example/` and `texts.example/`.

## When something fails

- Read the newest file in `logs/` first; the last lines name the step that failed.
- `environment: APIFY_TOKEN not found` (or `ANTHROPIC_API_KEY`): the key is missing in the workspace `.env`, or the command ran from a folder that is not the workspace.
- `config.yaml ... tracks` or `track id ...` messages: fix the `tracks:` list as the message says. `Missing profile for track X`: the file named in `profile:` is not in `profiles/`.
- `visa_skip_locations, exclude_visa_patterns moved under visa:`: an older config; move them under a `visa:` section as `skip_locations` and `exclude_patterns`.
- `prompts.yaml: unknown names` or `texts.yaml: unknown names`: a key is misspelled; the message lists the valid ones.
- **A day failed after retries (the user got a failure notification).** Running `run_daily.py` again is safe and does the right thing: the daily run records which steps finished for the day (`steps_done` in the `daily_runs` table) and a rerun resumes after the last finished step, without fetching again and without paying again for jobs already scored. If nobody reruns it, the next daily run first finishes any of the last 3 days that failed after fetching ("Catching up ..." in the log, shortlist in that day's own folder) and then does today. To resume without a full run: `score.py --only raw_<YYYYMMDD>.json --run-date <YYYY-MM-DD>`.
  - `Failed: resume: ... is recorded as done but data/... is missing` means a file of an unfinished day was deleted. Do not delete `jobs.db` rows to work around it without asking the user: tell them which day and file.
  - A day that failed before the fetch finished left no jobs marked as seen, so it simply starts over.
- Fetch returns fewer jobs than expected or none: the third-party Apify actor may have changed or the queries are too narrow; check the Apify console for the run before changing anything else.

## What not to do

- Do not run the full pipeline "to see what happens" without asking (rule 1).
- Do not delete `jobs.db`: it holds the seen-jobs history, the score cache and the daily run records. Deleting it makes every job look new and re-bills every score.
- Do not edit files under `data/` by hand except a note's `status`, `interest` and `user note`, which are the user's to change.
- Do not paste real postings, resumes or keys into issues, commits or shared documents.
