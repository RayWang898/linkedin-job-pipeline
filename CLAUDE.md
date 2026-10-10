# CLAUDE.md

Instructions for AI coding agents working on this repository.

## Read first
1. `CONTEXT.md`: what this project is, who it is for, the glossary, and the current state.
2. `docs/roadmap.md`: the full plan (P0 to P4), what is done, and what to do next.
3. `docs/adr/`: architecture decisions. Start with `0002-single-codebase-with-workspace.md`.

## Rules for this repo
- **Never put personal data in this repo.** No real names beyond the LICENSE, no phone numbers, no school or personal email addresses, no home-directory paths, no resumes, no job data, no API keys or tokens. Run the PII scan in `docs/roadmap.md` before every push.
- **Commits** use the author already configured in this repo (a GitHub noreply address). Do not change `user.email`.
- **Ask the maintainer before any git commit, push, branch, merge or delete.** Propose a commit message and the reason first. The maintainer is new to git, so explain git terms in one line.
- **Pushing** needs a personal access token and is done by the maintainer in the macOS Terminal app, not through the agent.
- **Display text** (note labels, summary file, notification) lives in `scripts/execution-python/texts.py`; a workspace can override it with `texts.yaml`. Do not hardcode such text elsewhere; console output and error messages are plain English.
- **Keep the engine free of workspace content.** Anything a user would customize (config, profiles, resumes, queries, filter rules) belongs in the workspace and has an `*.example` template in this repo.
- **Changing prompts changes scoring behavior.** All model-facing text lives in `scripts/execution-python/prompts.py` (English defaults; a workspace can override it with `prompts.yaml`). After editing it, or the tool schemas in `score.py`, check the scoring with `replay_triage.py` before committing. Compare against a rerun of the old prompts, not against stored scores, because the model itself varies between runs (see ADR 0003).
- **The repo is private until P3 is done.** Do not make it public or add collaborators without the maintainer's explicit yes.
- When a decision is hard to reverse, surprising later, and the result of a real trade-off, write an ADR in `docs/adr/`.
- Update `docs/roadmap.md` (status column) when a task finishes.

## Environment notes
- The agent's sandbox may block writes outside the working directory and block network pushes. If a command is blocked, report which restriction it hit instead of working around it.
- Use a virtual environment (`venv/`) and `pip install -r requirements.txt`. API keys live in `.env` (git-ignored).
