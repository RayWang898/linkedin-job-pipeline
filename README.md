# linkedin-job-pipeline

A daily pipeline for job hunting. It fetches new public LinkedIn postings, drops the ones that break your hard rules, scores each remaining job against your background with Claude, and writes a ranked shortlist as Markdown notes: one folder per job, with a short summary, a fit score for each of your job directions ("tracks") and advice on which resume to send and what to emphasize.

It only reads public job pages. It does not log in to LinkedIn and it does not apply for you.

> **Status: pre-release.** The engine works end to end, but setup is manual for now. An agent-assisted setup (your coding agent interviews you and writes your profiles and config) is planned; see [Roadmap](#roadmap).

## How it works

```
fetch -> slim -> dedup -> filter -> AI scoring -> shortlist
```

1. **Fetch:** an Apify actor pulls public LinkedIn job pages for each of your search queries and locations.
2. **Slim and dedup:** keeps the needed fields and drops jobs you have already seen (SQLite, `jobs.db`).
3. **Filter:** hard rules from `config.yaml`: location whitelist, applicant cap, title patterns, required years of experience, unpaid roles, staffing agencies, jobs you already applied to, and optionally visa-sponsorship phrases. No AI here.
4. **AI scoring:** a small model scores every remaining job 0-5 for each track using your profiles; a larger model writes detailed advice for the top ones; the small model also summarizes them.
5. **Shortlist:** `data/7_shortlist/<date>/` gets one folder and one `.md` per job plus a `_summary.md` with counts and token usage. Optionally your phone gets a short notification (ntfy).

The daily run is a plain script. It needs no agent at runtime; it calls the Anthropic API itself.

## What you need

- Python 3.11 or newer, and a terminal
- An [Apify](https://apify.com) account and API token (runs the scraper)
- For the scoring, one of:
  - an [Anthropic](https://console.anthropic.com) API key (the default), or
  - [Claude Code](https://claude.com/claude-code) logged in with a Claude subscription, and `scoring.provider: "claude-code"` in `config.yaml` (no API key; see [Cost](#cost))
- Optional: an [ntfy](https://ntfy.sh) topic for phone notifications
- Optional: macOS, if you want to use the included launchd example for scheduling

You pay for Apify and Anthropic usage yourself. See [Cost](#cost).

## Quickstart

**1. Get the engine and install its dependencies.**

```bash
git clone https://github.com/RayWang898/linkedin-job-pipeline.git
cd linkedin-job-pipeline
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**2. Create your workspace.** The workspace is a folder of your own that holds your config, profiles, data and keys, kept apart from the engine and outside the engine's folder (inside it, some of your files, such as `prompts.yaml` or an applied-jobs list, are not covered by the ignore rules and could be committed by mistake). `init.py` creates it from the examples: it copies `config.yaml` and `.env` and leaves `profiles/` empty, because you write your own profiles from the templates in `profiles.example/` (it never overwrites a file that is already there, and adds a `.gitignore` that keeps your keys and data out of git):

```bash
ENGINE="$PWD"                      # the folder you just cloned
"$ENGINE/venv/bin/python" "$ENGINE/scripts/execution-python/init.py" ~/jobsearch
cd ~/jobsearch
```

Add `--language zh-TW` to also copy the Traditional Chinese `prompts.yaml` and `texts.yaml` (see [Prompt and display language](#prompt-and-display-language)).

**3. Fill in your details.**

- `.env`: put your `APIFY_TOKEN` and `ANTHROPIC_API_KEY` (and `NTFY_TOPIC` if you want notifications). With `scoring.provider: "claude-code"`, leave `ANTHROPIC_API_KEY` out.
- `profiles/shared.md`: your background, shared experience, skills, hard rules, and the job types worth a custom resume. This is the most important file: the scoring is only as good as what you write here.
- `profiles/<track>.md`: one file per job direction, describing what you want and what to emphasize for it.
- `config.yaml`: your locations, search queries, filter rules and `tracks:` (one entry per direction: a letter, a name, a tag, the profile file in `profiles/` and the file name of your resume). The example has two tracks; use as many as you like, at least one. Every setting is commented in the file.

**4. Check the setup, for free.** The dry run reads your config and profiles and prints what a run would do, without any network call, model call or file write. It points out a missing key, a profile that is still the template, a profile file that does not exist, a typo in a setting, and which earlier days a run would first finish:

```bash
"$ENGINE/venv/bin/python" "$ENGINE/scripts/execution-python/run_daily.py" --dry-run
```

Add `--json` for a machine-readable report. It exits with 1 when something must be fixed first.

**5. Try a small run.** It fetches only 5 jobs per query and uses a separate test database, so your real `jobs.db` stays clean:

```bash
"$ENGINE/venv/bin/python" "$ENGINE/scripts/execution-python/run_daily.py" --no-notify --max-jobs 5 --db test.db
```

Then open `data/7_shortlist/<today>/_summary.md` and a job note next to it. Check that the filters and scores look sensible before running at full size. (With only 5 jobs per query the shortlist may be empty; that is normal.)

**6. Run it for real.**

```bash
"$ENGINE/venv/bin/python" "$ENGINE/scripts/execution-python/run_daily.py"
```

A day that already succeeded is skipped; `--force` runs it again with a new fetch (the day's earlier files are moved to `data/_replaced/` first). Run the scripts from inside your workspace, or from anywhere with `--workspace /path/to/workspace` (or the `JOBSEARCH_HOME` environment variable). Your data stays in the workspace, never in the engine folder; `config.yaml`, `profiles/`, `resumes/`, `data/`, `.env` and `*.db` are git-ignored in case you do keep a workspace inside a clone.

## Running it every day (macOS)

`scripts/launchd/com.example.jobsearch.plist` is a template for a launchd job. Replace `/path/to/engine` with your clone and `/path/to/workspace` with your workspace, copy it to `~/Library/LaunchAgents/`, and load it:

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.example.jobsearch.plist
```

The Mac must be awake at the scheduled time; if it was asleep, launchd runs the job at the next wake, and a day that already succeeded is skipped. To wake a Mac a few minutes earlier, `sudo pmset repeat wakeorpoweron MTWRFSU 07:55:00` works. On Linux, a cron entry that runs the same command with `--workspace` does the same job.

## Cost

You pay two providers. Check both price pages before you rely on these numbers; they change.

**Anthropic (scoring).** Measured on the author's setup, which fetches about 650 postings a day and scores them on four tracks: roughly 0.24M input, 0.03M output, 0.02M cache-write and 0.9M cache-read tokens a day, across triage and detailed review. At the list prices of October 2026 (Haiku 4.5: $1 input / $5 output per million tokens, used for triage and summaries; Sonnet 5.5: $2 / $10, used for the detailed review; cache reads about a tenth of the input price) that is about **$0.5 to $1 a day**, or $15 to $30 a month. A rough rule from a measured sample: triaging one job costs about a quarter of a cent with Haiku 4.5.

**Scoring on a Claude subscription instead (`scoring.provider: "claude-code"`).** Each model call runs `claude -p` (Claude Code in print mode) with no tools, so nothing is billed per token; the calls count toward your plan's usage limits instead. A full day is several hundred calls, so a smaller plan may hit its limit partway: the jobs that could not be scored are skipped that day (listed as failures in the summary), and you can score the day again later with `score.py --run-date`. Each call starts a new `claude` process, so a day takes longer than with the API. The `claude` command must be logged in for the user the schedule runs as; `--dry-run` only checks that the command exists.

**Apify (fetching).** The default actor charges per event: $0.00006 for each job, $0.00001 for each job whose full description it also fetches (`apify.fetch_details`, which the pipeline needs), and $0.00005 each time a search starts, plus Apify platform usage (compute). A real test run (one search, 5 jobs) was billed $0.0004 in all, and its compute (0.0006 compute units) showed as $0.000. At about 650 postings a day over 14 searches that is roughly $0.05 a day, or $1.50 a month, before compute. Compute depends on how long a run takes and was not measurable at test size, so check the Apify console after your first full-size run.

What moves the bill:

| Setting (`config.yaml`) | Effect |
| --- | --- |
| `apify.max_jobs_per_search`, `daily.max_total_jobs` | Fewer fetched jobs: less Apify and less scoring |
| Number of tracks and the length of your profiles | They are sent with every triage call (cached, but still tokens) |
| `scoring.triage_description_chars` | How much of each posting the triage model reads |
| `scoring.detail_count`, `scoring.detail_custom` | How many jobs get the more expensive detailed review |
| `scoring.triage_model` | A cheaper model costs less. Check agreement first with `replay_triage.py` (see below) |

## Your tracks and profiles

A **track** is a job direction you want (for example Marketing or Project Management), with one profile file and one resume. Triage scores every job once per track; the best score decides whether a job reaches the shortlist (`scoring.shortlist_min_score`, default 4; the list is never padded with weaker jobs). Jobs that fit a type listed under "Job types worth a custom resume" in `shared.md` but no base resume covers well are flagged `needs custom resume` and listed separately.

Note properties and tags use the track letter and tag (`score_A`, `track/A-Marketing`), so they stay stable if you rename a track.

## Optional features

- **Visa sponsorship (`visa:`).** Off by default. If you need an employer to sponsor a work visa, set `visa.enabled: true`: jobs whose description clearly refuses sponsorship are dropped in the filter, and the scoring model also warns about them (a warning line in the note and a list in the daily summary; their scores are not changed, so a wrong call never hides a job). The default wording is generic about the kind of visa (the examples are US ones such as OPT and H-1B); write your exact status in `profiles/shared.md`.
- **Triage flags (`triage_flags:`).** Off by default. For judgment calls the hard filters cannot make: when `triage_flags.enabled` is true, the scoring model also flags jobs that are clearly above your level, in a field on your "Not interested" list in `profiles/shared.md`, or suspicious (vague, no real employer, scam signs). A flagged job's scores are capped at 2, so it stays off the shortlist, and the daily summary lists it with the model's reason. A job on your "Interested" list is kept even when it asks for more years. The engine ships generic rules; write your own in `prompts.yaml` (key `flags_rubric`), and check them with `replay_triage.py` before relying on them.
- **Already-applied list (`tracker_path`).** Point it at a text file of jobs you already applied to or decided against, and matching jobs are skipped. The format is described in `config.example.yaml`.
- **Backfill.** `scripts/execution-python/backfill.py` fetches the past N days once and writes its own shortlist folder.
- **Add a job by hand.** `scripts/execution-python/add_to_shortlist.py` scores and files a single posting, including ones that are not on LinkedIn.
- **Calibration.** `scripts/execution-python/calibrate.py` lists jobs that were nearly excluded or nearly shortlisted, so you can check your rules in a few minutes a day. The daily run saves its output to `feedback/`.

## Prompt and display language

Everything the scoring models are told lives in `scripts/execution-python/prompts.py`, in English. Set `scoring.output_language` in `config.yaml` to change the language of the reasons, advice and summaries. To replace the instructions themselves, copy `prompts.example/zh-TW.yaml` (a Traditional Chinese set) to `prompts.yaml` in your workspace and edit it; keys you delete keep the English default. Changing a prompt makes cached scores be recomputed.

The notes, `_summary.md` and the phone notification are written from `scripts/execution-python/texts.py` (English). Copy `texts.example/zh-TW.yaml` to `texts.yaml` in your workspace to switch them, or edit any text. These never affect scoring. If your tracker file uses other words than `Applied` and `Evaluated, skipped`, set `tracker_labels` in `config.yaml`. Console output and error messages are English only.

## Tests

`venv/bin/pip install -r requirements-dev.txt && venv/bin/python -m pytest -n auto` runs about 270 tests in about a minute. They use fakes for Apify, Anthropic and the network, so they cost nothing and never touch your data. See `tests/README.md`.

## Limitations

- **Terms of service.** Fetching LinkedIn through a third-party scraper may conflict with LinkedIn's terms of service. Use it for your own job search only, and read the terms yourself. The scraper is a third-party Apify actor you rely on; if it changes or disappears, fetching breaks.
- **Scores are model judgments, and they vary.** In a test on 100 jobs, scoring the same job twice with the same model gave the same best score for 84% of the jobs and a score within one point for 95%. Jobs near the 3/4 boundary move most. Treat the shortlist as a starting point, not a verdict, and use `replay_triage.py` before you change prompts or models: compare against a rerun of the old setup, not against old stored scores.
- **Public data only.** Applicant counts and descriptions are whatever the public page shows; some postings are missing fields.
- **One successful run per day.** A day that succeeded is skipped unless you pass `--force`. A day that fails is resumed from the step it stopped at the next time `run_daily.py` runs, and a day that failed after fetching is finished automatically by the next daily run (up to 3 days back), so a temporary API outage does not lose that day's jobs.
- **macOS only for the scheduling example.** The pipeline itself is plain Python; other schedulers work too but are not documented yet.

## Roadmap

Next: testing with outside users and fixing what they run into, then a `CONTRIBUTING.md`. See `docs/roadmap.md`.

## License

MIT. See [LICENSE](LICENSE).
