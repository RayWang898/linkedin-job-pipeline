"""
一次性回補：抓過去 N 天內刊登的相關職缺，跑完 Step 1–6，短名單放進獨立資料夾 data/7_shortlist/<執行日>-backfill/。
設計見 architecture/01_scraping.md「兩週回補」。與每日流程分開，不寫 daily_runs。

做法：
- 每組搜尋各自抓（datePostedSeconds = N 天，上限 --cap），多組並行（--concurrency）。
- 每組抓完立刻存到 data/1_raw/backfill_<日期>/<組別>.json；中斷後用同一個 --run-date 重跑，已存的組別會跳過。
- 全部抓完後合併成 data/1_raw/raw_backfill_<日期>.json，再依序跑瘦身、去重、篩選、評分（沿用既有腳本）。
- 一次只抓一個地點：預設是 config 的第一個 locations，用 --location 指定其他地點（搭配 --folder-suffix 與新的 --run-date）。

用法：
  venv/bin/python scripts/execution-python/backfill.py --dry-run              # 只印計畫，不呼叫任何 API
  venv/bin/python scripts/execution-python/backfill.py                        # 完整回補（抓取約數小時，建議在背景執行）
  venv/bin/python scripts/execution-python/backfill.py --queries B,C --cap 100   # 小量測試
  venv/bin/python scripts/execution-python/backfill.py --fetch-only           # 只抓取與合併，不評分
  venv/bin/python scripts/execution-python/backfill.py --run-date 20261005    # 續跑先前中斷的回補（同一批次）
  venv/bin/python scripts/execution-python/backfill.py --location "London, UK" --folder-suffix backfill-london --run-date 20261005lon   # 倫敦回補
  venv/bin/python scripts/execution-python/backfill.py --queries A,E --run-date 20261005b   # 另一批：用新的 --run-date，檔案與統計檔都不會蓋掉前一批
需要環境變數 APIFY_TOKEN、ANTHROPIC_API_KEY（可放在 .env）。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent
os.chdir(ROOT)  # 各步驟腳本都用相對路徑
sys.path.insert(0, str(SCRIPTS_DIR))

import fetch  # noqa: E402

RAW_DIR = Path("data/1_raw")
SECONDS_PER_DAY = 86400
RUN_TIMEOUT_SECS = 8 * 3600  # 單組搜尋的執行時間上限；Apify 預設的逾時對上千筆詳情太短
SECS_PER_JOB = 6.4  # 實測：抓詳情約每筆 6 秒（見 01_scraping.md）


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def part_path(run_date: str, label: str) -> Path:
    return RAW_DIR / f"backfill_{run_date}" / f"{label}.json"


def fetch_one(client, apify_cfg: dict, location: str, label: str, query: str, days: int, cap: int,
              run_date: str, backoff: list[int]) -> tuple[str, int]:
    """抓單一組搜尋並存檔，回傳 (組別, 筆數)。失敗依 backoff 重試，仍失敗就丟出例外。"""
    primary_cfg = {**apify_cfg, "locations": [location]}
    run_input = fetch.build_run_input(primary_cfg, query, cap, label)
    run_input.pop("datePosted", None)
    run_input.update({"datePostedSeconds": days * SECONDS_PER_DAY, "maxJobsPerSearch": cap, "maxJobs": cap})
    last_error = None
    for wait in [0] + backoff:
        if wait:
            log(f"  搜尋 {label} 等待 {wait} 秒後重試")
            time.sleep(wait)
        try:
            log(f"搜尋 {label} 開始（過去 {days} 天，上限 {cap}）")
            run = client.actor(apify_cfg["actor"]).call(run_input=run_input, run_timeout=timedelta(seconds=RUN_TIMEOUT_SECS))
            if run is None or run.status not in ("SUCCEEDED", "TIMED-OUT"):
                raise RuntimeError(f"Apify 執行未成功（狀態：{getattr(run, 'status', '未知')}）")
            items = list(client.dataset(run.default_dataset_id).iterate_items())
            if run.status == "TIMED-OUT":
                log(f"  ⚠️ 搜尋 {label} 逾時，只保留已抓到的 {len(items)} 筆")
            path = part_path(run_date, label)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            return label, len(items)
        except Exception as e:
            last_error = e
            log(f"  ⚠️ 搜尋 {label} 失敗：{str(e)[:150]}")
    raise RuntimeError(f"搜尋 {label} 放棄：{last_error}")


def run_script(script: str, args: list[str]) -> None:
    cmd = [sys.executable, str(SCRIPTS_DIR / script), *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    for line in proc.stdout:
        print(f"  | {line.rstrip()}", flush=True)
    if proc.wait() != 0:
        raise RuntimeError(f"{script} 結束碼 {proc.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14, help="往前抓幾天（預設 14）")
    parser.add_argument("--cap", type=int, default=1000, help="每組搜尋的筆數上限（LinkedIn 單次搜尋約 1,000 筆為止）")
    parser.add_argument("--concurrency", type=int, default=3, help="同時執行幾組搜尋（受 Apify 帳號記憶體限制）")
    parser.add_argument("--queries", help="只抓指定組別，逗號分隔，例如 B,C（預設全部）")
    parser.add_argument("--location", help="要回補的地點（預設 config 的第一個地點），例如 \"London, UK\"")
    parser.add_argument("--folder-suffix", default="backfill", help="短名單資料夾後綴（預設 backfill；其他地點建議 backfill-london）")
    parser.add_argument("--run-date", default=date.today().strftime("%Y%m%d"), help="回補批次的日期（續跑時用原本的）")
    parser.add_argument("--fetch-only", action="store_true", help="只抓取與合併，不跑瘦身以後的步驟")
    parser.add_argument("--shortlist-size", type=int, default=40, help="回補短名單的一般名單上限")
    parser.add_argument("--dry-run", action="store_true", help="只印計畫，不呼叫任何 API")
    args = parser.parse_args()

    config = fetch.load_config()
    apify_cfg = config["apify"]
    labels = fetch.query_labels(apify_cfg)
    queries = dict(zip(labels, apify_cfg["queries"]))
    wanted = [l.strip().upper() for l in args.queries.split(",")] if args.queries else labels
    unknown = [l for l in wanted if l not in queries]
    if unknown:
        sys.exit(f"沒有這些組別：{', '.join(unknown)}（可用：{', '.join(labels)}）")
    done = [l for l in wanted if part_path(args.run_date, l).exists()]
    todo = [l for l in wanted if l not in done]
    worst_secs = max((args.cap * SECS_PER_JOB for _ in todo), default=0)
    total_secs = len(todo) * args.cap * SECS_PER_JOB / max(1, args.concurrency)

    location = args.location or apify_cfg["locations"][0]
    log(f"回補批次 {args.run_date}：地點 {location}，過去 {args.days} 天，每組上限 {args.cap}")
    log(f"已完成（跳過）：{', '.join(done) or '無'}；待抓：{', '.join(todo) or '無'}，並行 {args.concurrency}")
    log(f"抓取時間估計：最久的一組約 {worst_secs / 3600:.1f} 小時，全部約 {total_secs / 3600:.1f} 小時（最壞情況，各組未必抓滿）")
    if args.dry_run:
        log("dry-run：不呼叫 API，結束。")
        return

    load_dotenv()
    token = os.environ.get("APIFY_TOKEN")
    if todo and not token:
        sys.exit("找不到 APIFY_TOKEN，請在 .env 設定或匯出環境變數。")
    if not args.fetch_only and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("找不到 ANTHROPIC_API_KEY，請在 .env 設定或匯出環境變數。")

    failed = []
    if todo:
        from apify_client import ApifyClient

        client = ApifyClient(token)
        backoff = config["daily"]["retry_backoff_seconds"]
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            futures = {pool.submit(fetch_one, client, apify_cfg, location, l, queries[l], args.days, args.cap,
                                   args.run_date, backoff): l for l in todo}
            for fut in as_completed(futures):
                label = futures[fut]
                try:
                    _, n = fut.result()
                    log(f"搜尋 {label} 完成：{n} 筆（已存 {part_path(args.run_date, label)}）")
                except Exception as e:
                    failed.append(label)
                    log(f"❌ {e}")
    if failed:
        log(f"放棄的組別：{', '.join(failed)}。用同一個 --run-date {args.run_date} 重跑即可補抓這些組別（已完成的會跳過）。")

    results = {l: json.loads(part_path(args.run_date, l).read_text(encoding="utf-8"))
               for l in labels if part_path(args.run_date, l).exists()}
    if not results:
        sys.exit("沒有任何組別抓到資料，不產生檔案。")
    items = fetch.merge_results(results)
    log(f"合併去重後 {len(items)} 筆（各組合計 {sum(len(v) for v in results.values())}，組別：{', '.join(results)}）")
    raw_path = fetch.save_raw(items, f"backfill_{args.run_date}")
    fetch.process_raw_file(raw_path)
    if args.fetch_only:
        log("fetch-only：已完成抓取與瘦身，沒有跑去重、篩選與評分。")
        return

    name = raw_path.name
    run_script("dedup.py", ["--only", name])
    run_script("filter.py", ["--only", name])
    run_script("score.py", ["--only", name, "--folder-suffix", args.folder_suffix, "--shortlist-size", str(args.shortlist_size),
                            "--summary-name", f"_summary_backfill_{args.run_date}.md"])
    log(f"回補完成。短名單在 data/7_shortlist/{date.today().isoformat()}-{args.folder_suffix}/")


if __name__ == "__main__":
    main()
