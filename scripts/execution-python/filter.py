"""
階段三：篩選。用規則做硬篩，不用 AI。
依序套用：地點白名單 -> 應徵人數上限 -> 職稱排除 -> 簽證排除 -> 要求年資 -> 排除關鍵字 -> 排除人力仲介 -> 投遞紀錄查重。
任一條件不通過就整筆排除，並記錄原因。
地點白名單之外的職缺，額外用 Nominatim 算距離中心多遠，只當作診斷資訊印出來，不影響篩選結果。
"""

import argparse
import json
import re
import sqlite3
import time
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import requests
import yaml

DEDUPED_DIR = Path("data/3_deduped")
FILTERED_DIR = Path("data/4_filtered")
EXCLUDED_DIR = Path("data/5_excluded")
DB_PATH = Path("jobs.db")
CONFIG_PATH = Path("config.yaml")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "linkedin-job-search-tool (personal use)"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def init_geocode_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geocode_cache (
            address TEXT PRIMARY KEY,
            lat REAL,
            lon REAL
        )
        """
    )
    conn.commit()


def geocode(address: str, conn: sqlite3.Connection) -> tuple[float, float] | None:
    row = conn.execute(
        "SELECT lat, lon FROM geocode_cache WHERE address = ?", (address,)
    ).fetchone()
    if row:
        return (row[0], row[1]) if row[0] is not None else None

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        time.sleep(1)  # Nominatim 使用政策：每秒最多 1 次請求
    except Exception as e:
        print(f"  ⚠️ 無法定位「{address}」：{e}")
        conn.execute(
            "INSERT INTO geocode_cache (address, lat, lon) VALUES (?, NULL, NULL)",
            (address,),
        )
        conn.commit()
        return None

    if not results:
        conn.execute(
            "INSERT INTO geocode_cache (address, lat, lon) VALUES (?, NULL, NULL)",
            (address,),
        )
        conn.commit()
        return None

    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    conn.execute(
        "INSERT INTO geocode_cache (address, lat, lon) VALUES (?, ?, ?)",
        (address, lat, lon),
    )
    conn.commit()
    return (lat, lon)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8  # 地球半徑（英里）
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def passes_location(job: dict, config: dict) -> bool:
    location = (job.get("location") or "").lower()
    return any(entry.lower() in location for entry in config["location_whitelist"])


def effective_applicant_count(applicants_details: dict | None) -> int | None:
    if not applicants_details:
        return None
    kind = applicants_details.get("kind")
    if kind == "exact":
        return applicants_details.get("count")
    if kind == "upper_bound":
        return applicants_details.get("max")
    if kind == "lower_bound":
        return applicants_details.get("min")
    return None


def passes_applicant_cap(job: dict, config: dict) -> bool:
    count = effective_applicant_count(job.get("applicantsDetails"))
    if count is None:
        return True  # 資料缺漏時不因此排除，但會在報告中提醒
    return count <= config["max_applicants"]


def passes_keywords(job: dict, config: dict) -> bool:
    haystack = f"{job.get('title', '')} {job.get('description', '')}".lower()
    return not any(kw.lower() in haystack for kw in config["exclude_keywords"])


def passes_staffing_agency(job: dict, config: dict) -> bool:
    if not config.get("exclude_staffing_agencies"):
        return True
    company = (job.get("company") or "").lower()
    return not any(agency.lower() in company for agency in config["staffing_agency_blacklist"])


def passes_title(job: dict, config: dict) -> bool:
    # 括號內是選填，例如「(Senior) Product Builder」同時開給資深與非資深，不當成層級要求
    title = re.sub(r"\([^)]*\)", " ", job.get("title") or "")
    for pattern in config.get("exclude_title_patterns", []):
        if re.search(rf"\b{re.escape(pattern)}\b", title, re.IGNORECASE):
            return False
    return True


def passes_visa(job: dict, config: dict) -> bool:
    location = (job.get("location") or "").lower()
    if any(loc.lower() in location for loc in config.get("visa_skip_locations", [])):
        return True  # 簽證規則是針對美國 OPT；使用者在這些地點不需要 sponsor
    description = re.sub(r"\s+", " ", job.get("description") or "").lower()
    return not any(p.lower() in description for p in config.get("exclude_visa_patterns", []))


def passes_unpaid(job: dict, config: dict) -> bool:
    """無薪／志工職缺：職稱含關鍵字（整字比對，括號內也算），或描述符合 exclude_unpaid_patterns（正規表示式）。"""
    title = job.get("title") or ""
    for word in config.get("exclude_unpaid_title_words", []):
        if re.search(rf"\b{re.escape(word)}\b", title, re.IGNORECASE):
            return False
    description = job.get("description") or ""
    return not any(re.search(p, description, re.IGNORECASE) for p in config.get("exclude_unpaid_patterns", []))


NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_NUM = r"(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")"
# 「5+ years」「3-5 years」「five to seven years」「at least 5 years」「5 years or more」都取第一個數字。
YEARS_RE = re.compile(
    _NUM + r"(?:\s*\+|\s*(?:-|–|to)\s*" + _NUM + r")?\s*(?:or more |plus )?years?\b",
    re.IGNORECASE,
)
YEARS_CONTEXT_RE = re.compile(r"experience|exp\b", re.IGNORECASE)
# 「8+ years across …」這種寫法附近沒有 experience 字樣，但有「+」，視為要求；前後若有公司歷史字眼則不算
YEARS_HISTORY_RE = re.compile(
    r"history|legacy|in business|since|founded|serving|of service|of innovation|of excellence|old\b|anniversary|established|heritage",
    re.IGNORECASE,
)
YEARS_AFTER_WINDOW = 100  # experience 常在同一條要求的句尾，例如「5-7 years' Medical Marketing experience」
YEARS_SOFT_RE = re.compile(r"preferred|nice to have|a plus|bonus|desired", re.IGNORECASE)
MAX_PLAUSIBLE_YEARS = 15  # 超過視為公司歷史（例如「50 years of experience in the industry」），不當作要求


def _to_int(token: str) -> int:
    return int(token) if token.isdigit() else NUMBER_WORDS[token.lower()]


def required_years(description: str) -> int | None:
    """回傳 description 中明確要求的最低年資，沒有則回傳 None。

    多處出現時取最小者：職缺常寫「2+ years ... OR 6+ years」這類替代條件或分項要求，
    取最大值會誤殺；取最小值最多漏放，漏放的交給 Step 5 的「層級與年資」面向判斷。
    """
    text = re.sub(r"\s+", " ", description or "")
    found = []
    for m in YEARS_RE.finditer(text):
        before = text[max(0, m.start() - 40):m.start()]
        after = text[m.end():m.end() + YEARS_AFTER_WINDOW]
        has_context = YEARS_CONTEXT_RE.search(before) or YEARS_CONTEXT_RE.search(after)
        has_plus = "+" in m.group(0) and not YEARS_HISTORY_RE.search(before + after[:50])
        if not (has_context or has_plus):
            continue  # 附近沒有 experience 字樣（也不是「N+ years」），不是年資要求
        if YEARS_SOFT_RE.search(re.split(r"[.;]", after)[0][:40]):
            continue  # 「5+ years preferred」只是加分條件
        years = _to_int(m.group(1))
        if years <= MAX_PLAUSIBLE_YEARS:
            found.append(years)
    return min(found) if found else None


def passes_min_years(job: dict, config: dict) -> bool:
    limit = config.get("min_years_exclude")
    if not limit:
        return True
    years = required_years(job.get("description"))
    return years is None or years < limit


COMPANY_SUFFIXES ={"inc", "llc", "ltd", "corp", "co", "company"}
TRACKER_STATUS = {"已投遞": "applied", "已評估不投": "evaluated_skip"}


def normalize_text(text: str, drop_company_suffix: bool = False) -> str:
    text = re.sub(r"\([^)]*\)", " ", (text or "").lower())
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    if drop_company_suffix:
        words = [w for w in words if w not in COMPANY_SUFFIXES]
    return " ".join(words)


def load_tracker(path: Path) -> list[dict]:
    if not path.exists():
        print(f"  ⚠️ 找不到投遞紀錄 {path}，略過投遞紀錄查重。")
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        label, sep, body = re.split(r"([:：])", line, maxsplit=1) if re.search(r"[:：]", line) else (line, "", "")
        status = TRACKER_STATUS.get(label.strip())
        if not status:
            continue
        for item in body.split("｜"):
            if " · " not in item:
                continue
            title, company = item.rsplit(" · ", 1)
            entries.append({
                "title": title.strip(),
                "company": company.strip(),
                "title_key": normalize_text(title),
                "company_key": normalize_text(company, drop_company_suffix=True),
                "status": status,
            })
    return entries


def match_tracker(job: dict, tracker: list[dict]) -> tuple[dict | None, list[dict]]:
    """回傳（公司與職稱都相同的紀錄, 只有公司相同的紀錄清單）。"""
    company_key = normalize_text(job.get("company"), drop_company_suffix=True)
    title_key = normalize_text(job.get("title"))
    same_company = [e for e in tracker if e["company_key"] == company_key]
    exact = next((e for e in same_company if e["title_key"] == title_key), None)
    others = [e for e in same_company if e is not exact]
    return exact, others


def filter_job(job: dict, config: dict) -> str | None:
    """回傳 None 表示通過，否則回傳排除原因。"""
    if not passes_location(job, config):
        return "location"
    if not passes_applicant_cap(job, config):
        return "max_applicants"
    if not passes_title(job, config):
        return "exclude_title"
    if not passes_visa(job, config):
        return "exclude_visa"
    if not passes_unpaid(job, config):
        return "exclude_unpaid"
    if not passes_min_years(job, config):
        return "min_years"
    if not passes_keywords(job, config):
        return "exclude_keywords"
    if not passes_staffing_agency(job, config):
        return "staffing_agency"
    return None


def filter_batch(jobs: list[dict], config: dict, conn: sqlite3.Connection) -> dict:
    kept = []
    excluded_by_reason: dict[str, list[dict]] = {}

    center = geocode(config["location_center_address"], conn)
    tracker = load_tracker(Path(config["tracker_path"])) if config.get("tracker_path") else []

    for job in jobs:
        reason = filter_job(job, config)
        if reason is None:
            exact, others = match_tracker(job, tracker)
            if exact:
                reason = "already_in_tracker"
                job = {**job, "tracker_match": {"title": exact["title"], "status": exact["status"]}}
            else:
                if others:
                    job = {**job, "tracker_company_match": [
                        {"title": e["title"], "status": e["status"]} for e in others
                    ]}
                kept.append(job)
                continue

        excluded_by_reason.setdefault(reason, []).append(job)

        if reason == "location" and center:
            job_coords = geocode(job.get("location", ""), conn)
            if job_coords:
                dist = haversine_miles(*center, *job_coords)
                print(
                    f"  ⚠️ 排除「{job.get('title')}」@ {job.get('location')} "
                    f"（距離中心約 {dist:.0f} 英里，不在白名單內）"
                )

    return {"kept": kept, "excluded_by_reason": excluded_by_reason}


def process_deduped_file(conn: sqlite3.Connection, config: dict, path: Path) -> None:
    with path.open(encoding="utf-8") as f:
        jobs = json.load(f)

    result = filter_batch(jobs, config, conn)

    print(f"讀取：{path}")
    print(f"輸入筆數：{len(jobs)}")
    print(f"通過篩選：{len(result['kept'])}")
    for reason, items in result["excluded_by_reason"].items():
        print(f"排除（{reason}）：{len(items)}")

    FILTERED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = FILTERED_DIR / path.name
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result["kept"], f, ensure_ascii=False, indent=2)

    print(f"已存檔至：{out_path}")

    EXCLUDED_DIR.mkdir(parents=True, exist_ok=True)
    excluded_path = EXCLUDED_DIR / path.name
    excluded_records = [
        {"reason": reason, "job": job}
        for reason, items in result["excluded_by_reason"].items()
        for job in items
    ]
    with excluded_path.open("w", encoding="utf-8") as f:
        json.dump(excluded_records, f, ensure_ascii=False, indent=2)
    print(f"被排除職缺已存檔至：{excluded_path}")
    print("-" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH, help="資料庫路徑（測試時用 test.db）")
    parser.add_argument("--only", help="只處理指定檔名（每日執行用，例如 raw_20261004.json）")
    args = parser.parse_args()
    config = load_config()
    conn = sqlite3.connect(args.db)
    init_geocode_cache(conn)

    deduped_files = sorted(DEDUPED_DIR.glob("*.json"))
    if args.only:
        deduped_files = [f for f in deduped_files if f.name == args.only]
    if not deduped_files:
        print(f"{DEDUPED_DIR}/ 底下沒有找到任何 .json 檔案，請先執行 scripts/execution-python/dedup.py。")
    for deduped_path in deduped_files:
        process_deduped_file(conn, config, deduped_path)

    conn.close()
