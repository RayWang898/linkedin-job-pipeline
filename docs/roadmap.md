# Roadmap: from personal tool to public open-source project

Status values: done, next, todo, later. Update the Status column when work lands.

## P0: Blockers before anything is public (done)

| # | Task | Status | Notes |
|---|---|---|---|
| 1 | Decide the architecture: one engine plus a user workspace | done | `docs/adr/0002-single-codebase-with-workspace.md` |
| 2 | New repo with a clean git history, built from an explicit allowlist of files | done | commit `b861ed4`, pushed to a private GitHub repo |
| 3 | Remove personal data: track names and resume file names, the hardcoded four-track wording in prompts, example places, launchd paths | done | track list reduced to two example tracks |
| 4 | PII scan | done | no name, email, phone, school, home path or key left. Only expected hits: the LICENSE name and the OPT visa wording, which P1 makes optional |
| 5 | License | done | MIT, copyright holder as named in `LICENSE` |

## P1: Make the engine usable by other people

Do these in order. Tasks 1 and 2 matter most: without them the engine cannot run the maintainer's real setup, so the duplicate codebase cannot be retired.

| # | Task | Status | Notes |
|---|---|---|---|
| 6 | Workspace path for every script. Resolve in this order: `--workspace` argument, `JOBSEARCH_HOME` environment variable, current directory. Replace the `os.chdir(ROOT)` pattern in `run_daily.py`, `backfill.py`, `replay_triage.py` and the cwd-relative `Path("data/...")` constants | next | Acceptance: running from an empty folder with a workspace argument works; nothing reads or writes inside the repo folder |
| 7 | Read tracks from `config.yaml` (`tracks:` list with id letter, name, tag, profile file, resume file). Remove `TRACKS` from `score.py`, `TRACK_LABELS` from `run_daily.py` and `TRACK_KEYS` from `replay_triage.py`. Keep property names such as `score_A` and tag `track/A-Marketing` unchanged | todo | Acceptance: a four-track config reproduces the maintainer's current setup; a one-track config works. Then update the README Setup section, which still tells users to edit `TRACKS`. Search queries are also labeled A, B, C by order; that is a separate system from track ids and stays independent |
| 8 | Make the visa feature optional: `visa.enabled`, default false. When off, the `RUBRIC` has no visa paragraph and the triage tool schema has no `visa_blocked` | todo | Also gate the other visa logic on the same switch: `passes_visa` in `filter.py`, `visa_excerpts` and the `<visa_excerpts>` prompt block and the clamp-to-1 in `score.py`, and the call in `replay_triage.py`. Move `visa_skip_locations` and `exclude_visa_patterns` under a `visa:` section in `config.example.yaml`. Test both paths. Keep the feature itself; it was just built and works for users on a work visa |
| 9 | Translate prompts and all output text to English (`RUBRIC`, `DETAIL_INSTRUCTIONS`, notes, summaries, error messages, docstrings and comments), and translate the ADRs in `docs/adr/`, which are still Chinese | todo | Prompts feed `triage_hash`, so cached scores are invalidated. Use `replay_triage.py` to compare scores before and after; scoring must not get worse |
| 10 | README: quickstart (agent path and manual path), cost, limits | todo | Draft exists |
| 11 | State the LinkedIn / Apify terms-of-service caveat and personal-use limit | todo | Included in the README draft; keep it prominent |

## P2: Make the agent-assisted setup work (the core of the positioning)

| # | Task | Status | Notes |
|---|---|---|---|
| 12 | `AGENTS.md` for the user's agent: install, run, where files live | later | Separate from `CLAUDE.md`, which is for developing this repo |
| 13 | `/setup` skill: interview the user, then write `profiles/` and `config.yaml` | later | The hardest part for new users is writing profiles; this is the main selling point |
| 14 | `init` command that creates a workspace and copies the examples | later | |
| 15 | `--dry-run` and machine-readable output on the main commands | later | Safer and easier for agents |
| 16 | Fixture data and basic tests so the pipeline can run end to end without API cost | later | Also protects against regressions |

## P3: Test with a real outsider, then release

| # | Task | Status | Notes |
|---|---|---|---|
| 17 | A friend who is not an international student installs it from the README only | later | Record every place they get stuck |
| 18 | Fix what they found | later | |
| 19 | Make the GitHub repo public; set description and topics | later | Only with the maintainer's explicit yes |
| 20 | Migrate the maintainer's own setup: move their data into a workspace, repoint the launchd job to this repo, retire the engine code in the old folder | later | Keep the old folder as a backup for a while. This is the step that ends the duplication |
| 21 | `CONTRIBUTING.md` and issue templates (short) | later | |

## P4: Later, not blocking

- English architecture docs and a flow diagram under `docs/`.
- Cron and Windows scheduling examples (launchd is macOS only).
- Versioning and a changelog (semantic versioning).
- An MCP server wrapper, only if people ask. The standards are still moving.
- Packaging the engine so it can be installed with `pip`.
- A generic version of the cover-letter skill.

## PII scan (run before every push)

Search every tracked file, and the git history, for: the maintainer's phone number, any school or personal email address, the home-directory path, the old folder name, the maintainer's city and country preferences from the original config, and any `API_KEY`, `TOKEN` or `sk-` string with a value. Expected hits only: the name in `LICENSE`. Any other hit blocks the push.

## Decisions already made (do not re-open without a reason)
- License: MIT.
- Repo name: `linkedin-job-pipeline`, private until P3.
- Track ids are single letters; the number of tracks is the user's choice.
- Visa handling stays as a feature, optional, default off.
- Output language: English.
- The first release excludes the cover-letter skill, hooks, Chinese architecture docs and the Obsidian dashboard.
