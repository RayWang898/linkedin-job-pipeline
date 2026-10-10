"""
階段五、六：AI 評分並產出短名單。
Step 5 初評：讀 data/4_filtered/*.json，用 Haiku 對各條路線各給 0-5 分（profiles/ 提供背景）。
Step 6 輸出：依初評挑出短名單，對入選職缺做詳評（Sonnet），每筆職缺寫成 data/7_shortlist/<執行日>/<排名>_<公司>_<職稱>/<同名>.md，
並在 jobs.db 填上 recommended_date。
初評結果快取在 jobs.db 的 triage_scores 表（jobId + profiles 雜湊 + 模型都相同才沿用），
詳評結果快取在 detail_reviews 表。
設計見 architecture/05_scoring.md、06_output.md。

用法：venv/bin/python scripts/execution-python/score.py [--db test.db] [--limit N] [--no-detail]
需要環境變數 ANTHROPIC_API_KEY（可放在 .env）。
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import anthropic
import yaml

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)
import prompts
from dedup import init_db
from filter import effective_applicant_count, visa_settings

DATA_DIR = Path("data")
FILTERED_DIR = DATA_DIR / "4_filtered"
SCORED_DIR = DATA_DIR / "6_scored"
SHORTLIST_DIR = DATA_DIR / "7_shortlist"
PROFILES_DIR = Path("profiles")
DB_PATH = Path("jobs.db")
CONFIG_PATH = Path("config.yaml")

TRACK_FIELDS = ("id", "name", "tag", "profile", "resume")


def load_tracks(path: Path = CONFIG_PATH) -> dict:
    """Read the `tracks:` list from config.yaml. Returns {id: {name, tag, file, resume}} in config order."""
    if not path.exists():
        sys.exit(f"{path} not found in {Path.cwd()}. Copy config.example.yaml there, or pass --workspace.")
    with path.open(encoding="utf-8") as f:
        entries = (yaml.safe_load(f) or {}).get("tracks")
    if not isinstance(entries, list) or not entries:
        sys.exit(f"{path}: `tracks:` must be a list with at least one track (see config.example.yaml).")
    tracks = {}
    for n, entry in enumerate(entries, 1):
        missing = [k for k in TRACK_FIELDS if not isinstance(entry, dict) or not str(entry.get(k) or "").strip()]
        if missing:
            sys.exit(f"{path}: tracks entry {n} is missing {', '.join(missing)}.")
        tid = str(entry["id"]).strip()
        if not re.fullmatch(r"[A-Z]", tid):
            sys.exit(f"{path}: track id {tid!r} must be a single uppercase letter.")
        if tid in tracks:
            sys.exit(f"{path}: track id {tid} is used twice.")
        tracks[tid] = {"name": str(entry["name"]).strip(), "tag": str(entry["tag"]).strip(),
                       "file": str(entry["profile"]).strip(), "resume": str(entry["resume"]).strip()}
    return tracks


TRACKS = load_tracks()
with CONFIG_PATH.open(encoding="utf-8") as _f:
    _config = yaml.safe_load(_f)
VISA_ENABLED = visa_settings(_config)["enabled"]
OUTPUT_LANGUAGE = (_config.get("scoring") or {}).get("output_language") or "English"
P = prompts.load(OUTPUT_LANGUAGE)  # all model-facing text; see prompts.py
SHARED_FILE = "shared.md"
UNKNOWN_APPLICANTS = 10**9  # 應徵人數未知時，排序放最後

TRIAGE_TOOL = {
    "name": "submit_triage",
    "description": P["triage_tool_description"],
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "object",
                "properties": {t: {"type": "integer", "minimum": 0, "maximum": 5} for t in TRACKS},
                "required": list(TRACKS),
            },
            "custom_family": {"type": "boolean", "description": P["custom_family_description"]},
            "reason": {"type": "string", "description": P["reason_description"]},
        },
        "required": ["scores", "custom_family", "reason"],
    },
}
if VISA_ENABLED:  # the visa feature adds one field; with it off, the schema has no visa_blocked
    _props = {
        "scores": TRIAGE_TOOL["input_schema"]["properties"]["scores"],
        "custom_family": TRIAGE_TOOL["input_schema"]["properties"]["custom_family"],
        "visa_blocked": {"type": "boolean", "description": P["visa_blocked_description"]},
        "reason": {"type": "string", "description": P["reason_description_visa"]},
    }
    TRIAGE_TOOL["input_schema"]["properties"] = _props
    TRIAGE_TOOL["input_schema"]["required"] = ["scores", "custom_family", "visa_blocked", "reason"]

DETAIL_TOOL = {
    "name": "submit_detail",
    "description": P["detail_tool_description"],
    "input_schema": {
        "type": "object",
        "properties": {
            "resume": {"type": "string", "enum": list(TRACKS), "description": P["resume_description"]},
            "emphasize": {"type": "array", "items": {"type": "string"}, "maxItems": 3, "description": P["emphasize_description"]},
            "gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 3, "description": P["gaps_description"]},
        },
        "required": ["resume", "emphasize", "gaps"],
    },
}

SUMMARY_TOOL = {
    "name": "submit_summary",
    "description": P["summary_tool_description"],
    "input_schema": {
        "type": "object",
        "properties": {"summary": {"type": "string", "description": P["summary_field_description"]}},
        "required": ["summary"],
    },
}

SUMMARY_INSTRUCTIONS = P["summary_instructions"]
RUBRIC = P["rubric"] + ("\n\n" + P["visa_rubric"] if VISA_ENABLED else "")
DETAIL_INSTRUCTIONS = P["detail_instructions"]


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    visa_settings(config)  # fail early on an outdated config
    return config


# ---------- profiles ----------

def load_profiles(directory: Path = PROFILES_DIR) -> dict:
    """回傳 {'shared': str, 'tracks': {'A': str, ...}, 'hash': str}。triage_hash／detail_hash 各涵蓋 profiles 加上該階段的規則文字（初評規則、詳評規則與工具定義）。"""
    needed = {SHARED_FILE: "shared profile", **{info["file"]: f"profile for track {t}" for t, info in TRACKS.items()}}
    for name, what in needed.items():
        if not (directory / name).is_file():
            sys.exit(f"Missing {what}: {directory / name} (in workspace {Path.cwd()}).")
    shared = (directory / SHARED_FILE).read_text(encoding="utf-8")
    tracks = {t: (directory / info["file"]).read_text(encoding="utf-8") for t, info in TRACKS.items()}
    base = json.dumps(TRACKS, ensure_ascii=False, sort_keys=True) + shared + "".join(tracks[t] for t in TRACKS)

    def digest(extra: str) -> str:
        return hashlib.sha256((extra + base).encode("utf-8")).hexdigest()

    return {
        "shared": shared,
        "tracks": tracks,
        "triage_hash": digest(RUBRIC + json.dumps(TRIAGE_TOOL, ensure_ascii=False, sort_keys=True)),
        "detail_hash": digest(DETAIL_INSTRUCTIONS + json.dumps(DETAIL_TOOL, ensure_ascii=False, sort_keys=True)),
    }


def triage_system(profiles: dict) -> list[dict]:
    text = f"{RUBRIC}\n\n<profiles>\n## shared.md\n{profiles['shared']}\n"
    for t, info in TRACKS.items():
        text += f"\n{prompts.heading(P['profiles_track_heading'], id=t, name=info['name'])}\n{profiles['tracks'][t]}\n"
    text += "</profiles>"
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def detail_system(profiles: dict, track: str) -> list[dict]:
    info = TRACKS[track]
    text = (
        f"{DETAIL_INSTRUCTIONS}\n\n<profiles>\n## shared.md\n{profiles['shared']}\n"
        f"\n{prompts.heading(P['detail_track_heading'], id=track, name=info['name'], resume=info['resume'])}\n{profiles['tracks'][track]}\n</profiles>"
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# ---------- 模型呼叫（集中在此；換模型或供應商只改這裡與 config） ----------

class Usage:
    FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

    def __init__(self) -> None:
        self.calls = 0
        self.failures: list[str] = []
        self.tokens = dict.fromkeys(self.FIELDS, 0)

    def add(self, usage) -> None:
        self.calls += 1
        for f in self.FIELDS:
            self.tokens[f] += getattr(usage, f, 0) or 0

    def line(self) -> str:
        t = self.tokens
        retries = f"；失敗 {len(self.failures)} 次" if self.failures else ""
        return (f"{self.calls} 次呼叫{retries}；輸入 {t['input_tokens']}、輸出 {t['output_tokens']}、"
                f"快取寫入 {t['cache_creation_input_tokens']}、快取讀取 {t['cache_read_input_tokens']} tokens")


def call_model(client, provider: str, model: str, system: list[dict], user: str,
               tool: dict, validate, usage: Usage, max_tokens: int = 1024,
               force_tool: bool = True) -> dict | None:
    """呼叫模型並回傳 tool 的輸入；失敗（含格式錯誤）重試一次，仍失敗回傳 None。

    force_tool=False 時用 auto，模型靠 prompt 呼叫工具（部分模型不支援強制指定）。
    """
    if provider != "anthropic":
        raise NotImplementedError(f"尚未實作供應商：{provider}")
    last_error = None
    for _ in range(2):
        try:
            response = client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]} if force_tool else {"type": "auto"},
                messages=[{"role": "user", "content": user}],
            )
            usage.add(response.usage)
            for block in response.content:
                if block.type == "tool_use":
                    validate(block.input)
                    return block.input
            raise ValueError("模型沒有回傳 tool 結果")
        except Exception as e:  # 網路、API 與格式錯誤一律重試一次
            last_error = e
            usage.failures.append(str(e)[:80])
            print(f"  ⚠️ 嘗試失敗：{str(e)[:120]}")
    print(f"  ⚠️ 呼叫失敗（已重試一次）：{last_error}")
    return None


def validate_triage(data: dict) -> None:
    scores = data["scores"]
    for t in TRACKS:
        if not isinstance(scores[t], int) or not 0 <= scores[t] <= 5:
            raise ValueError(f"路線 {t} 分數不合法：{scores[t]!r}")
    if (not isinstance(data["custom_family"], bool) or not str(data["reason"]).strip()
            or (VISA_ENABLED and not isinstance(data["visa_blocked"], bool))):
        raise ValueError("custom_family、visa_blocked 或 reason 不合法")


def validate_detail(data: dict) -> None:
    if data["resume"] not in TRACKS:
        raise ValueError(f"resume 不是路線代號：{data['resume']!r}")
    for key in ("emphasize", "gaps"):
        if not isinstance(data[key], list) or len(data[key]) > 3:
            raise ValueError(f"{key} 必須是最多 3 條的清單")


def validate_summary(data: dict) -> None:
    if not isinstance(data["summary"], str) or not data["summary"].strip():
        raise ValueError("summary 不能是空的")


# ---------- 快取表 ----------

def init_score_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS triage_scores (
            job_id TEXT PRIMARY KEY,
            profiles_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            result_json TEXT NOT NULL,
            scored_date TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS detail_reviews (
            job_id TEXT PRIMARY KEY,
            profiles_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            result_json TEXT NOT NULL,
            reviewed_date TEXT NOT NULL
        )
        """
    )
    conn.commit()


def get_cached(conn: sqlite3.Connection, table: str, job_id: str, profiles_hash: str, model: str) -> dict | None:
    row = conn.execute(
        f"SELECT result_json FROM {table} WHERE job_id = ? AND profiles_hash = ? AND model = ?",
        (job_id, profiles_hash, model),
    ).fetchone()
    return json.loads(row[0]) if row else None


def save_cached(conn: sqlite3.Connection, table: str, job_id: str, profiles_hash: str, model: str, result: dict) -> None:
    date_col = "scored_date" if table == "triage_scores" else "reviewed_date"
    conn.execute(
        f"INSERT OR REPLACE INTO {table} (job_id, profiles_hash, model, result_json, {date_col}) VALUES (?, ?, ?, ?, ?)",
        (job_id, profiles_hash, model, json.dumps(result, ensure_ascii=False), date.today().isoformat()),
    )
    conn.commit()


# ---------- Step 5：初評 ----------

VISA_HINT = re.compile(
    r"sponsor|\bvisas?\b|work authori[sz]ation|authori[sz]ed to work|legally (?:able|authori[sz]ed)|"
    r"right to work|immigration|\bOPT\b|\bCPT\b|citizen|permanent resident", re.I)


def visa_excerpts(description: str, max_items: int = 3, before: int = 150, after: int = 250) -> list[str]:
    """從完整描述抽出簽證相關片段（合併重疊的視窗），讓只看前段的初評也讀得到結尾的簽證聲明。"""
    text = re.sub(r"\s+", " ", description or "")
    windows: list[list[int]] = []
    for m in VISA_HINT.finditer(text):
        start, end = max(0, m.start() - before), min(len(text), m.end() + after)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    return [text[a:b].strip() for a, b in windows[:max_items]]


def triage_user_prompt(job: dict, chars: int, visa_skip_locations: list[str] | None = None) -> str:
    fields = {
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "salary": job.get("salaryDetails"),
        "description": (job.get("description") or "")[:chars],
    }
    location = (job.get("location") or "").lower()
    skip_visa = any(loc.lower() in location for loc in (visa_skip_locations or []))  # 例如求職者本國的地點不需要 sponsor
    excerpts = [] if skip_visa or not VISA_ENABLED else visa_excerpts(job.get("description") or "")
    visa_block = ""
    if excerpts:
        visa_block = "\n\n<visa_excerpts>\n" + "\n".join(f"- {e}" for e in excerpts) + "\n</visa_excerpts>"
    return f"<job>\n{json.dumps(fields, ensure_ascii=False, indent=2)}\n</job>{visa_block}\n\n{P['triage_request']}"


def finalize_triage(raw: dict) -> dict:
    """由程式推出最佳路線與 needs_custom（分數相同時依 TRACKS 的順序）。

    visa_blocked 為 true 時，各條路線的分數一律壓到 1（原始分數存在 scores_raw，方便事後檢查或撈回），且不算需客製。
    """
    visa_blocked = VISA_ENABLED and bool(raw.get("visa_blocked"))
    scores = {t: min(raw["scores"][t], 1) for t in TRACKS} if visa_blocked else raw["scores"]
    best_track = max(TRACKS, key=lambda t: (scores[t], -list(TRACKS).index(t)))
    best_score = scores[best_track]
    result = {
        "scores": scores,
        "best_track": best_track,
        "best_score": best_score,
        "custom_family": raw["custom_family"],
        "needs_custom": raw["custom_family"] and best_score < 4 and not visa_blocked,
        "visa_blocked": visa_blocked,
        "reason": raw["reason"].strip(),
    }
    if visa_blocked:
        result["scores_raw"] = raw["scores"]
    return result


def triage_job(conn, client, config, profiles, usage, job: dict) -> dict | None:
    cfg = config["scoring"]
    model = cfg["triage_model"]
    cached = get_cached(conn, "triage_scores", job["jobId"], profiles["triage_hash"], model)
    if cached:
        return cached
    raw = call_model(client, cfg.get("provider", "anthropic"), model, triage_system(profiles),
                     triage_user_prompt(job, cfg["triage_description_chars"], visa_settings(config)["skip_locations"]),
                     TRIAGE_TOOL, validate_triage, usage)
    if raw is None:
        return None
    result = finalize_triage(raw)
    save_cached(conn, "triage_scores", job["jobId"], profiles["triage_hash"], model, result)
    return result


# ---------- Step 6：挑選短名單 ----------

def sort_key(job: dict) -> tuple:
    applicants = effective_applicant_count(job.get("applicantsDetails"))
    return (-job["triage"]["best_score"], applicants if applicants is not None else UNKNOWN_APPLICANTS)


def select_shortlist(scored: list[dict], size: int, min_score: int = 4, custom_min_score: int = 3) -> dict:
    """回傳 {'main': 一般名單, 'custom': 需客製履歷區塊}。

    一般名單：最佳分數 >= min_score 才入選（短名單門檻），最多 size 筆，不用較低分補滿；
    同分應徵人數少者在前。needs_custom 的職缺進獨立區塊，不受一般門檻限制、也不佔一般名額，
    但最佳分數仍須 >= custom_min_score（分數太低表示只有類型沾邊，不值得客製）。
    """
    custom = sorted((j for j in scored if j["triage"]["needs_custom"]
                     and j["triage"]["best_score"] >= custom_min_score), key=sort_key)
    rest = [j for j in scored if not j["triage"]["needs_custom"]]
    main = sorted((j for j in rest if j["triage"]["best_score"] >= min_score), key=sort_key)[:size]
    return {"main": main, "custom": custom}


def detail_job(conn, client, config, profiles, usage, job: dict) -> dict | None:
    cfg = config["scoring"]
    model = cfg["detail_model"]
    cached = get_cached(conn, "detail_reviews", job["jobId"], profiles["detail_hash"], model)
    if cached:
        return cached
    triage = job["triage"]
    full = {k: job.get(k) for k in ("title", "company", "location", "salaryDetails", "description")}
    user = (
        f"<job>\n{json.dumps(full, ensure_ascii=False, indent=2)}\n</job>\n\n"
        f"<triage>\n{json.dumps(triage, ensure_ascii=False, indent=2)}\n</triage>\n\n"
        + (P["detail_custom_note"] + "\n\n"
           if triage["needs_custom"] else "")
        + P["detail_request"]
    )
    result = call_model(client, cfg.get("provider", "anthropic"), model,
                        detail_system(profiles, triage["best_track"]), user,
                        DETAIL_TOOL, validate_detail, usage, max_tokens=1500,
                        force_tool=False)  # Sonnet 5.5 不支援強制指定工具，改靠 prompt 要求呼叫
    if result is not None:
        save_cached(conn, "detail_reviews", job["jobId"], profiles["detail_hash"], model, result)
    return result


def add_details(conn, client, config, profiles, usage, selection: dict) -> None:
    cfg = config["scoring"]
    targets = selection["main"][: cfg["detail_count"]]
    if cfg.get("detail_custom", True):
        targets = targets + selection["custom"]
    for job in targets:
        job["detail"] = detail_job(conn, client, config, profiles, usage, job)


def summarize_job(client, config, usage, job: dict) -> str | None:
    cfg = config["scoring"]
    full = {k: job.get(k) for k in ("title", "company", "location", "description")}
    user = f"<job>\n{json.dumps(full, ensure_ascii=False, indent=2)}\n</job>\n\n{P['summary_request']}"
    system = [{"type": "text", "text": SUMMARY_INSTRUCTIONS}]
    result = call_model(client, cfg.get("provider", "anthropic"), cfg["summary_model"], system, user,
                        SUMMARY_TOOL, validate_summary, usage, max_tokens=500)
    return result["summary"].strip() if result else None


def add_summaries(client, config, usage, selection: dict, day_dir: Path) -> None:
    """每筆入選職缺補上 SUMMARY。既有 .md 已有 SUMMARY 就原樣沿用（含使用者改過的），不重呼叫模型；失敗就不顯示，下次重跑再補。"""
    for job in selection["main"] + selection["custom"]:
        folder = find_job_folder(day_dir, job["jobId"]) if day_dir.exists() else None
        md = folder / f"{folder.name}.md" if folder else None
        job["summary"] = (read_summary(md) if md and md.exists() else "") or summarize_job(client, config, usage, job)


def mark_recommended(conn: sqlite3.Connection, picks: list[dict]) -> None:
    conn.executemany(
        "UPDATE jobs SET recommended_date = ? WHERE job_id = ?",
        [(date.today().isoformat(), j["jobId"]) for j in picks],
    )
    conn.commit()


# ---------- Step 6：輸出 ----------

def _count(path: Path) -> int | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return len(json.load(f))


PRE_STAGES = (("抓取（瘦身後）", "2_slim"), ("去重後", "3_deduped"), ("篩選後", "4_filtered"))


def pipeline_counts(name: str, data_dir: Path = DATA_DIR) -> list[tuple[str, int]]:
    counts = ((label, _count(data_dir / folder / name)) for label, folder in PRE_STAGES)
    return [(label, n) for label, n in counts if n is not None]


def pipeline_stats(name: str, data_dir: Path = DATA_DIR) -> list[str]:
    """用各步驟同檔名的輸出算出當天流程統計；缺檔的步驟略過。"""
    lines = []
    for label, n in pipeline_counts(name, data_dir):
        lines.append(f"{label}：{n}")
    excluded_path = data_dir / "5_excluded" / name
    if excluded_path.exists():
        with excluded_path.open(encoding="utf-8") as f:
            reasons = Counter(r["reason"] for r in json.load(f))
        if reasons:
            lines.append("Step 4 排除：" + "、".join(f"{k} {v}" for k, v in reasons.most_common()))
    return lines


def salary_text(salary) -> str:
    if isinstance(salary, dict):
        return salary.get("text") or "未揭露"
    return salary or "未揭露"


def slug(text: str, limit: int = 60) -> str:
    text = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", text or "")
    return re.sub(r"\s+", "-", text).strip("-")[:limit].strip("-")


def yaml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)  # JSON 字串也是合法的 YAML


def read_frontmatter(path: Path) -> dict:
    """讀 .md 開頭 --- 區塊的簡單 key: value；只用來取回 jobId 與使用者擁有的 status。"""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    props = {}
    for line in text[4:].split("\n---", 1)[0].splitlines():
        key, sep, val = line.partition(":")
        if sep and not line.startswith(" "):
            props[key.strip()] = val.strip().strip('"')
    return props


def read_list_property(path: Path, key: str) -> list[str]:
    """取回列表型 property 的所有值。Obsidian 把它存成列表（types.json 的 multitext），手寫常是單一字串，兩種都讀，統一回傳列表。"""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return []
    lines = text[4:].split("\n---", 1)[0].splitlines()
    for n, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            inline = line.partition(":")[2].strip().strip('"')
            if inline:
                return [inline]
            items = []
            for item in lines[n + 1:]:
                if not item.startswith("  - "):
                    break
                items.append(item[4:].strip().strip('"'))
            return items
    return []


def read_status(path: Path) -> list[str]:
    return read_list_property(path, "status")


def read_interest(path: Path) -> str:
    """取回 interest（checkbox）那行冒號後的原始文字（空白、true 或 false），重寫 .md 時原樣放回。"""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return ""
    for line in text[4:].split("\n---", 1)[0].splitlines():
        if line.startswith("interest:"):
            return line.partition(":")[2].strip()
    return ""


def list_lines(key: str, values: list[str] | str) -> list[str]:
    """列表型 property 一律輸出成列表，與 Obsidian 的 multitext 格式一致，使用者在 Obsidian 改過也不會變格式。空列表只輸出 `key:`。"""
    items = [values] if isinstance(values, str) else values
    return [f"{key}:"] + [f"  - {s}" if re.fullmatch(r"[\w][\w /-]*", s) else f"  - {yaml_value(s)}" for s in items]


def status_lines(status: list[str] | str) -> list[str]:
    return list_lines("status", status)


def read_user_note(path: Path) -> str:
    """取回 user note 那行冒號後的原始文字（含引號），重寫 .md 時原樣放回，不丟使用者寫的內容。"""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return ""
    for line in text[4:].split("\n---", 1)[0].splitlines():
        if line.startswith("user note:"):
            return line.partition(":")[2].strip()
    return ""


def read_summary(path: Path) -> str:
    """取回 .md 裡「## SUMMARY」到下一個 ## 標題之間的文字，重寫 .md 時原樣放回。"""
    m = re.search(r"^## SUMMARY\n\n(.*?)(?=\n## |\Z)", path.read_text(encoding="utf-8"), re.S | re.M)
    return m.group(1).strip() if m else ""


def find_job_folder(day_dir: Path, job_id: str) -> Path | None:
    for md in day_dir.glob("*/*.md"):
        if read_frontmatter(md).get("jobId") == job_id:
            return md.parent
    return None


def job_frontmatter(rank: int | None, job: dict, run_date: str, scraped_date: str, status: list[str] | str,
                    user_note: str = "", interest: str = "") -> list[str]:
    tr = job["triage"]
    detail = job.get("detail")
    tags = [f"track/{tr['best_track']}-{TRACKS[tr['best_track']]['tag']}"]
    if tr["needs_custom"]:
        tags.append("needs-custom")
    posted = job.get("postedDate")
    source = job.get("source", "linkedin")  # 非 LinkedIn 的職缺沒有 jobId、應徵人數，也沒有每日排名
    props = [
        ("title", job["title"]),
        ("company", job["company"]),
        ("source", source),
        ("rank", rank),
        ("date_recommended", run_date),
        ("date_posted", posted),
        ("date_scraped", scraped_date),
        ("best_track", tr["best_track"]),
        ("best_score", tr["best_score"]),
        *((f"score_{t}", tr["scores"][t]) for t in TRACKS),
        ("resume", job.get("resume_file") or (TRACKS[detail["resume"]]["resume"] if detail else None)),  # resume_file：手動指定的客製履歷
        ("location", job.get("location")),
        ("salary", salary_text(job.get("salaryDetails"))),
        ("applicants", (job.get("applicantsDetails") or {}).get("text", "未知") if source == "linkedin" else None),
        ("url", job.get("jobUrl")),
        ("jobId", job["jobId"] if source == "linkedin" else None),
    ]
    lines = ["---"]
    for key, val in props:
        if val is None:
            continue
        # 日期要輸出成未加引號的 YYYY-MM-DD，Obsidian 才會當日期欄位
        lines.append(f"{key}: {val if key.startswith('date_') else yaml_value(val)}")
    lines += status_lines(status)
    lines.append(f"interest: {interest}".rstrip())  # 使用者自己勾選（checkbox：勾＝有興趣，空白＝未表態）；空的也要有這個欄位，Obsidian 才看得到
    lines.append("tags:")
    lines += [f"  - {t}" for t in tags]
    lines.append(f"user note: {user_note}".rstrip())  # 使用者自己填；空的也要有這個欄位，Obsidian 才看得到
    lines.append("---")
    return lines


def format_job_note(rank: int | None, job: dict, run_date: str, scraped_date: str, status: list[str] | str,
                    user_note: str = "", interest: str = "") -> str:
    tr = job["triage"]
    best = TRACKS[tr["best_track"]]
    score_line = "　".join(f"{t} {tr['scores'][t]}" for t in TRACKS)
    lines = job_frontmatter(rank, job, run_date, scraped_date, status, user_note, interest)
    lines += [
        "",
        f"# {job['title']} — {job['company']}",
        "",
        f"- 最佳：{tr['best_track']} {best['name']} {tr['best_score']} 分　各路線：{score_line}",
        f"- 連結：[LinkedIn 職缺]({job.get('jobUrl')})" if job.get("jobUrl") else "- 連結：無",
        f"- 初評理由：{tr['reason']}",
    ]
    for e in job.get("tracker_company_match") or []:
        label = "已投遞" if e["status"] == "applied" else "已評估不投"
        lines.append(f"- 同公司過去：{e['title']}（{label}）")
    if job.get("summary"):
        lines += ["", "## SUMMARY", "", job["summary"]]
    detail = job.get("detail")
    if detail:
        lines += [
            "",
            "## 詳評",
            "",
            f"- 建議履歷：{detail['resume']} {TRACKS[detail['resume']]['name']}（{TRACKS[detail['resume']]['resume']}）",
            "- 強調：" + "；".join(detail["emphasize"]),
            "- 缺口：" + "；".join(detail["gaps"]),
        ]
    lines += ["", "## 原始職缺描述", "", job.get("description") or "（無描述）", ""]
    return "\n".join(lines)


def track_table(scored: list[dict], selection: dict, pre_counts: list[tuple[str, int]]) -> list[str]:
    """各條路線在各階段的筆數。抓取到篩選階段還沒評分，沒有路線可分，只列總筆數。"""
    picks = selection["main"] + selection["custom"]
    stages = [
        ("初評（最佳路線）", lambda j, t: j["triage"]["best_track"] == t, scored),
        ("該路線 >= 3 分", lambda j, t: j["triage"]["scores"][t] >= 3, scored),
        ("該路線 >= 4 分", lambda j, t: j["triage"]["scores"][t] >= 4, scored),
        ("短名單（最佳路線）", lambda j, t: j["triage"]["best_track"] == t, picks),
        ("詳評（最佳路線）", lambda j, t: j["triage"]["best_track"] == t, [j for j in picks if j.get("detail")]),
        ("詳評建議履歷", lambda j, t: j["detail"]["resume"] == t, [j for j in picks if j.get("detail")]),
    ]
    lines = ["| 階段 | " + " | ".join(f"{t} {TRACKS[t]['name']}" for t in TRACKS) + " | 總筆數 |",
             "| --- |" + " --- |" * (len(TRACKS) + 1)]
    for label, n in pre_counts:
        lines.append(f"| {label} |" + " — |" * len(TRACKS) + f" {n} |")
    for label, test, jobs in stages:
        counts = [sum(1 for j in jobs if test(j, t)) for t in TRACKS]
        total = len(jobs) if label.endswith(("最佳路線）", "建議履歷")) else "—"  # 「>= N 分」可跨路線重複計，不加總
        lines.append(f"| {label} | " + " | ".join(map(str, counts)) + f" | {total} |")
    return lines


def read_stage_jobs(name: str, folder: str, data_dir: Path = DATA_DIR) -> list[dict]:
    path = data_dir / folder / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def query_table(name: str, scored: list[dict], selection: dict, query_names: list[str]) -> list[str]:
    """每組搜尋抓到的職缺在各階段的筆數。同一筆被多組抓到時每組都算一次，所以各列加起來會超過總筆數。"""
    slim = read_stage_jobs(name, "2_slim")
    if not any(j.get("matched_queries") for j in slim):
        return []  # 舊資料沒有記錄來源搜尋
    labels = sorted({q for j in slim for q in j["matched_queries"]})
    stages = [
        ("抓取", slim),
        ("去重後", read_stage_jobs(name, "3_deduped")),
        ("篩選後", read_stage_jobs(name, "4_filtered")),
        ("初評 >= 4 分", [j for j in scored if j["triage"]["best_score"] >= 4]),
        ("短名單", selection["main"] + selection["custom"]),
    ]
    def hit(job, label):
        return label in (job.get("matched_queries") or [])
    lines = ["| 搜尋組別 | " + " | ".join(["獨有（只有這組抓到）"] + [s for s, _ in stages]) + " |",
             "| --- |" + " --- |" * (len(stages) + 1)]
    for label in labels:
        i = ord(label) - ord("A")
        title = f"{label} {query_names[i]}" if i < len(query_names) else label
        unique = sum(1 for j in slim if j["matched_queries"] == [label])
        lines.append(f"| {title} | {unique} | " + " | ".join(
            str(sum(1 for j in jobs if hit(j, label))) for _, jobs in stages) + " |")
    lines.append("| 全部（不重複） | — | " + " | ".join(str(len(jobs)) for _, jobs in stages) + " |")
    return lines


def format_summary(run_date: str, stats: list[str], usages: dict[str, Usage], selection: dict,
                   scored: list[dict], pre_counts: list[tuple[str, int]],
                   queries_table: list[str], previous_usage_lines: list[str] | None = None) -> str:
    lines = [f"# 每日職缺短名單統計 {run_date}", ""]
    lines += [f"- 短名單：一般 {len(selection['main'])}、需客製 {len(selection['custom'])}"]
    lines += [f"- {s}" for s in stats]
    blocked = [j for j in scored if j["triage"].get("visa_blocked")]
    if blocked:  # 簽證判定是靜默降分，列出來方便檢查有沒有誤判
        lines += [f"- 初評判定簽證不符（分數壓為 1）：{len(blocked)} 筆"]
        lines += [f"  - {j['company']}｜{j['title']}：{j['triage']['reason']}" for j in blocked[:30]]
    lines += ["", "## 各路線筆數", ""] + track_table(scored, selection, pre_counts) + [""]
    if queries_table:
        lines += ["## 各搜尋組別筆數", ""] + queries_table + [""]
    if previous_usage_lines and not any(u.calls for u in usages.values()):
        lines += previous_usage_lines  # 重跑全靠快取時，保留當天原本真正花掉的用量
    else:
        lines += [f"- {label} API 用量：{u.line()}" for label, u in usages.items()]
    lines.append("")
    return "\n".join(lines)


def write_shortlist(selection: dict, scored: list[dict], run_date: str, scraped_date: str,
                    stats: list[str], usages: dict[str, Usage],
                    pre_counts: list[tuple[str, int]], queries_table: list[str],
                    root: Path = SHORTLIST_DIR, folder_name: str | None = None,
                    summary_name: str = "_summary.md") -> Path:
    """一職缺一資料夾。同 jobId 的資料夾已存在就只重寫 .md，不改名、不動裡面使用者的檔案。"""
    day_dir = root / (folder_name or run_date)
    day_dir.mkdir(parents=True, exist_ok=True)
    for rank, job in enumerate(selection["main"] + selection["custom"], 1):
        folder = find_job_folder(day_dir, job["jobId"])
        status = "backlog"
        user_note = ""
        interest = ""
        if folder:
            md = folder / f"{folder.name}.md"
            if md.exists():
                status = read_status(md) or status
                user_note = read_user_note(md)
                interest = read_interest(md)
        else:
            name = f"{rank:02d}_{slug(job['company'])}_{slug(job['title'])}"
            folder = day_dir / name
            if folder.exists():  # 同公司同職稱的不同職缺
                folder = day_dir / f"{name}_{job['jobId']}"
            folder.mkdir()
        (folder / f"{folder.name}.md").write_text(
            format_job_note(rank, job, run_date, scraped_date, status, user_note, interest), encoding="utf-8")
    summary_path = day_dir / summary_name
    previous = []
    if summary_path.exists():
        previous = [l for l in summary_path.read_text(encoding="utf-8").splitlines() if "API 用量：" in l]
    summary_path.write_text(
        format_summary(run_date, stats, usages, selection, scored, pre_counts, queries_table, previous), encoding="utf-8")
    return day_dir


# ---------- 主流程 ----------

def process_filtered_file(conn, client, config, profiles, path: Path, limit: int | None, detail: bool,
                           stats_path: Path | None = None, folder_suffix: str = "",
                           shortlist_size: int | None = None, summary_name: str = "_summary.md") -> None:
    """folder_suffix：短名單資料夾名稱加後綴（例如 backfill → 2026-10-05-backfill），date_recommended 仍是執行日。"""
    with path.open(encoding="utf-8") as f:
        jobs = json.load(f)
    if limit:
        jobs = jobs[:limit]

    usages = {"初評": Usage(), "詳評": Usage(), "摘要": Usage()}
    scored = []
    for job in jobs:
        triage = triage_job(conn, client, config, profiles, usages["初評"], job)
        if triage is None:
            print(f"  ⚠️ 跳過 {job['jobId']}「{job.get('title')}」（初評失敗，下次重評）")
            continue
        scored.append({**job, "triage": triage})

    selection = select_shortlist(scored, shortlist_size or config["scoring"]["shortlist_size"],
                                 config["scoring"].get("shortlist_min_score", 4),
                                 config["scoring"].get("custom_min_score", 3))
    folder_name = date.today().isoformat() + (f"-{folder_suffix}" if folder_suffix else "")
    if detail:
        add_details(conn, client, config, profiles, usages["詳評"], selection)
    add_summaries(client, config, usages["摘要"], selection, SHORTLIST_DIR / folder_name)

    SCORED_DIR.mkdir(parents=True, exist_ok=True)
    SHORTLIST_DIR.mkdir(parents=True, exist_ok=True)
    with (SCORED_DIR / path.name).open("w", encoding="utf-8") as f:
        json.dump(scored, f, ensure_ascii=False, indent=2)
    run_date = date.today().isoformat()
    m = re.search(r"(\d{4})(\d{2})(\d{2})", path.stem)
    scraped_date = "-".join(m.groups()) if m else run_date
    shortlist_path = write_shortlist(
        selection, scored, run_date, scraped_date, pipeline_stats(path.name), usages,
        pipeline_counts(path.name),
        query_table(path.name, scored, selection, config["apify"].get("query_names", [])),
        folder_name=folder_name, summary_name=summary_name)
    mark_recommended(conn, selection["main"] + selection["custom"])

    if stats_path:
        picks = selection["main"] + selection["custom"]
        stats = {
            "main": len(selection["main"]),
            "custom": len(selection["custom"]),
            "by_track": dict(Counter(j["triage"]["best_track"] for j in picks)),
            "tokens": {f: sum(u.tokens[f] for u in usages.values()) for f in Usage.FIELDS},
            "failures": sum(len(u.failures) for u in usages.values()),
        }
        stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"讀取：{path}")
    print(f"輸入筆數：{len(jobs)}，初評成功：{len(scored)}")
    print(f"短名單：一般 {len(selection['main'])}、需客製 {len(selection['custom'])}")
    for label, u in usages.items():
        print(f"{label}用量：{u.line()}")
    print(f"已存檔至：{SCORED_DIR / path.name}、{shortlist_path}")
    print("-" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH, help="資料庫路徑（測試時用 test.db）")
    parser.add_argument("--limit", type=int, help="每個檔案最多評分幾筆（測試用）")
    parser.add_argument("--no-detail", action="store_true", help="不做詳評（省 token）")
    parser.add_argument("--stats-json", type=Path, help="把短名單統計（筆數、各 track、token 用量）寫成 JSON，每日執行用")
    parser.add_argument("--only", help="只處理指定檔名（每日執行用，例如 raw_20261004.json）")
    parser.add_argument("--folder-suffix", default="", help="短名單資料夾後綴，例如 backfill（回補用）")
    parser.add_argument("--shortlist-size", type=int, help="一般名單上限（覆蓋 config，回補用）")
    parser.add_argument("--summary-name", default="_summary.md", help="統計檔檔名（回補每批用不同檔名，避免覆蓋）")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("找不到 ANTHROPIC_API_KEY，請在 .env 設定或匯出環境變數。")
    try:
        profiles = load_profiles()
    except FileNotFoundError as e:
        sys.exit(f"找不到 profile 檔：{e.filename}，請先建立 profiles/。")

    config = load_config()
    client = anthropic.Anthropic()
    conn = init_db(args.db)
    init_score_tables(conn)

    files = sorted(FILTERED_DIR.glob("*.json"))
    if args.only:
        files = [f for f in files if f.name == args.only]
    if not files:
        print(f"{FILTERED_DIR}/ 底下沒有找到任何 .json 檔案，請先執行 scripts/execution-python/filter.py。")
    for filtered_path in files:
        process_filtered_file(conn, client, config, profiles, filtered_path, args.limit, not args.no_detail,
                              args.stats_json, args.folder_suffix, args.shortlist_size, args.summary_name)
    conn.close()
