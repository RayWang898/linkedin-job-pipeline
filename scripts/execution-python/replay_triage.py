"""
Triage replay: take already-scored jobs, score them again with the same (or another) model and compare with the existing scores.
The first use is to measure how consistent Haiku is with itself: triage has no fixed temperature, so rescoring the same job naturally gives different scores.
You need that floor first; only then is the agreement rate meaningful when comparing candidate models (for example a cheaper model on OpenRouter).

It writes no cache tables, leaves jobs.db alone and does not overwrite data/6_scored; results go to feedback/ only (not in git).

Sampling: up to --positives jobs whose baseline score is >= 4 (to see whether they stay >= 4), plus --others stratified random jobs (to see whether new >= 4 appear).

Usage:
  venv/bin/python scripts/execution-python/replay_triage.py --file raw_<name>.json                        # default: Haiku, 50 + 50 jobs
  venv/bin/python scripts/execution-python/replay_triage.py --file raw_<name>.json --positives 10 --others 10   # small test
Needs the environment variable ANTHROPIC_API_KEY (can be set in .env).
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import date
from pathlib import Path

import anthropic

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)
import score  # noqa: E402
from filter import visa_settings  # noqa: E402

FEEDBACK_DIR = Path("feedback")


def sample_jobs(scored: list[dict], positives: int, others: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    pos = [j for j in scored if j["triage"]["best_score"] >= 4]
    rest = [j for j in scored if j["triage"]["best_score"] < 4]
    return rng.sample(pos, min(positives, len(pos))) + rng.sample(rest, min(others, len(rest)))


def replay_one(client, config, profiles, usage, job: dict, model: str) -> dict | None:
    cfg = config["scoring"]
    raw = score.call_model(client, cfg.get("provider", "anthropic"), model, score.triage_system(profiles),
                           score.triage_user_prompt(job, cfg["triage_description_chars"], visa_settings(config)["skip_locations"]),
                           score.TRIAGE_TOOL, score.validate_triage, usage)
    return score.finalize_triage(raw) if raw else None


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d:.0%})" if d else "-"


def build_report(model: str, source: str, rows: list[dict], usage: score.Usage, seconds: float) -> str:
    ok = [r for r in rows if r["new"]]
    pos = [r for r in ok if r["old"]["best_score"] >= 4]
    neg = [r for r in ok if r["old"]["best_score"] < 4]
    exact = sum(r["new"]["best_score"] == r["old"]["best_score"] for r in ok)
    within1 = sum(abs(r["new"]["best_score"] - r["old"]["best_score"]) <= 1 for r in ok)
    kept = sum(r["new"]["best_score"] >= 4 for r in pos)
    gained = sum(r["new"]["best_score"] >= 4 for r in neg)
    track_same = sum(r["new"]["best_track"] == r["old"]["best_track"] for r in ok)
    custom_same = sum(r["new"]["needs_custom"] == r["old"]["needs_custom"] for r in ok)
    per_track = {t: sum(r["new"]["scores"][t] == r["old"]["scores"][t] for r in ok) for t in score.TRACKS}
    crossing = [r for r in ok if (r["old"]["best_score"] >= 4) != (r["new"]["best_score"] >= 4)]
    lines = [
        f"# Triage replay: {model} ({date.today().isoformat()})", "",
        f"- Source: {source}; {len(ok)} valid ({len(rows) - len(ok)} failed); took {seconds:.0f} s",
        f"- Compared with: the existing triage scores (possibly another run of the same model), to estimate the model's self-consistency",
        "", "## Results", "",
        f"- Identical best score: {pct(exact, len(ok))}; within 1 point: {pct(within1, len(ok))}",
        f"- **Baseline >= 4 and still >= 4 on replay**: {pct(kept, len(pos))} (the main pass/fail indicator)",
        f"- Baseline < 4 and >= 4 on replay: {pct(gained, len(neg))} (the sample is stratified, so this cannot be converted to an overall rate)",
        f"- Same best track: {pct(track_same, len(ok))}; same needs_custom: {pct(custom_same, len(ok))}",
        "- Identical score per track: " + ", ".join(f"{t} {pct(n, len(ok))}" for t, n in per_track.items()),
        "", f"## Jobs that crossed the 4-point threshold ({len(crossing)})", "",
        "| Job | Baseline best | Replay best | Replay reason |", "| --- | --- | --- | --- |",
    ]
    for r in crossing:
        j = r["job"]
        cell = lambda s: str(s).replace("|", "/")  # a | in a title or company name would break the table
        lines.append(f"| {cell(j['company'][:24])}: {cell(j['title'][:40])} | {r['old']['best_track']}{r['old']['best_score']} | "
                     f"{r['new']['best_track']}{r['new']['best_score']} | {cell(r['new']['reason'][:80])} |")
    lines += ["", "## API usage", "", f"- {usage.plain()}", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="file name under data/6_scored/ to sample from")
    parser.add_argument("--positives", type=int, default=50, help="how many jobs with baseline >= 4 to sample")
    parser.add_argument("--others", type=int, default=50, help="how many jobs with baseline < 4 to sample")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--model", help="model to replay with (default: triage_model from config)")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not found. Set it in .env or export it as an environment variable.")
    config = score.load_config()
    model = args.model or config["scoring"]["triage_model"]
    path = score.SCORED_DIR / args.file
    scored = json.loads(path.read_text(encoding="utf-8"))
    jobs = sample_jobs(scored, args.positives, args.others, args.seed)
    print(f"Replaying {len(jobs)} jobs (baseline >= 4: {sum(j['triage']['best_score'] >= 4 for j in jobs)}), model {model}", flush=True)

    profiles = score.load_profiles()
    client = anthropic.Anthropic()
    usage = score.Usage()
    rows = []
    start = time.time()
    for n, job in enumerate(jobs, 1):
        new = replay_one(client, config, profiles, usage, job, model)
        rows.append({"job": {k: job.get(k) for k in ("jobId", "title", "company")}, "old": job["triage"], "new": new})
        if n % 10 == 0:
            print(f"  {n}/{len(jobs)}", flush=True)
    report = build_report(model, f"{path} ({len(jobs)} sampled)", rows, usage, time.time() - start)
    FEEDBACK_DIR.mkdir(exist_ok=True)
    stem = f"triage_replay_{model.replace('/', '_')}_{date.today().isoformat()}"
    (FEEDBACK_DIR / f"{stem}.md").write_text(report, encoding="utf-8")
    (FEEDBACK_DIR / f"{stem}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(report)
    print(f"Saved: {FEEDBACK_DIR / (stem + '.md')}")


if __name__ == "__main__":
    main()
