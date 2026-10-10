# One shared codebase for the public and personal versions; user data lives in a workspace

The version published on GitHub and the version its author runs every day are the same engine. The engine (code, templates, docs) lives in this repo. A user's data (`config.yaml`, `profiles/`, `resumes/`, `data/`, `jobs.db`, `.env`) lives in a workspace folder outside the repo. The author's own data is just another workspace.

Two alternatives were considered: a `public-release` branch in the existing repo, or a second copy of the code maintained separately for a long time. The branch has a problem because the daily launchd job runs from that folder, so switching branches would make the schedule run unverified code, and `profiles/` and `data/` do not follow a branch. Two copies have a problem because every engine fix has to be synced by hand and the copies drift further apart.

Cost: the engine must stop assuming that its own folder is also the data folder (the scripts used to call `os.chdir(ROOT)` and use relative paths; they now resolve a workspace instead), and the author has to move their data into a workspace before switching. Until the public version has passed a real-world test, the author's original folder and schedule stay untouched.
