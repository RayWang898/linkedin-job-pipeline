"""The daily run end to end: one normal day, and everything that can go wrong with it. Apify and Anthropic are fakes (tests/fakes)."""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

DAY = "2026-10-09"
FILE = "raw_20261009.json"
STEPS = ["fetch", "dedup", "filter", "score"]
SCORE_DOWN = {"JOBSEARCH_TEST_ANTHROPIC_INIT_FAILS": "99"}  # every scoring process dies at start: the "API is down" day


def apify_calls(ws):
    return ws.calls("apify")


# ---- a normal day ----
def test_a_normal_day(ws):
    proc = ws.daily()
    assert proc.returncode == 0, proc.all_output
    assert ws.runs() == {DAY: ("success", STEPS)}
    row = ws.db().execute("SELECT raw_count, main_count, custom_count, by_track FROM daily_runs").fetchone()
    assert row[0] == 28 and row[1] + row[2] == len(ws.shortlist(DAY)) == 15  # main + needs-custom = the folders written
    assert json.loads(row[3]) == {"A": 12, "B": 3}
    for stage in ("1_raw", "2_slim", "3_deduped", "4_filtered", "5_excluded", "6_scored"):
        assert ws.exists(f"data/{stage}/{FILE}"), stage
    assert ws.exists(f"logs/{DAY}.log") and ws.exists("feedback/calibrate_20261009.md")


def test_the_stages_drop_the_expected_jobs(ws):
    ws.use_tracker()
    ws.daily()
    ids = lambda stage: sorted(j["jobId"] if "jobId" in j else j["job"]["jobId"] for j in json.loads(ws.read(f"data/{stage}/{FILE}")))
    assert len(ids("2_slim")) == 28 and "1026" not in ids("3_deduped") and len(ids("3_deduped")) == 27
    excluded = {(e["reason"], e["job"]["jobId"]) for e in json.loads(ws.read(f"data/5_excluded/{FILE}"))}
    assert ("already_in_tracker", "1028") in excluded and ("location", "1016") in excluded and ("min_years", "1020") in excluded
    assert "1028" not in ids("4_filtered") and "1029" in ids("4_filtered")


def test_a_job_found_by_two_queries_is_merged_and_remembers_both(ws):
    ws.daily()
    slim = {j["jobId"]: j for j in json.loads(ws.read(f"data/2_slim/{FILE}"))}
    assert slim["1001"]["matched_queries"] == ["A", "B"]


def test_a_day_that_succeeded_is_skipped(ws):
    ws.daily()
    before = len(apify_calls(ws))
    proc = ws.daily()
    assert proc.returncode == 0 and "already succeeded" in proc.all_output
    assert len(apify_calls(ws)) == before


def test_no_new_jobs_is_a_success_with_nothing_to_do(ws):
    ws.use_jobs([])
    proc = ws.daily()
    assert proc.returncode == 0 and "no new data today" in proc.all_output
    assert ws.runs() == {DAY: ("success", [])}
    assert ws.calls("anthropic") == []


def test_missing_keys_fail_before_anything_is_fetched(ws):
    proc = ws.daily(env={"APIFY_TOKEN": ""})
    assert proc.returncode == 1 and "APIFY_TOKEN not found" in proc.all_output
    assert ws.runs()[DAY][0] == "failed" and apify_calls(ws) == []


def test_the_fetch_caps(ws):
    ws.update_config({"apify": {"max_jobs_per_search": 7, "max_jobs_by_query": {"B": 4}}})
    ws.daily()
    assert [c["run_input"]["maxJobs"] for c in apify_calls(ws)] == [7, 4]  # query B has its own cap
    assert ws.db().execute("SELECT raw_count FROM daily_runs").fetchone()[0] == 7  # both searches return the same first jobs, merged by id
    ws.daily("--force", "--max-jobs", "3")
    assert [c["run_input"]["maxJobs"] for c in apify_calls(ws)[-2:]] == [3, 3]


def test_two_locations_make_twice_the_searches(ws):
    ws.update_config({"apify": {"locations": ["Austin, TX", "Remote"]}})
    ws.daily()
    assert len(apify_calls(ws)) == 4
    assert all(len(c["run_input"]["locations"]) == 1 for c in apify_calls(ws))  # one location per search, each with its own cap


def test_total_job_cap_trims_the_fetch(ws):
    ws.update_config({"daily": {"max_total_jobs": 5}})
    proc = ws.daily()
    assert "over the overall cap of 5" in proc.all_output
    assert ws.db().execute("SELECT raw_count FROM daily_runs").fetchone()[0] == 5


# ---- a transient failure inside the day ----
def test_scoring_that_fails_twice_and_then_works_still_finishes_the_day(ws):
    proc = ws.daily(env={"JOBSEARCH_TEST_ANTHROPIC_INIT_FAILS": "2"})
    assert proc.returncode == 0 and proc.all_output.count("attempt") >= 2
    assert ws.runs()[DAY] == ("success", STEPS)


# ---- a day that fails, then is rerun ----
def test_a_failed_day_is_resumed_without_fetching_again(ws, make_ws):
    first = ws.daily(env=SCORE_DOWN)
    assert first.returncode == 1 and "Steps 5-6 scoring and output" in first.all_output
    assert ws.runs()[DAY] == ("failed", ["fetch", "dedup", "filter"])
    assert ws.db().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 28  # dedup already marked all 28 as seen (one as a duplicate)
    searches = len(apify_calls(ws))

    second = ws.daily()
    assert second.returncode == 0, second.all_output
    assert "Resuming" in second.all_output and "already done" in second.all_output
    assert len(apify_calls(ws)) == searches  # no new Apify call
    assert ws.runs()[DAY] == ("success", STEPS)

    control = make_ws()
    control.daily()
    assert ws.shortlist(DAY) == control.shortlist(DAY) and ws.shortlist(DAY)  # nothing was lost


def test_a_plain_rerun_would_have_kept_nothing_so_the_resume_matters(ws):
    ws.daily(env=SCORE_DOWN)
    ws.daily()
    kept = json.loads(ws.read(f"data/4_filtered/{FILE}"))
    assert len(kept) == 17  # the filtered file was not overwritten with an empty one


def test_a_day_that_failed_before_fetching_finished_starts_over(ws):
    first = ws.daily(env={"JOBSEARCH_TEST_APIFY_FAILS": "99"})
    assert first.returncode == 1 and "all queries failed" in first.all_output
    assert ws.runs()[DAY] == ("failed", [])
    assert ws.daily().returncode == 0
    assert ws.runs()[DAY] == ("success", STEPS)


def test_a_recorded_step_whose_file_is_gone_stops_with_a_clear_message(ws):
    ws.daily(env=SCORE_DOWN)
    (ws.path / "data" / "3_deduped" / FILE).unlink()
    searches = len(apify_calls(ws))
    proc = ws.daily()
    assert proc.returncode == 1
    assert "step dedup is recorded as done but data/3_deduped/raw_20261009.json is missing" in proc.all_output
    assert len(apify_calls(ws)) == searches


def test_a_stale_running_row_is_taken_over_and_a_fresh_one_is_respected(ws):
    ws.daily()  # creates the database and the table
    db = ws.db()
    db.execute("DELETE FROM daily_runs")
    fresh = datetime.now().isoformat(timespec="seconds")
    db.execute("INSERT INTO daily_runs (run_date, status, started_at) VALUES (?, 'running', ?)", (DAY, fresh))
    db.commit()
    skipped = ws.daily()
    assert "already has a run in progress" in skipped.all_output
    old = (datetime.now() - timedelta(hours=5)).isoformat(timespec="seconds")
    db.execute("UPDATE daily_runs SET started_at = ? WHERE run_date = ?", (old, DAY))
    db.commit()
    db.close()
    taken = ws.daily("--force")
    assert taken.returncode == 0


# ---- earlier failed days are finished first ----
def fail_a_day(ws, day):
    proc = ws.daily(today=day, env=SCORE_DOWN)
    assert proc.returncode == 1
    assert ws.runs()[day] == ("failed", ["fetch", "dedup", "filter"])


def test_an_earlier_failed_day_is_caught_up_into_its_own_folder(ws):
    fail_a_day(ws, "2026-10-08")
    ws.use_jobs([])  # nothing new to fetch today
    proc = ws.daily(today=DAY)
    assert proc.returncode == 0 and "Catching up 2026-10-08" in proc.all_output
    assert ws.runs()["2026-10-08"] == ("success", STEPS) and ws.runs()[DAY] == ("success", [])
    assert ws.shortlist("2026-10-08") and ws.shortlist(DAY) == []
    folder = ws.shortlist("2026-10-08")[0]
    assert "date_recommended: 2026-10-08" in ws.note("2026-10-08", folder)
    marked = ws.db().execute("SELECT DISTINCT recommended_date FROM jobs WHERE recommended_date IS NOT NULL").fetchall()
    assert marked == [("2026-10-08",)]


def test_catching_up_reaches_back_three_days_and_no_further(ws):
    fail_a_day(ws, "2026-10-06")  # exactly three days before
    ws.daily(today="2026-10-01", env=SCORE_DOWN)  # a failure eight days before
    ws.use_jobs([])
    proc = ws.daily(today=DAY)
    assert ws.runs()["2026-10-06"][0] == "success"
    assert ws.runs()["2026-10-01"][0] == "failed"
    assert "Catching up 2026-10-01" not in proc.all_output


def test_catch_up_can_be_switched_off(ws):
    fail_a_day(ws, "2026-10-08")
    ws.use_jobs([])
    ws.daily("--no-catch-up", today=DAY)
    assert ws.runs()["2026-10-08"][0] == "failed"


def test_a_catch_up_that_fails_does_not_block_today(ws):
    fail_a_day(ws, "2026-10-08")
    ws.use_jobs([])
    proc = ws.daily(today=DAY, env=SCORE_DOWN)
    assert proc.returncode == 0 and "Catching up 2026-10-08 failed" in proc.all_output
    assert ws.runs()["2026-10-08"][0] == "failed" and ws.runs()[DAY][0] == "success"


def test_a_day_that_never_got_past_the_fetch_is_not_caught_up(ws):
    ws.daily(today="2026-10-08", env={"JOBSEARCH_TEST_APIFY_FAILS": "99"})
    ws.use_jobs([])
    proc = ws.daily(today=DAY)
    assert "Catching up" not in proc.all_output and ws.runs()["2026-10-08"] == ("failed", [])


# ---- --force ----
def test_force_moves_the_days_files_aside_and_runs_again(ws):
    ws.daily()
    shortlist_before = ws.shortlist(DAY)
    ws.use_jobs([])
    proc = ws.daily("--force")
    assert proc.returncode == 0 and "--force: moved the day's earlier files" in proc.all_output
    replaced = list((ws.path / "data" / "_replaced").iterdir())
    assert len(replaced) == 1 and replaced[0].name.startswith(DAY)
    kept = {p.name for p in replaced[0].iterdir()}
    assert {"1_raw", "2_slim", "3_deduped", "4_filtered", "5_excluded", "6_scored", "_summary.md"} <= kept
    assert ws.shortlist(DAY) == shortlist_before  # the notes themselves are not touched
    assert not ws.exists(f"data/4_filtered/{FILE}")


# ---- upgrades, notifications, housekeeping ----
def test_a_database_from_before_steps_were_recorded_is_upgraded(ws):
    db = ws.db()
    db.execute("CREATE TABLE daily_runs (run_date TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, "
               "raw_count INTEGER, failed_queries TEXT, main_count INTEGER, custom_count INTEGER, by_track TEXT, tokens TEXT, error TEXT)")
    db.execute("INSERT INTO daily_runs (run_date, status, started_at, raw_count) VALUES ('2026-10-01', 'success', '2026-10-01T08:00:00', 200)")
    db.commit()
    db.close()
    assert ws.daily().returncode == 0
    assert ws.runs()["2026-10-01"] == ("success", []) and ws.runs()[DAY] == ("success", STEPS)


def test_the_phone_notification_has_counts_only(ws):
    proc = ws.run("run_daily.py", today=DAY, env={"NTFY_TOPIC": "my-topic"})
    assert proc.returncode == 0
    (post,) = ws.calls("ntfy")
    assert post["url"] == "https://ntfy.sh/my-topic" and post["headers"]["Title"] == "Job Search"
    assert post["message"].startswith("Shortlist 15 | A 12 / B 3 | needs custom 1 | tokens input ")
    assert "Acme" not in post["message"]  # no company names or job titles


def test_a_failure_notification_is_high_priority(ws):
    ws.run("run_daily.py", today=DAY, env={**SCORE_DOWN, "NTFY_TOPIC": "my-topic"})
    (post,) = ws.calls("ntfy")
    assert post["headers"]["Title"] == "Job Search FAILED" and post["headers"]["Priority"] == "high"
    assert post["message"].startswith("Daily run failed: Steps 5-6 scoring and output")


def test_a_caught_up_day_gets_its_own_notification(ws):
    fail_a_day(ws, "2026-10-08")
    ws.use_jobs([])
    ws.run("run_daily.py", today=DAY, env={"NTFY_TOPIC": "t"})
    messages = [c["message"] for c in ws.calls("ntfy")]
    assert any(m.startswith("Caught up 2026-10-08: Shortlist") for m in messages)


def test_no_notify_sends_nothing(ws):
    ws.daily(env={"NTFY_TOPIC": "my-topic"})
    assert ws.calls("ntfy") == []


def test_old_logs_are_deleted_and_recent_ones_kept(ws):
    logs = ws.path / "logs"
    logs.mkdir()
    old, recent = logs / "2026-01-01.log", logs / "2026-10-01.log"
    old.write_text("x"), recent.write_text("x")
    os.utime(old, (time.time() - 90 * 86400,) * 2)
    ws.daily()
    assert not old.exists() and recent.exists()
