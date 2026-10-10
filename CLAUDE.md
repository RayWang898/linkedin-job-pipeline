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
- `.claude/skills/setup/` is the `/setup` interview for new users. It names config keys, commands and file formats; when those change, update the skill in the same commit.
- `init.py` and `check.py` (the `--dry-run` report) name config keys and files; when you add or rename a setting, update the checks in `check.py` and the files `init.py` copies.
- `AGENTS.md` is the manual for a *user's* agent (commands, setup interview, tuning, recovery). When you change a command, flag, config key or failure behavior, update it in the same commit.
- **Run the tests before you commit:** `venv/bin/pip install -r requirements-dev.txt` once, then `venv/bin/python -m pytest -n auto` (see `tests/README.md`). They use no network and cost nothing. A wording change in `prompts.py` or `texts.py` will fail `test_golden.py` on purpose: regenerate the golden files with `UPDATE_GOLDEN=1` and read the diff. The tests do not judge the quality of real model scores; that is still `replay_triage.py`.
- Update `docs/roadmap.md` (status column) when a task finishes.

## Working test-first

Every behavior change and every bug fix starts with a test that fails. The point is that a test written after the code tends to describe what the code already does, so it cannot catch a mistake in it.

1. **Write the test first, from the requirement.** The requirement is the roadmap's acceptance criteria, or the maintainer's description of the bug, not the current output of the code. Do not fill in expected values by running the code and copying what it prints. (Golden files are the one exception: they are generated, and a human reads the diff before they are accepted.)
2. **Run it and show the red result** before you write the fix. A new test that passes on its first run proves nothing: check that it can fail (undo the fix, or break the code on purpose) before you trust it.
3. **Then make the smallest change that turns it green**, and run the whole suite.
4. **Propose two commits**, the failing test and then the fix, so the order is visible in the history. If that is not practical, show the red run in the conversation.
5. **When a test fails after your change, say which side is wrong and why.** Never edit a test to make it pass without telling the maintainer what you changed and the reason. A test that you changed to match new code needs the maintainer's yes.
6. **Tests that only pin today's behavior** (the golden files, and checks written to protect a refactor) are a safety net, not a specification. Say so in the test's docstring, and change them deliberately, never as a side effect.

If your setup has a TDD skill (for example `/tdd`), use it for a single behavior. For a larger change, interview first (`/grill-with-docs`), then write a spec and tickets, and implement each ticket test-first.

What test-first does not cover, and what to do instead: the quality of real model scores (record a baseline with `replay_triage.py` before you change a prompt, then compare against a rerun of the old setup), the `/setup` interview (its test is a real person installing from the README), and documentation-only changes.

## Environment notes
- The agent's sandbox may block writes outside the working directory and block network pushes. If a command is blocked, report which restriction it hit instead of working around it.
- Use a virtual environment (`venv/`) and `pip install -r requirements.txt`. API keys live in `.env` (git-ignored).
