"""run_daily.py --dry-run: a free check that says what is wrong and what a run would do, and writes nothing."""

import json
import os
from datetime import datetime, timedelta

import pytest
import yaml

DAY = "2026-10-09"


def dry(ws, *args, today=DAY, env=None):
    return ws.run("run_daily.py", "--dry-run", *args, today=today, env=env)


def report(ws, *args, **kw):
    proc = dry(ws, "--json", *args, **kw)
    return proc.returncode, json.loads(proc.stdout)


def statuses(rep):
    return {(c["status"], c["name"]) for c in rep["checks"]}


def snapshot(path):
    return sorted((str(p.relative_to(path)), p.stat().st_mtime_ns, p.stat().st_size) for p in path.rglob("*") if p.is_file())


# ---- verdict ----
def test_a_fresh_workspace_with_keys_is_ready_with_warnings_about_the_template(ws):
    proc = dry(ws)
    assert proc.returncode == 0, proc.all_output
    assert "Ready, with warnings." in proc.stdout
    assert "identical to the template" in proc.stdout


def test_missing_keys_make_it_not_ready(ws):
    proc = dry(ws, env={"APIFY_TOKEN": "", "ANTHROPIC_API_KEY": ""})
    assert proc.returncode == 1 and "[ERROR] env APIFY_TOKEN: not set" in proc.stdout and "Not ready" in proc.stdout


def test_key_values_are_never_printed(ws):
    proc = dry(ws, "--json")
    assert "test-apify-token" not in proc.all_output and "test-anthropic-key" not in proc.all_output


def test_edited_profiles_are_no_longer_flagged(ws):
    for name in ("shared.md", "a_marketing.md", "b_pm.md"):
        ws.write(f"profiles/{name}", f"my own {name}\n")
    proc = dry(ws)
    assert "identical to the template" not in proc.stdout and "Ready." in proc.stdout


def test_unconfirmed_markers_are_counted_but_not_in_comments(ws):
    for name in ("shared.md", "a_marketing.md", "b_pm.md"):
        ws.write(f"profiles/{name}", f"x {name}\n")
    ws.write("profiles/shared.md", "<!-- mark guesses with (unconfirmed) -->\nLevel: junior (unconfirmed)\nSkill (unconfirmed)\n")
    rep = report(ws)[1]
    (warn,) = [c for c in rep["checks"] if "unconfirmed" in c["detail"]]
    assert warn["name"] == "profile shared.md" and "2 (unconfirmed)" in warn["detail"]


def test_very_long_profiles_get_a_cost_warning(ws):
    ws.write("profiles/shared.md", "x" * 21000)
    assert ("warn", "profiles size") in statuses(report(ws)[1])


# ---- plan ----
def test_the_plan_counts_searches_and_jobs(ws):
    code, rep = report(ws)
    assert code == 0 and rep["ok"] is True
    assert rep["plan"]["searches"] == 2 and rep["plan"]["max_jobs_fetched"] == 100 and rep["plan"]["tracks"] == ["A", "B"]
    ws.update_config({"apify": {"locations": ["Austin, TX", "Remote"], "max_jobs_by_query": {"B": 10}}, "daily": {"max_total_jobs": 120}})
    plan = report(ws)[1]["plan"]
    assert plan["searches"] == 4 and plan["max_jobs_fetched"] == 120  # 2 x (50 + 10) = 120, also the daily cap
    assert report(ws, "--max-jobs", "5")[1]["plan"]["max_jobs_fetched"] == 20


def test_the_text_report_describes_a_run(ws):
    out = dry(ws).stdout
    assert "make 2 Apify searches and fetch at most 100 jobs" in out and "Today (2026-10-09): start fresh" in out


def test_help_lists_the_flags(ws):
    out = ws.run("run_daily.py", "--help").stdout
    assert "--dry-run" in out and "--json" in out


# ---- it changes nothing ----
def test_a_dry_run_writes_nothing_and_creates_no_database(ws):
    before = snapshot(ws.path)
    dry(ws)
    dry(ws, "--json")
    assert snapshot(ws.path) == before and not ws.exists("jobs.db")


def test_a_dry_run_leaves_an_existing_database_untouched(ws):
    ws.daily()
    before = snapshot(ws.path)
    dry(ws)
    after = snapshot(ws.path)
    assert [x for x in after if x[0] == "jobs.db"] == [x for x in before if x[0] == "jobs.db"]


# ---- what a run would do today ----
def test_it_reports_a_finished_day_a_failed_day_and_earlier_failed_days(ws):
    ws.daily()
    assert report(ws)[1]["state"]["would"].startswith("skip (today already succeeded")
    ws.daily("--force", env={"JOBSEARCH_TEST_ANTHROPIC_INIT_FAILS": "99"})  # run it again; this time scoring is down
    assert report(ws)[1]["state"]["would"] == "resume after: fetch, dedup, filter"


def test_resume_and_catch_up_are_reported(ws):
    ws.daily(today="2026-10-08", env={"JOBSEARCH_TEST_ANTHROPIC_INIT_FAILS": "99"})
    ws.daily(today="2026-10-01", env={"JOBSEARCH_TEST_ANTHROPIC_INIT_FAILS": "99"})  # too old to be caught up
    state = report(ws)[1]["state"]
    assert state["would"] == "start fresh"
    assert [c["day"] for c in state["catch_up"]] == ["2026-10-08"]
    assert state["catch_up"][0]["steps_done"] == ["fetch", "dedup", "filter"]
    ws.daily(today=DAY, env={"JOBSEARCH_TEST_ANTHROPIC_INIT_FAILS": "99"})  # scoring is down for today and for the catch-up
    state = report(ws)[1]["state"]
    assert state["would"] == "resume after: fetch, dedup, filter" and state["steps_done"] == ["fetch", "dedup", "filter"]
    assert "first finish 2026-10-08" in dry(ws).stdout


def test_a_run_in_progress_is_reported_as_skipped(ws):
    ws.daily()
    db = ws.db()
    db.execute("DELETE FROM daily_runs")
    db.execute("INSERT INTO daily_runs (run_date, status, started_at) VALUES (?, 'running', ?)", (DAY, datetime.now().isoformat(timespec="seconds")))
    db.commit()
    assert "still marked running" in report(ws)[1]["state"]["would"]
    db.execute("UPDATE daily_runs SET started_at = ?", ((datetime.now() - timedelta(hours=5)).isoformat(timespec="seconds"),))
    db.commit()
    assert report(ws)[1]["state"]["would"] == "start fresh"


# ---- mistakes it must catch ----
def edit_config(ws, mutate):
    config = ws.read_config()
    mutate(config)
    (ws.path / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize("name,mutate,expected", [
    ("fetch_details off", lambda c: c["apify"].update(fetch_details=False), ("error", "apify.fetch_details")),
    ("empty whitelist", lambda c: c.update(location_whitelist=[]), ("error", "location_whitelist")),
    ("no queries", lambda c: c["apify"].update(queries=[]), ("error", "apify.queries")),
    ("no locations", lambda c: c["apify"].update(locations=[]), ("error", "apify.locations")),
    ("query names mismatch", lambda c: c["apify"].update(query_names=["one"]), ("warn", "apify.query_names")),
    ("tracker file missing", lambda c: c.update(tracker_path="nope.txt"), ("warn", "tracker_path")),
    ("visa on without phrases", lambda c: c.update(visa={"enabled": True, "exclude_patterns": []}), ("warn", "visa")),
    ("duplicate track id", lambda c: c["tracks"][1].update(id="A"), ("error", "config values")),
    ("old visa key", lambda c: c.update(visa_skip_locations=[]), ("error", "config values")),
])
def test_config_mistakes_are_reported_and_set_the_exit_code(ws, name, mutate, expected):
    edit_config(ws, mutate)
    code, rep = report(ws)
    assert expected in statuses(rep), name
    assert code == (1 if expected[0] == "error" else 0)


def test_a_missing_profile_file_is_reported(ws):
    (ws.path / "profiles" / "b_pm.md").unlink()
    code, rep = report(ws)
    assert code == 1 and any(c["name"] == "profiles" and "Missing profile for track B" in c["detail"] for c in rep["checks"])


def test_missing_and_broken_config_files(ws):
    (ws.path / "config.yaml").write_text("tracks: [unclosed\n", encoding="utf-8")
    assert any("not valid YAML" in c["detail"] for c in report(ws)[1]["checks"])
    (ws.path / "config.yaml").unlink()
    code, rep = report(ws)
    assert code == 1 and "config.yaml not found" in rep["checks"][0]["detail"] and rep["state"] == {}


def test_a_bad_override_file_is_reported_not_raised(ws):
    ws.write("prompts.yaml", "rubrik: x\n")
    code, rep = report(ws)
    assert code == 1 and any("unknown names: rubrik" in c["detail"] for c in rep["checks"])
