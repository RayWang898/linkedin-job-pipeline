# linkedin-job-pipeline

A daily pipeline that fetches new LinkedIn job postings, filters them with hard rules, scores them against your background with Claude, and writes a ranked shortlist as Markdown notes.

> **Status: work in progress.** This is a first cut of the public version. Prompts and output text are still partly in Chinese, and setup is manual. English prompts, a setup guide and an agent-assisted setup are planned.

## How it works

```
fetch -> slim -> dedup -> filter -> AI scoring -> shortlist
```

1. **Fetch:** pulls public LinkedIn job pages through an Apify actor. It does not log in and does not apply for you.
2. **Slim and dedup:** keeps the needed fields and drops jobs seen before (SQLite).
3. **Filter:** drops jobs by location, title, applicant count, required years, unpaid roles and staffing agencies. All rules live in `config.yaml`.
4. **AI scoring:** a small model scores every job 0-5 for each of your tracks. A larger model writes detailed advice for the top ones.
5. **Shortlist:** one Markdown note per job, plus a daily summary.

## What you need

- Python 3.11+
- An [Apify](https://apify.com) token
- An [Anthropic](https://console.anthropic.com) API key
- Optional: an [ntfy](https://ntfy.sh) topic for phone notifications

You pay for Apify and Anthropic usage yourself. Lower `max_jobs_per_search` and `daily.max_total_jobs` in `config.yaml` to control cost.

## Setup (manual)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml    # edit locations, queries and filter rules
cp -r profiles.example profiles       # write your own background and tracks
cp .env.example .env                  # add your keys
venv/bin/python scripts/execution-python/run_daily.py --no-notify --max-jobs 5 --db test.db
```

Your tracks are defined in `TRACKS` in `scripts/execution-python/score.py` (two example tracks, A and B). Put your resumes in `resumes/`. `config.yaml`, `profiles/`, `resumes/`, `data/`, `.env` and the databases are git-ignored, so your personal data stays out of the repository.

To run it every day on macOS, adapt `scripts/launchd/com.example.jobsearch.plist`.

## Limitations

- Fetching LinkedIn through a third-party scraper may conflict with LinkedIn's terms of service. Use it for your own job search only, and check the terms yourself.
- Scores are model judgments. Treat the shortlist as a starting point, not a verdict.

## License

MIT. See [LICENSE](LICENSE).
