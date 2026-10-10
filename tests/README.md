# Tests

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest -n auto      # about a minute on a laptop; drop -n auto to run one at a time
venv/bin/python -m pytest tests/test_filter.py -k years
```

No test uses the network or costs money, and none reads your real workspace.

## How it works

The engine finds its workspace and reads `config.yaml` when a script starts, so most tests build a throw-away workspace with the real `init.py` and run the scripts as subprocesses, the way a user does (`Workspace.run` and `Workspace.daily` in `conftest.py`). A few files (`test_filter`, `test_dedup`, `test_score`, `test_notes`, `test_fetch`) import engine modules directly; they share one boot workspace created at the top of `conftest.py`.

Everything that would leave the machine is replaced by a stand-in in `tests/fakes/`, loaded through `sitecustomize.py` (Python runs it at start-up when `tests/fakes` is on `PYTHONPATH`, so the engine itself has no test code in it):

| Replaced | By | How a test steers it |
| --- | --- | --- |
| the `anthropic` SDK | `fakes.py`: answers from rules on the job title (`Coordinator` scores track A a 4, `Analyst` scores the second track a 4, and so on) | pick job titles in `fixtures/jobs.json`; read what the code sent with `ws.calls("anthropic")` |
| the `apify_client` package | returns `fixtures/jobs.json` (28 fictional postings, one per filter case) | `ws.use_jobs([...])` for another list |
| `requests` (ntfy, Nominatim) | records the call, returns a fixed answer | `ws.calls("ntfy")` |
| `time.sleep`, today's date | no-op; `JOBSEARCH_TEST_TODAY` | `ws.daily(today="2026-10-08")` |

Failures are injected with environment variables, for example `JOBSEARCH_TEST_ANTHROPIC_INIT_FAILS=99` makes every scoring process die at start (the "API is down" day), `JOBSEARCH_TEST_APIFY_FAILS` and `JOBSEARCH_TEST_MODEL_ERRORS` do the same for fetching and for model calls. The list is at the top of `fakes/fakes.py`.

## Golden files

`test_golden.py` compares a few notes and the daily summary, in English and in Traditional Chinese, with the files in `fixtures/golden/`. When you change wording on purpose:

```bash
UPDATE_GOLDEN=1 venv/bin/python -m pytest tests/test_golden.py
git diff tests/fixtures/golden        # read it before you commit
```

## Adding a test

Write it before the code it checks, from the requirement, and run it to see it fail (see "Working test-first" in `CLAUDE.md`). Name it after the behavior (`test_a_failed_day_is_resumed_without_fetching_again`), build the situation with `ws.update_config(...)`, `ws.use_jobs(...)` and the failure switches above, and assert on what a user would see: files, the database rows, the notes, the messages. Tests that only freeze current output say so in their docstring (`test_golden.py` does).

## What the tests do not cover

- Whether the real model scores well. The fake model follows fixed rules, so these tests show that the pipeline around the model works, not that the prompts are good. Use `replay_triage.py` for that (it costs money).
- The real Apify actor and LinkedIn. If the actor changes its output format, only a real run notices.
- Scheduling (launchd, cron) and notifications on a real phone.
