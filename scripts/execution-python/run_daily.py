"""
每日執行：依序跑完六步流程，失敗時重試，結束後用 ntfy 通知手機。設計見 architecture/07_scheduling.md。

只負責協調，步驟本身的邏輯都在 fetch.py、dedup.py、filter.py、score.py。

用法：
  venv/bin/python scripts/execution-python/run_daily.py                 # 正常每日執行（同一天已成功就略過）
  venv/bin/python scripts/execution-python/run_daily.py --force         # 同一天已成功也重跑
  venv/bin/python scripts/execution-python/run_daily.py --no-notify     # 不發通知（測試用）
  venv/bin/python scripts/execution-python/run_daily.py --max-jobs 5 --db test.db   # 測試：小量、用測試資料庫
需要環境變數 APIFY_TOKEN、ANTHROPIC_API_KEY、NTFY_TOPIC（可放在 .env）。
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent
os.chdir(ROOT)  # 各步驟腳本都用相對路徑，排程啟動時的工作目錄不一定是專案根目錄
sys.path.insert(0, str(SCRIPTS_DIR))

import fetch  # noqa: E402
from dedup import init_db  # noqa: E402

LOG_DIR = Path("logs")
DB_PATH = Path("jobs.db")
CONFIG_PATH = Path("config.yaml")
TRACK_LABELS = ["A", "B"]
STALE_RUNNING_HOURS = 3  # 超過這個時間還標記為 running，視為上次異常中斷

_log_file = None


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if _log_file:
        _log_file.write(line + "\n")
        _log_file.flush()


class StepFailed(Exception):
    def __init__(self, step: str, detail: str):
        super().__init__(f"{step}：{detail}")
        self.step = step
        self.detail = detail


# ---------- daily_runs 表 ----------

def init_runs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_runs (
            run_date TEXT PRIMARY KEY,
            status TEXT NOT NULL,          -- running / success / failed
            started_at TEXT NOT NULL,
            finished_at TEXT,
            raw_count INTEGER,
            failed_queries TEXT,           -- 放棄的搜尋組別，逗號分隔
            main_count INTEGER,
            custom_count INTEGER,
            by_track TEXT,                 -- JSON：短名單依最佳 track 分組的筆數
            tokens TEXT,                   -- JSON：初評與詳評合計的 token 用量
            error TEXT
        )
        """
    )
    conn.commit()


def run_status(conn: sqlite3.Connection, run_date: str) -> tuple[str, str] | None:
    return conn.execute(
        "SELECT status, started_at FROM daily_runs WHERE run_date = ?", (run_date,)
    ).fetchone()


def days_since_last_success(conn: sqlite3.Connection, run_date: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(run_date) FROM daily_runs WHERE status = 'success' AND run_date < ?", (run_date,)
    ).fetchone()
    if not row or not row[0]:
        return None
    return (date.fromisoformat(run_date) - date.fromisoformat(row[0])).days


def start_run(conn: sqlite3.Connection, run_date: str) -> None:
    conn.execute("DELETE FROM daily_runs WHERE run_date = ?", (run_date,))
    conn.execute(
        "INSERT INTO daily_runs (run_date, status, started_at) VALUES (?, 'running', ?)",
        (run_date, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_date: str, status: str, **fields) -> None:
    fields = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
    fields.update(status=status, finished_at=datetime.now().isoformat(timespec="seconds"))
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE daily_runs SET {cols} WHERE run_date = ?", [*fields.values(), run_date])
    conn.commit()


# ---------- 步驟執行與重試 ----------

def with_retries(step: str, backoff: list[int], fn):
    """fn 失敗（丟例外）就依 backoff 等待後重試；用完仍失敗丟 StepFailed。"""
    waits = [0] + list(backoff)
    last = None
    for attempt, wait in enumerate(waits, 1):
        if wait:
            log(f"{step}：等待 {wait} 秒後重試（第 {attempt - 1} 次重試）")
            time.sleep(wait)
        try:
            return fn()
        except Exception as e:
            last = e
            log(f"{step}：第 {attempt} 次嘗試失敗：{e}")
    raise StepFailed(step, f"重試 {len(backoff)} 次後仍失敗（{last}）")


def run_script(script: str, args: list[str]) -> None:
    cmd = [sys.executable, str(SCRIPTS_DIR / script), *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    for line in proc.stdout:
        log(f"  | {line.rstrip()}")
    if proc.wait() != 0:
        raise RuntimeError(f"{script} 結束碼 {proc.returncode}")


# ---------- 校準清單 ----------

def write_calibration(run_date: str) -> None:
    """跑 calibrate.py，輸出存到 feedback/calibrate_<日期>.md。失敗只記日誌，不影響流程結果。"""
    day = run_date.replace("-", "")
    out = Path("feedback") / f"calibrate_{day}.md"
    try:
        out.parent.mkdir(exist_ok=True)
        proc = subprocess.run([sys.executable, str(SCRIPTS_DIR / "calibrate.py"), day],
                              capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"結束碼 {proc.returncode}")
        out.write_text(proc.stdout, encoding="utf-8")
        log(f"校準清單已存至 {out}")
    except Exception as e:
        log(f"⚠️ 校準清單產生失敗（不影響結果）：{e}")


# ---------- 通知 ----------

def fmt_tokens(tokens: dict) -> str:
    inp = tokens.get("input_tokens", 0) + tokens.get("cache_creation_input_tokens", 0) + tokens.get("cache_read_input_tokens", 0)
    return f"輸入 {inp / 1000:.1f}k／輸出 {tokens.get('output_tokens', 0) / 1000:.1f}k"


def success_message(stats: dict, failed_queries: list[str], gap: int | None) -> str:
    total = stats["main"] + stats["custom"]
    tracks = "・".join(f"{t} {stats['by_track'].get(t, 0)}" for t in TRACK_LABELS)
    parts = [f"短名單 {total} 筆", tracks]
    if stats["custom"]:
        parts.append(f"需客製 {stats['custom']}")
    parts.append(f"tokens {fmt_tokens(stats['tokens'])}")
    if stats.get("failures"):
        parts.append(f"評分失敗 {stats['failures']} 次")
    if failed_queries:
        parts.append(f"抓取不完整，缺 {'、'.join(failed_queries)} 組")
    if gap and gap > 1:
        parts.append(f"距上次成功 {gap} 天")
    return "｜".join(parts)


def notify(topic: str | None, message: str, failure: bool = False) -> None:
    """通知失敗只記日誌，不影響流程結果。內容只放數字，不放公司與職稱。"""
    if not topic:
        log("⚠️ 沒有設定 NTFY_TOPIC，略過通知。")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": "Job Search FAILED" if failure else "Job Search", "Tags": "x" if failure else "briefcase"}
    if failure:
        headers["Priority"] = "high"
    try:
        r = requests.post(f"{server}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=15)
        r.raise_for_status()
        log(f"已通知：{message}")
    except Exception as e:
        log(f"⚠️ 通知失敗（不影響結果）：{e}")


# ---------- 主流程 ----------

def pipeline(args, cfg: dict, token: str, run_date: str) -> dict:
    daily = cfg["daily"]
    backoff = daily["retry_backoff_seconds"]
    name = f"raw_{run_date.replace('-', '')}.json"
    db_args = ["--db", str(args.db)]

    # Step 1–2：抓取只重試失敗的那一組，不重跑已成功的組別
    def do_scrape():
        return fetch.scrape(token, cfg["apify"], args.max_jobs, backoff)

    items, failed = with_retries("Step 1 抓取", [], do_scrape)
    if not items:
        if failed:
            raise StepFailed("Step 1 抓取", f"所有搜尋組別都失敗（{', '.join(failed)}）")
        log("Apify 沒有回傳任何職缺，今天沒有新資料。")
        return {"raw_count": 0, "failed_queries": [], "stats": {"main": 0, "custom": 0, "by_track": {}, "tokens": {}, "failures": 0}}
    cap = daily["max_total_jobs"]
    if len(items) > cap:
        log(f"⚠️ 抓到 {len(items)} 筆，超過整體上限 {cap}，只保留前 {cap} 筆。")
        items = items[:cap]
    raw_path = fetch.save_raw(items, run_date.replace("-", ""))
    with_retries("Step 2 瘦身", backoff, lambda: fetch.process_raw_file(raw_path))

    stats_path = LOG_DIR / f"stats_{run_date}.json"
    stats_path.unlink(missing_ok=True)
    with_retries("Step 3 去重", backoff, lambda: run_script("dedup.py", ["--only", name, *db_args]))
    with_retries("Step 4 篩選", backoff, lambda: run_script("filter.py", ["--only", name, *db_args]))
    with_retries("Step 5–6 評分與輸出", backoff,
                 lambda: run_script("score.py", ["--only", name, "--stats-json", str(stats_path), *db_args]))
    if not stats_path.exists():
        raise StepFailed("Step 5–6 評分與輸出", "沒有產生統計檔（沒有符合的輸入檔？）")
    return {
        "raw_count": len(items),
        "failed_queries": failed,
        "stats": json.loads(stats_path.read_text(encoding="utf-8")),
    }


def cleanup_logs(keep_days: int) -> None:
    cutoff = time.time() - keep_days * 86400
    for f in LOG_DIR.glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()


def main() -> int:
    global _log_file
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="同一天已成功也重跑")
    parser.add_argument("--no-notify", action="store_true", help="不發通知（測試用）")
    parser.add_argument("--max-jobs", type=int, help="測試用：每組搜尋最多抓幾筆")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="資料庫路徑（測試時用 test.db）")
    args = parser.parse_args()

    load_dotenv()
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    run_date = date.today().isoformat()
    LOG_DIR.mkdir(exist_ok=True)
    _log_file = (LOG_DIR / f"{run_date}.log").open("a", encoding="utf-8")
    cleanup_logs(cfg["daily"]["log_keep_days"])
    topic = None if args.no_notify else os.environ.get("NTFY_TOPIC")

    conn = init_db(args.db)
    init_runs_table(conn)
    prior = run_status(conn, run_date)
    if prior and not args.force:
        status, started = prior
        if status == "success":
            log(f"{run_date} 已成功執行過，略過（要重跑請加 --force）。")
            return 0
        age = datetime.now() - datetime.fromisoformat(started)
        if status == "running" and age < timedelta(hours=STALE_RUNNING_HOURS):
            log(f"{run_date} 已有執行中的流程（{started} 開始），略過。")
            return 0

    log(f"===== 每日執行 {run_date} 開始 =====")
    gap = days_since_last_success(conn, run_date)
    start_run(conn, run_date)
    try:
        for key in ("APIFY_TOKEN", "ANTHROPIC_API_KEY"):
            if not os.environ.get(key):
                raise StepFailed("環境設定", f"找不到 {key}")
        result = pipeline(args, cfg, os.environ["APIFY_TOKEN"], run_date)
    except StepFailed as e:
        log(f"❌ 失敗：{e}")
        finish_run(conn, run_date, "failed", error=str(e))
        if not args.no_notify:
            notify(topic, f"每日執行失敗：{e.step}（{e.detail}）", failure=True)
        return 1
    except Exception as e:  # 預期外的錯誤也要通知，不然你只會看到沉默
        log(f"❌ 預期外錯誤：{e!r}")
        finish_run(conn, run_date, "failed", error=repr(e))
        notify(topic, f"每日執行發生預期外錯誤：{e!r}", failure=True)
        return 1

    stats = result["stats"]
    finish_run(conn, run_date, "success", raw_count=result["raw_count"],
               failed_queries=",".join(result["failed_queries"]), main_count=stats["main"],
               custom_count=stats["custom"], by_track=stats["by_track"], tokens=stats["tokens"])
    if result["raw_count"]:
        write_calibration(run_date)
    message = success_message(stats, result["failed_queries"], gap)
    log(f"===== 完成：{message} =====")
    notify(topic, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
