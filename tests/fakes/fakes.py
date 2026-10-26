"""Stand-ins for everything that would use the network. Deterministic, so tests can assert on exact results.

Environment variables (set by tests/conftest.py):
  JOBSEARCH_TEST_LOG     JSONL file; every fake call appends one line
  JOBSEARCH_TEST_JOBS    JSON file of raw jobs the fake Apify actor returns
  JOBSEARCH_TEST_STATE   folder for small counter files (failure injection)
  JOBSEARCH_TEST_TODAY   YYYY-MM-DD; date.today() returns this
  JOBSEARCH_TEST_ANTHROPIC_INIT_FAILS   the first N Anthropic() constructions raise (a process that dies at start)
  JOBSEARCH_TEST_APIFY_FAILS            the first N actor calls raise
  JOBSEARCH_TEST_MODEL_ERRORS           every model call raises (the retry and skip path)
"""

import datetime
import json
import os
import re
import time
from types import SimpleNamespace


# ---------- helpers ----------

def log(entry: dict) -> None:
    path = os.environ.get("JOBSEARCH_TEST_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def bump(name: str) -> int:
    """Increment and return a persistent counter (shared by the processes of one test)."""
    folder = os.environ.get("JOBSEARCH_TEST_STATE") or os.getcwd()
    path = os.path.join(folder, f".fake_counter_{name}")
    n = int(open(path).read()) + 1 if os.path.exists(path) else 1
    with open(path, "w") as f:
        f.write(str(n))
    return n


def install_basics() -> None:
    time.sleep = lambda seconds: None
    today = os.environ.get("JOBSEARCH_TEST_TODAY")
    if today:
        class FakeDate(datetime.date):
            @classmethod
            def today(cls):
                return cls.fromisoformat(today)

        datetime.date = FakeDate


# ---------- Anthropic ----------

def _job_in(user: str) -> dict:
    m = re.search(r"<job>\n(.*?)\n</job>", user, re.S)
    return json.loads(m.group(1)) if m else {}


def answer_for(tool: dict, user: str) -> dict:
    """The fake model's rules. Titles decide the scores, so a test controls the outcome through its fixture jobs."""
    name = tool["name"]
    job = _job_in(user)
    title = (job.get("title") or "")
    low = title.lower()
    if name == "submit_summary":
        return {"summary": f"Fake summary of {title}."}
    if name == "submit_detail":
        m = re.search(r"<triage>\n(.*?)\n</triage>", user, re.S)
        best = json.loads(m.group(1))["best_track"]
        return {"resume": best, "emphasize": ["Fake emphasis"], "gaps": ["Fake gap"]}
    props = tool["input_schema"]["properties"]
    tracks = list(props["scores"]["properties"])
    first, second, last = tracks[0], tracks[min(1, len(tracks) - 1)], tracks[-1]
    scores = dict.fromkeys(tracks, 1)
    custom = False
    if "coordinator" in low:
        scores[first] = 4
    if "associate" in low:
        scores[first] = 5
    if "analyst" in low:
        scores[second] = 4
    if "specialist" in low:
        scores[last] = 4
    if "producer" in low or "immersive" in low:
        scores[first], custom = 3, True
    if "intern" in low:
        scores = dict.fromkeys(tracks, 0)
    answer = {"scores": scores, "custom_family": custom, "reason": f"Fake reason: {title}"}
    if "visa_blocked" in props:
        answer["visa_blocked"] = "not able to support opt" in user.lower()
    for flag in ("level_too_high", "not_interested", "suspicious", "interest_match"):  # triage flags: a marker in the description sets one
        if flag in props:
            answer[flag] = f"FLAG:{flag}" in (job.get("description") or "")
    return answer


class _Messages:
    def create(self, model, max_tokens, system, tools, tool_choice, messages):
        if os.environ.get("JOBSEARCH_TEST_MODEL_ERRORS"):
            raise RuntimeError("fake model error")
        tool, user = tools[0], messages[0]["content"]
        system_text = "".join(block["text"] for block in system)
        log({"kind": "anthropic", "model": model, "tool": tool["name"], "system": system_text, "user": user,
             "schema": tool["input_schema"], "tool_choice": tool_choice})
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_creation_input_tokens=10, cache_read_input_tokens=50)
        block = SimpleNamespace(type="tool_use", input=answer_for(tool, user))
        return SimpleNamespace(content=[block], usage=usage)


class FakeAnthropic:
    def __init__(self, *args, **kwargs):
        fails = int(os.environ.get("JOBSEARCH_TEST_ANTHROPIC_INIT_FAILS", "0"))
        if fails and bump("anthropic_init") <= fails:
            raise RuntimeError("fake Anthropic client failed to start")
        self.messages = _Messages()


def patch_anthropic(module) -> None:
    module.Anthropic = FakeAnthropic


# ---------- Apify ----------

class _Actor:
    def __init__(self, name):
        self.name = name

    def call(self, run_input, **options):  # the real client also takes run_timeout, memory and so on
        fails = int(os.environ.get("JOBSEARCH_TEST_APIFY_FAILS", "0"))
        n = bump("apify_calls")
        log({"kind": "apify", "actor": self.name, "run_input": run_input})
        if n <= fails:
            raise RuntimeError("fake Apify error")
        jobs = json.load(open(os.environ["JOBSEARCH_TEST_JOBS"], encoding="utf-8"))
        _Dataset.stored[str(n)] = jobs[: run_input.get("maxJobs") or len(jobs)]
        return SimpleNamespace(status="SUCCEEDED", default_dataset_id=str(n))


class _Dataset:
    stored: dict = {}

    def __init__(self, dataset_id):
        self.dataset_id = dataset_id

    def iterate_items(self):
        return iter(self.stored[self.dataset_id])


class FakeApifyClient:
    def __init__(self, token=None, *args, **kwargs):
        self.token = token

    def actor(self, name):
        return _Actor(name)

    def dataset(self, dataset_id):
        return _Dataset(dataset_id)


def patch_apify(module) -> None:
    module.ApifyClient = FakeApifyClient


# ---------- requests (ntfy and Nominatim) ----------

class _Response:
    def __init__(self, payload=None):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def patch_requests(module) -> None:
    def fake_get(url, **kwargs):
        log({"kind": "requests.get", "url": url, "params": kwargs.get("params")})
        return _Response([{"lat": "30.27", "lon": "-97.74"}])  # every address geocodes to one point

    def fake_post(url, data=None, headers=None, **kwargs):
        log({"kind": "ntfy", "url": url, "message": (data or b"").decode("utf-8"), "headers": dict(headers or {})})
        return _Response()

    module.get, module.post = fake_get, fake_post
