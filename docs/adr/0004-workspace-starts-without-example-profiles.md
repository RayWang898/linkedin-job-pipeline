# 0004. A new workspace starts without example profiles

`init.py` used to copy `profiles.example/` into the workspace. A workspace then began with finished-looking files (`shared.md`, `a_marketing.md`, `b_pm.md`) that held example text. In a real setup run the example track B and its profile `b_pm.md` stayed behind after the user chose one direction, and `--dry-run` could not see it, because no track pointed at it.

Decision: `init.py` copies only `config.yaml` and `.env` and creates an empty `profiles/`. The example profiles stay in the engine as templates; `/setup` (or the user) reads them and writes the user's own files. A workspace then holds only what the user wrote.

Trade-offs. A user who does not use `/setup` must copy and rename the templates by hand (the README says so). A fresh workspace fails `--dry-run` with `Missing profile` for the example tracks in `config.yaml` until the profiles are written; that is intended, a loud failure instead of a silent leftover. `config.yaml` itself is still copied with two example tracks, and the `/setup` skill replaces them.

Alternative not chosen: keep copying and rely on the setup skill to delete the examples. It depends on the agent remembering the step in every session.
