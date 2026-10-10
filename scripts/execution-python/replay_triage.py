"""
初評重播：拿已評分的職缺，用同一個（或另一個）模型再評一次，和既有分數比對。
第一個用途是量「Haiku 自己跟自己」的一致性：初評沒有固定溫度，同一筆職缺重評分數本來就會有出入，
要先知道這個底線，之後比較候選模型（例如 OpenRouter 上的便宜模型）時，「吻合率」才有意義。

不寫快取表、不動 jobs.db、不覆蓋 data/6_scored，結果只寫到 feedback/（不進 git）。

抽樣：基準分數 >= 4 的職缺最多抽 --positives 筆（看「是否仍 >= 4」），其餘分層隨機抽 --others 筆（看「有沒有冒出新的 >= 4」）。

用法：
  venv/bin/python scripts/execution-python/replay_triage.py                      # 預設：backfill 批次 1、Haiku、50 + 50 筆
  venv/bin/python scripts/execution-python/replay_triage.py --positives 10 --others 10   # 小量測試
需要環境變數 ANTHROPIC_API_KEY（可放在 .env）。
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
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(SCRIPTS_DIR))

import score  # noqa: E402

FEEDBACK_DIR = Path("feedback")
TRACK_KEYS = list(score.TRACKS)


def sample_jobs(scored: list[dict], positives: int, others: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    pos = [j for j in scored if j["triage"]["best_score"] >= 4]
    rest = [j for j in scored if j["triage"]["best_score"] < 4]
    return rng.sample(pos, min(positives, len(pos))) + rng.sample(rest, min(others, len(rest)))


def replay_one(client, config, profiles, usage, job: dict, model: str) -> dict | None:
    cfg = config["scoring"]
    raw = score.call_model(client, cfg.get("provider", "anthropic"), model, score.triage_system(profiles),
                           score.triage_user_prompt(job, cfg["triage_description_chars"], config.get("visa_skip_locations")),
                           score.TRIAGE_TOOL, score.validate_triage, usage)
    return score.finalize_triage(raw) if raw else None


def pct(n: int, d: int) -> str:
    return f"{n}/{d}（{n / d:.0%}）" if d else "—"


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
    per_track = {t: sum(r["new"]["scores"][t] == r["old"]["scores"][t] for r in ok) for t in TRACK_KEYS}
    crossing = [r for r in ok if (r["old"]["best_score"] >= 4) != (r["new"]["best_score"] >= 4)]
    lines = [
        f"# 初評重播：{model}（{date.today().isoformat()}）", "",
        f"- 來源：{source}；有效 {len(ok)} 筆（失敗 {len(rows) - len(ok)} 筆）；耗時 {seconds:.0f} 秒",
        f"- 比對對象：既有初評分數（可能是另一次執行、同一個模型），用於估計模型的自我一致性",
        "", "## 結果", "",
        f"- 最佳分數完全相同：{pct(exact, len(ok))}；相差不超過 1 分：{pct(within1, len(ok))}",
        f"- **基準 >= 4 的職缺，重播仍 >= 4**：{pct(kept, len(pos))}（這是通過標準的主要指標）",
        f"- 基準 < 4 的職缺，重播冒出 >= 4：{pct(gained, len(neg))}（抽樣為分層，不能直接換算成整體比例）",
        f"- 最佳路線相同：{pct(track_same, len(ok))}；needs_custom 相同：{pct(custom_same, len(ok))}",
        "- 各路線分數完全相同：" + "、".join(f"{t} {pct(n, len(ok))}" for t, n in per_track.items()),
        "", f"## 跨過 4 分門檻的職缺（{len(crossing)} 筆）", "",
        "| 職缺 | 原最佳 | 重播最佳 | 重播理由 |", "| --- | --- | --- | --- |",
    ]
    for r in crossing:
        j = r["job"]
        cell = lambda s: str(s).replace("|", "/")  # 職稱或公司名含 | 會弄壞表格
        lines.append(f"| {cell(j['company'][:24])}：{cell(j['title'][:40])} | {r['old']['best_track']}{r['old']['best_score']} | "
                     f"{r['new']['best_track']}{r['new']['best_score']} | {cell(r['new']['reason'][:80])} |")
    lines += ["", "## API 用量", "", f"- {usage.line()}", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="raw_backfill_20261005.json", help="data/6_scored/ 底下的檔名")
    parser.add_argument("--positives", type=int, default=50, help="基準 >= 4 的職缺抽幾筆")
    parser.add_argument("--others", type=int, default=50, help="基準 < 4 的職缺抽幾筆")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--model", help="要重播的模型（預設 config 的 triage_model）")
    args = parser.parse_args()

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("找不到 ANTHROPIC_API_KEY，請在 .env 設定或匯出環境變數。")
    config = score.load_config()
    model = args.model or config["scoring"]["triage_model"]
    path = score.SCORED_DIR / args.file
    scored = json.loads(path.read_text(encoding="utf-8"))
    jobs = sample_jobs(scored, args.positives, args.others, args.seed)
    print(f"重播 {len(jobs)} 筆（基準 >= 4：{sum(j['triage']['best_score'] >= 4 for j in jobs)}），模型 {model}", flush=True)

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
    report = build_report(model, f"{path}（{len(jobs)} 筆抽樣）", rows, usage, time.time() - start)
    FEEDBACK_DIR.mkdir(exist_ok=True)
    stem = f"triage_replay_{model.replace('/', '_')}_{date.today().isoformat()}"
    (FEEDBACK_DIR / f"{stem}.md").write_text(report, encoding="utf-8")
    (FEEDBACK_DIR / f"{stem}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(report)
    print(f"已存檔：{FEEDBACK_DIR / (stem + '.md')}")


if __name__ == "__main__":
    main()
