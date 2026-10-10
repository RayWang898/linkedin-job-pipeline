"""把單筆職缺補進某一天的短名單。兩種來源：

1. LinkedIn 職缺（例如被篩選誤殺、但使用者已經投遞）：給 jobId，從 data/2_slim/ 找出職缺（要有 description）。
   排名接在該日現有最大 rank 之後，並把 jobs 表的 recommended_date 設成該日期。
2. 抓取範圍外的 LinkedIn 職缺（例如其他城市）：同時給 jobId 與 --file、--title、--company，描述用文字檔。
   property 為 source: linkedin、有 jobId 與 rank，但沒有刊登日、應徵人數、抓取日；並把 jobId 寫進 jobs 表，之後被抓到時會被去重擋下。
3. 非 LinkedIn 職缺：用 --file 給職缺描述文字檔，並給 --title、--company。property 會有 source: other、
   沒有 jobId、rank 與應徵人數；資料夾名稱不帶排名前綴。

兩種都會做初評、詳評、SUMMARY（同 score.py，沿用快取），再在 data/7_shortlist/<日期>/ 建立職缺資料夾與 .md。
非 LinkedIn 職缺若資料夾已存在（例如先手動放了履歷與 cover letter）且 .md 還沒有 SUMMARY，就沿用該資料夾，
只重寫 .md，保留 status、interest 與 user note，不動資料夾內的其他檔案。其餘情況不動既有職缺的資料夾。

用法：
  venv/bin/python scripts/execution-python/add_to_shortlist.py <jobId> [--date 2026-10-04] [--status applied]
  venv/bin/python scripts/execution-python/add_to_shortlist.py <jobId> --file jd.txt --title ... --company ... [--location ...]
  venv/bin/python scripts/execution-python/add_to_shortlist.py --file jd.txt --title "Technical Writer" \
      --company "Curio Science" [--location ...] [--url ...] [--salary ...] [--date ...] [--status ...]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import dedup
import score

SLIM_DIR = Path("data/2_slim")
LINKEDIN_JOB_URL = "https://www.linkedin.com/jobs/view/{}"


def find_job(job_id: str) -> tuple[dict, str]:
    """回傳 (職缺, 抓取日)。從最新的瘦身檔往回找，要有 description 才算。"""
    for path in sorted(SLIM_DIR.glob("*.json"), reverse=True):
        for job in json.loads(path.read_text(encoding="utf-8")):
            if str(job.get("jobId")) == job_id and job.get("description"):
                m = re.search(r"(\d{4})(\d{2})(\d{2})", path.stem)
                return job, "-".join(m.groups()) if m else ""
    sys.exit(f"{SLIM_DIR}/ 裡找不到 jobId {job_id}（或沒有 description）。")


def next_rank(day_dir: Path) -> int:
    ranks = [int(r) for md in day_dir.glob("*/*.md") if (r := score.read_frontmatter(md).get("rank", "")).isdigit()]
    return max(ranks, default=0) + 1


def manual_linkedin_job(args) -> dict:
    """抓取範圍外的 LinkedIn 職缺：有 jobId，但資料來自使用者給的描述文字檔，沒有刊登日與應徵人數。"""
    if not (args.title and args.company):
        sys.exit("手動補 LinkedIn 職缺需要 --title、--company（描述用 --file）。")
    description = args.file.read_text(encoding="utf-8").strip()
    if not description:
        sys.exit(f"{args.file} 是空的。")
    return {
        "jobId": args.job_id, "source": "linkedin", "title": args.title, "company": args.company,
        "location": args.location, "jobUrl": args.url or LINKEDIN_JOB_URL.format(args.job_id),
        "salaryDetails": args.salary, "description": description,
    }


def other_job(args) -> dict:
    """非 LinkedIn 職缺：jobId 只當評分快取的鍵（other- 開頭），不會寫進 property。"""
    if not (args.file and args.title and args.company):
        sys.exit("非 LinkedIn 職缺需要 --file、--title、--company。")
    description = args.file.read_text(encoding="utf-8").strip()
    if not description:
        sys.exit(f"{args.file} 是空的。")
    return {
        "jobId": f"other-{score.slug(args.company)}-{score.slug(args.title)}",
        "source": "other", "title": args.title, "company": args.company,
        "location": args.location, "jobUrl": args.url, "salaryDetails": args.salary,
        "description": description,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id", nargs="?", help="LinkedIn jobId；非 LinkedIn 職缺不給，改用 --file")
    parser.add_argument("--date", default="2026-10-04", help="要補進哪一天的短名單（YYYY-MM-DD）")
    parser.add_argument("--status", help="status property，例如 applied；預設 backlog，重寫既有 .md 時預設沿用原值")
    parser.add_argument("--db", type=Path, default=score.DB_PATH)
    parser.add_argument("--file", type=Path, help="非 LinkedIn 職缺：職缺描述文字檔")
    parser.add_argument("--title")
    parser.add_argument("--company")
    parser.add_argument("--location")
    parser.add_argument("--url")
    parser.add_argument("--salary")
    args = parser.parse_args()
    if not (args.job_id or args.file):
        sys.exit("請給 LinkedIn jobId，或用 --file 加入非 LinkedIn 職缺（兩者都給＝抓取範圍外的 LinkedIn 職缺）。")

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("找不到 ANTHROPIC_API_KEY，請在 .env 設定或匯出環境變數。")

    day_dir = score.SHORTLIST_DIR / args.date
    day_dir.mkdir(parents=True, exist_ok=True)
    if args.job_id:
        if score.find_job_folder(day_dir, args.job_id):
            sys.exit(f"{day_dir} 已經有 jobId {args.job_id}，不重複加入。")
        if args.file:
            job, scraped_date = manual_linkedin_job(args), None
        else:
            job, scraped_date = find_job(args.job_id)
        rank = next_rank(day_dir)
        folder = day_dir / f"{rank:02d}_{score.slug(job['company'])}_{score.slug(job['title'])}"
    else:
        job, scraped_date, rank = other_job(args), None, None
        folder = day_dir / f"{score.slug(job['company'])}_{score.slug(job['title'])}"
    md = folder / f"{folder.name}.md"
    reuse = not args.job_id and md.exists() and "## SUMMARY" not in md.read_text(encoding="utf-8")
    if folder.exists() and not reuse:
        sys.exit(f"{folder} 已經存在，不重複加入。")
    status = [args.status] if args.status else ((score.read_status(md) if reuse else None) or ["backlog"])
    user_note = score.read_user_note(md) if reuse else ""
    interest = score.read_interest(md) if reuse else ""

    profiles = score.load_profiles()
    config = score.load_config()
    client = anthropic.Anthropic()
    conn = score.init_db(args.db)
    score.init_score_tables(conn)
    usages = {"初評": score.Usage(), "詳評": score.Usage(), "摘要": score.Usage()}

    triage = score.triage_job(conn, client, config, profiles, usages["初評"], job)
    if triage is None:
        sys.exit("初評失敗，沒有寫入。")
    job = {**job, "triage": triage}
    if reuse and (custom_resume := score.read_frontmatter(md).get("resume")):
        job["resume_file"] = custom_resume  # 使用者已放進資料夾的客製履歷，不被路線預設履歷蓋掉
    job["detail"] = score.detail_job(conn, client, config, profiles, usages["詳評"], job)
    job["summary"] = score.summarize_job(client, config, usages["摘要"], job)

    folder.mkdir(exist_ok=True)
    md.write_text(score.format_job_note(rank, job, args.date, scraped_date, status, user_note, interest), encoding="utf-8")
    if args.job_id:
        conn.execute(
            "INSERT OR IGNORE INTO jobs (job_id, company_key, description_hash, first_seen_date) VALUES (?, ?, ?, ?)",
            (args.job_id, dedup.company_key(job), dedup.description_hash(job["description"]), args.date))
        conn.execute("UPDATE jobs SET recommended_date = ? WHERE job_id = ?", (args.date, args.job_id))
        conn.commit()
    conn.close()

    print(f"已寫入：{folder}/{folder.name}.md（rank {rank or '無'}，最佳 {triage['best_track']} {triage['best_score']} 分）")
    for label, u in usages.items():
        print(f"{label}用量：{u.line()}")
