---
name: setup
description: Set up the job-search pipeline for a user. Interviews them, then writes their profiles and config.yaml in their workspace, checks the setup and runs a small test. Use when the user types /setup, asks to set up, configure or get started with this pipeline, or wants their profiles or search queries (re)written.
---

# /setup

Walk a user from a fresh clone to a working pipeline. You do the writing; the user supplies the facts and approves every file.

Before anything else, read `AGENTS.md` in the repository root and follow its rules. The ones that matter most here: anything that calls Apify or Anthropic costs money, so ask first; never print or commit `.env`; edit the user's workspace, never the engine; never invent facts about the user.

Read the files in `references/` when a step points to them; do not load them all up front:

- `references/interview.md`: the questions for each part and how to turn the answers into file text
- `references/profile-quality.md`: what a profile that scores well looks like, and a checklist to run before saving
- `references/queries-and-filters.md`: how to write search queries, the location whitelist and the filter rules

## How to run the interview

- Talk in the user's language. Ask at most three questions at a time, and say why a question matters in a few words. Offer an example or a default so they can answer in a line.
- Do not interrogate. After a first answer, draft the text yourself, show it, and ask what is wrong or missing. Drafts beat questionnaires.
- The fastest path is a resume: ask for the file path or pasted text, read it only after the user agrees, and draft from it. Whatever you infer rather than read, mark `(unconfirmed)` in the draft and clear every marker with the user before saving.
- Write down only what the user told you or showed you. If you do not know a number, ask for it or leave it out.
- The user may say "skip" or "later" to anything. Leave a sensible default and note it at the end.

## Steps

Do these in order. Stop for the user's go-ahead at the end of steps 2 to 7.

### 0. Preflight

- Find the engine folder (the repository this skill lives in). Check `python3 --version` is 3.11 or newer. If `venv/` is missing, offer to run `python3 -m venv venv && venv/bin/pip install -r requirements.txt` in the engine folder.
- Say in two sentences what you are about to do, that it is a real conversation (not a form), and that nothing is fetched or paid for until the test run in step 8.

### 1. Workspace

- Ask where the user's data should live (suggest `~/jobsearch`, outside this repository). If the folder already has a `config.yaml` or `profiles/`, ask whether to update them or start over. Never overwrite a file without asking.
- Create it with `"$ENGINE/venv/bin/python" "$ENGINE/scripts/execution-python/init.py" <folder>`. It copies in `config.yaml`, `profiles/` and `.env` from the examples, adds a `.gitignore`, and never overwrites a file that is already there.
- Ask the user to paste their `APIFY_TOKEN` and `ANTHROPIC_API_KEY` into `.env` themselves. Check afterwards that both are non-empty without printing them (for example `grep -c '^APIFY_TOKEN=.' .env`).

### 2. Background (`profiles/shared.md`)

Read `references/interview.md` part A and `references/profile-quality.md`. Collect level, education, languages, work authorization, the experience that goes on any resume (with numbers), skills, the job types that fit but need a custom resume, and the hard no's. Draft `shared.md` from the example's headings, show it, revise, and get a yes.

### 3. Tracks (`profiles/<track>.md` and `tracks:`)

Read `references/interview.md` part B. Help the user name two to four directions they want (one is fine), and for each one write a track profile: positioning, target titles, strengths, honest gaps, red lines. Agree on a track letter, a name, a tag, the profile file name and the resume file name, and write the `tracks:` list in `config.yaml`. Explain that more tracks make every scoring call longer and a bit dearer.

### 4. Search and filters (`config.yaml`)

Read `references/queries-and-filters.md`. Fill in `apify.locations`, `apify.queries` and `apify.query_names`, `location_whitelist`, `max_applicants`, `exclude_title_patterns`, `min_years_exclude`, the staffing-agency list and the unpaid-role rules. Queries follow job themes, not tracks. Show the resulting YAML before writing it.

### 5. Options

Ask only what applies: whether they need visa sponsorship (`visa.enabled`; leave it off otherwise), an already-applied list (`tracker_path`), the language the model should write reasons and advice in (`scoring.output_language`), a phone notification topic (`NTFY_TOPIC` in `.env`), and the daily caps (`apify.max_jobs_per_search`, `daily.max_total_jobs`). Keep the example's models unless they want to change cost; if they do, tell them to check agreement with `replay_triage.py` first.

### 6. Review and save

Show every file you are about to write or change, in full, one at a time, and write each only after a yes. Run the profile checklist in `references/profile-quality.md` and fix what fails. Do not put contact details (phone, email, home address) in any profile: `shared.md` is sent to the model with every job.

### 7. Check the setup without spending anything

From the workspace run:

```bash
"$ENGINE/venv/bin/python" "$ENGINE/scripts/execution-python/run_daily.py" --dry-run
```

It checks `config.yaml` (tracks, visa, `prompts.yaml`, `texts.yaml`), every profile file the tracks name, and that both keys are set, and it prints how many searches and jobs a real run would make. It makes no network call and writes nothing. Fix every `ERROR` line and run it again until it ends with `Ready.` Read the `WARN` lines with the user: a profile "identical to the template" or with `(unconfirmed)` markers left means step 2 or 3 is not finished.

### 8. Test run, then iterate

With the user's yes (state the cost: see the README), run `run_daily.py --no-notify --max-jobs 5 --db test.db` from the workspace. Open `data/7_shortlist/<today>/_summary.md` and one or two notes with the user and look at where jobs dropped out (`data/5_excluded`, `data/6_scored`). Common fixes:

- Nothing relevant fetched: widen or add queries, check the location whitelist.
- Mostly the wrong seniority: tighten `exclude_title_patterns` or `min_years_exclude`, and sharpen the level line in `shared.md`.
- Scores feel off: look for a missing gap or red line in the track profile, not at the prompts.

Rerun the small test after each change. Delete `test.db` when done so the real `jobs.db` has no test rows.

### 9. Hand off

Tell the user, in plain words: what to run each day (`run_daily.py`), how to schedule it (README, "Running it every day"), which files they own and can edit (`config.yaml`, `profiles/`, `.env`, `prompts.yaml`, `texts.yaml`), that the workspace must stay out of git, and to run `calibrate.py` after the first week to check for jobs the filters wrongly removed. Offer to schedule it with them. Summarize anything left as a default or `(unconfirmed)`.
