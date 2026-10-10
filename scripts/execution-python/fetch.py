"""
階段一、二：抓取與瘦身。
Step 1：呼叫 Apify actor 抓 LinkedIn 公開職缺（不登入），原始資料存到 data/1_raw/raw_<YYYYMMDD>.json。
Step 2：只保留必要欄位，存到 data/2_slim/<同檔名>.json。

用法：
  venv/bin/python scripts/execution-python/fetch.py                      # 呼叫 Apify 抓取後瘦身
  venv/bin/python scripts/execution-python/fetch.py --max-jobs 5         # 測試用：每組搜尋最多 5 筆
  venv/bin/python scripts/execution-python/fetch.py --from-samples       # 不呼叫 Apify，改瘦身 samples/*.json
需要環境變數 APIFY_TOKEN（可放在 .env）。
"""

import argparse
import json
import os
import string
import sys
import time
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

SAMPLES_DIR = Path("samples")
RAW_DIR = Path("data/1_raw")
SLIM_DIR = Path("data/2_slim")
CONFIG_PATH = Path("config.yaml")

ESSENTIAL_FIELDS = [
    "jobId",
    "title",
    "company",
    "companyId",
    "location",
    "postedDate",
    "description",
    "applicantsDetails",
    "applicationMethod",
    "jobUrl",
    "salaryDetails",
    "recruiter",
    "matched_queries",  # 本程式在 Step 1 加上：抓到這筆的搜尋組別（A、B…）
]


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------- Step 1：抓取 ----------

def query_labels(apify_cfg: dict) -> list[str]:
    """每組搜尋依序標 A、B、C…，與 config.yaml 裡 queries 的順序一致。"""
    return list(string.ascii_uppercase[: len(apify_cfg["queries"])])


def max_jobs_for(apify_cfg: dict, label: str | None) -> int:
    """該組搜尋的筆數上限：max_jobs_by_query 有列就用它，否則用 max_jobs_per_search。
    單一地點的搜尋（apify_cfg["locations"] 只有一個）若在 max_jobs_by_location 有列，再取兩者較小值。"""
    cap = apify_cfg.get("max_jobs_by_query", {}).get(label, apify_cfg["max_jobs_per_search"])
    locations = apify_cfg["locations"]
    if len(locations) == 1:
        loc_cap = apify_cfg.get("max_jobs_by_location", {}).get(locations[0])
        if loc_cap:
            cap = min(cap, loc_cap)
    return cap


def location_tag(apify_cfg: dict, location: str) -> str:
    """第一個地點不加標記（與舊紀錄一致），其他地點在組別後面加 @城市，用在失敗清單與日誌。"""
    return "" if location == apify_cfg["locations"][0] else f"@{location.split(',')[0]}"


def build_run_input(apify_cfg: dict, query: str, max_jobs: int | None = None, label: str | None = None) -> dict:
    """組出 actor 的輸入（單一搜尋）。maxJobs 預設為每組上限乘地點數，否則 actor 預設只回 25 筆。"""
    per_search = max_jobs_for(apify_cfg, label)
    return {
        "queries": [query],
        "locations": apify_cfg["locations"],
        "datePosted": apify_cfg["date_posted"],
        "sortBy": apify_cfg["sort_by"],
        "fetchDetails": apify_cfg["fetch_details"],
        "includeSimilar": False,  # 相似職缺在 Step 2 會被丟掉，開了只會多花錢
        "maxJobsPerSearch": per_search,
        "maxJobs": max_jobs if max_jobs else per_search * len(apify_cfg["locations"]),
    }


def merge_results(results: dict[str, list[dict]]) -> list[dict]:
    """各組結果依 jobId 合併；同一筆被多組抓到時，matched_queries 記下所有組別。"""
    merged: dict[str, dict] = {}
    for label, items in results.items():
        for item in items:
            job = merged.setdefault(item["jobId"], {**item, "matched_queries": []})
            if label not in job["matched_queries"]:
                job["matched_queries"].append(label)
    return list(merged.values())


def run_query(client, apify_cfg: dict, name: str, query: str, max_jobs: int | None,
              label: str | None = None) -> list[dict]:
    """name 是日誌顯示用（可能帶地點標記），label 是查上限用的組別（A、B…）；沒給 label 就用 name。"""
    label = label or name
    run_input = build_run_input(apify_cfg, query, max_jobs, label)
    print(f"呼叫 Apify actor：{apify_cfg['actor']}（搜尋 {name}，maxJobs={run_input['maxJobs']}）")
    run = client.actor(apify_cfg["actor"]).call(run_input=run_input)
    if run is None or run.status != "SUCCEEDED":
        status = getattr(run, "status", "未知")
        raise RuntimeError(f"Apify 執行未成功（搜尋 {name}，狀態：{status}）")
    items = list(client.dataset(run.default_dataset_id).iterate_items())
    print(f"  搜尋 {name} 回傳筆數：{len(items)}")
    return items


def scrape(token: str, apify_cfg: dict, max_jobs: int | None = None,
           backoff: list[int] | None = None) -> tuple[list[dict], list[str]]:
    """逐組搜尋抓取，回傳 (合併後職缺, 放棄的組別)。

    backoff 為各次重試前等待的秒數（長度即重試次數）；只重試失敗的那一組，不重跑已成功的組別。
    沒有 backoff 時失敗的組別不重試，直接放棄。
    """
    from apify_client import ApifyClient  # 只有真的抓取時才需要

    client = ApifyClient(token)
    results = {}
    failed = []
    # 每個地點各自呼叫（各有自己的筆數上限）；同一組別在不同地點的結果合併在同一個組別標記下
    for location in apify_cfg["locations"]:
        loc_cfg = {**apify_cfg, "locations": [location]}
        tag = location_tag(apify_cfg, location)
        for label, query in zip(query_labels(apify_cfg), apify_cfg["queries"]):
            name = f"{label}{tag}"
            waits = [0] + list(backoff or [])
            for attempt, wait in enumerate(waits, 1):
                if wait:
                    print(f"  搜尋 {name} 等待 {wait} 秒後重試（第 {attempt - 1} 次重試）")
                    time.sleep(wait)
                try:
                    results.setdefault(label, []).extend(run_query(client, loc_cfg, name, query, max_jobs, label))
                    break
                except Exception as e:
                    print(f"  ⚠️ 搜尋 {name} 失敗：{e}")
            else:
                failed.append(name)
    items = merge_results(results)
    print(f"合併去重後：{len(items)}（各組合計 {sum(len(v) for v in results.values())}）")
    if failed:
        print(f"⚠️ 放棄的搜尋組別：{', '.join(failed)}")
    return items, failed


def save_raw(items: list[dict], run_date: str | None = None) -> Path:
    run_date = run_date or date.today().strftime("%Y%m%d")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"raw_{run_date}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"原始資料已存檔至：{path}")
    return path


# ---------- Step 2：瘦身 ----------

def slim_job(raw_job: dict) -> dict:
    return {field: raw_job.get(field) for field in ESSENTIAL_FIELDS}


def field_completeness(records: list[dict]) -> dict[str, int]:
    return {
        field: sum(1 for r in records if r.get(field) is not None)
        for field in ESSENTIAL_FIELDS
    }


def process_raw_file(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        raw_jobs = json.load(f)

    slimmed = [slim_job(job) for job in raw_jobs]

    total = len(raw_jobs)
    counts = field_completeness(slimmed)

    print(f"讀取：{path}")
    print(f"原始筆數：{total}")
    print(f"瘦身後筆數：{len(slimmed)}", end="")
    if len(slimmed) < total:
        print("  ⚠️ 筆數比原始少，可能有資料遺失！")
    else:
        print()
    print()
    print("欄位完整度：")
    for field, count in counts.items():
        pct = (count / total * 100) if total else 0
        print(f"  {field:<18} {count}/{total} ({pct:.0f}%)")

    SLIM_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SLIM_DIR / path.name
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(slimmed, f, ensure_ascii=False, indent=2)

    print()
    print(f"已存檔至：{out_path}")
    print("-" * 50)

    return slimmed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-samples", action="store_true", help="不呼叫 Apify，改瘦身 samples/*.json")
    parser.add_argument("--max-jobs", type=int, help="測試用：每組搜尋最多抓幾筆")
    args = parser.parse_args()

    if args.from_samples:
        sample_files = sorted(SAMPLES_DIR.glob("*.json"))
        if not sample_files:
            print(f"{SAMPLES_DIR}/ 底下沒有找到任何 .json 檔案。")
        for sample_path in sample_files:
            process_raw_file(sample_path)
        sys.exit(0)

    load_dotenv()
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("找不到 APIFY_TOKEN，請在 .env 設定或匯出環境變數。")

    config = load_config()
    items, failed = scrape(token, config["apify"], args.max_jobs)
    if not items:
        sys.exit("Apify 沒有回傳任何職缺，不產生檔案。")

    process_raw_file(save_raw(items))
