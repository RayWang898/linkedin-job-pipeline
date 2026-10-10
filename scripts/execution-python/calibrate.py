"""
校準輔助：列出「差一點就被排除／差一點就入選」的職缺，方便每天花幾分鐘檢查規則有沒有誤殺。
只讀檔，不改任何資料。用法：python scripts/execution-python/calibrate.py [YYYYMMDD] [--margin 50]
"""

import argparse
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

EXCLUDED_DIR = Path("data/5_excluded")
SCORED_DIR = Path("data/6_scored")
SHORTLIST_DIR = Path("data/7_shortlist")


def applicants(job: dict):
    d = job.get("applicantsDetails") or {}
    return d.get("count") or d.get("max") or d.get("min")


def shortlisted_ids(day: str) -> set[str]:
    folder = SHORTLIST_DIR / f"{day[:4]}-{day[4:6]}-{day[6:]}"
    ids = set()
    for md in folder.glob("*/*.md"):
        m = re.search(r"^jobId:\s*\"?(\d+)", md.read_text(encoding="utf-8"), re.M)
        if m:
            ids.add(m.group(1))
    return ids


def line(job: dict, extra: str = "") -> str:
    return f"  - {job['title']} @ {job['company']}（{job.get('location')}）{extra}\n    {job.get('jobUrl', '')}  [{job['jobId']}]"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("day", nargs="?", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--margin", type=int, default=50, help="應徵人數超過上限多少以內算差一點")
    args = parser.parse_args()

    cap = yaml.safe_load(open("config.yaml", encoding="utf-8"))["max_applicants"]
    excluded = json.load(open(EXCLUDED_DIR / f"raw_{args.day}.json", encoding="utf-8"))
    by_reason = defaultdict(list)
    for e in excluded:
        by_reason[e["reason"]].append(e["job"])

    print(f"===== {args.day} 校準清單（上限 {cap}，寬限 +{args.margin}）=====\n")

    near = [j for j in by_reason["max_applicants"] if (applicants(j) or 0) <= cap + args.margin]
    print(f"## 應徵人數剛好超過上限（{len(near)} 筆）")
    for j in sorted(near, key=applicants):
        print(line(j, f"{applicants(j)} 人"))

    print(f"\n## 因職稱被排除（{len(by_reason['exclude_title'])} 筆，檢查有沒有誤殺）")
    for j in by_reason["exclude_title"]:
        print(line(j))

    print(f"\n## 因地點被排除（{len(by_reason['location'])} 筆）")
    for j in by_reason["location"]:
        print(line(j))

    print(f"\n## 因年資被排除（{len(by_reason['min_years'])} 筆）")
    for j in by_reason["min_years"]:
        print(line(j))

    scored = json.load(open(SCORED_DIR / f"raw_{args.day}.json", encoding="utf-8"))
    picked = shortlisted_ids(args.day)
    missed = [j for j in scored if j["jobId"] not in picked]
    print(f"\n## 通過初評但沒進短名單（{len(missed)} 筆，檢查有沒有低估）")
    for j in sorted(missed, key=lambda j: -j["triage"]["best_score"]):
        t = j["triage"]
        print(line(j, f"{t['best_track']}={t['best_score']} {t['scores']}"))
        print(f"    {t['reason']}")


if __name__ == "__main__":
    main()
