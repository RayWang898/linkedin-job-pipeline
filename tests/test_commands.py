"""The side commands: calibrate, add_to_shortlist, replay_triage and backfill."""

import re

DAY = "2026-10-09"
DAY_FILE = "raw_20261009.json"


def test_calibrate_lists_the_near_misses(ws):
    ws.daily()
    proc = ws.run("calibrate.py", "20261009")
    assert proc.returncode == 0, proc.all_output
    out = proc.stdout
    assert "calibration list (cap 75, margin +50)" in out
    assert "## Just over the applicant cap (1)" in out and "(Austin, TX) - 80 applicants" in out and "[1017]" in out
    assert "## Excluded by title (2" in out and "[1013]" in out and "[1014]" in out
    assert "## Excluded by location (1)" in out and "[1016]" in out
    assert "## Excluded by required years (2)" in out and "[1018]" in out and "[1020]" in out
    assert "## Passed triage but missed the shortlist (2" in out and "[1007]" in out and "[1008]" in out


def test_calibrate_margin_widens_the_near_miss_window(ws):
    ws.daily()
    assert "Just over the applicant cap (1)" in ws.run("calibrate.py", "20261009", "--margin", "50").stdout
    assert "Just over the applicant cap (2)" in ws.run("calibrate.py", "20261009", "--margin", "200").stdout  # now 1011 (over 200) counts too


# ---- add_to_shortlist ----
def test_adding_a_linkedin_job_continues_the_ranking(ws):
    ws.daily()
    existing = len(ws.shortlist(DAY))
    proc = ws.run("add_to_shortlist.py", "1013", "--date", DAY, "--status", "applied")
    assert proc.returncode == 0, proc.all_output
    folder = f"{existing + 1:02d}_Nu-Co_Senior-Marketing-Manager"
    assert folder in ws.shortlist(DAY)
    note = ws.note(DAY, folder)
    assert "status:\n  - applied" in note and f"rank: {existing + 1}" in note and 'jobId: "1013"' in note
    assert ws.db().execute("SELECT recommended_date FROM jobs WHERE job_id = '1013'").fetchone()[0] == DAY


def test_adding_the_same_job_twice_is_refused(ws):
    ws.daily()
    ws.run("add_to_shortlist.py", "1013", "--date", DAY)
    again = ws.run("add_to_shortlist.py", "1013", "--date", DAY)
    assert again.returncode != 0 and "already" in again.all_output


def test_adding_a_job_that_was_never_fetched_is_refused(ws):
    ws.daily()
    proc = ws.run("add_to_shortlist.py", "999999", "--date", DAY)
    assert proc.returncode != 0 and "not found" in proc.all_output


def test_adding_a_non_linkedin_job_from_a_text_file(ws):
    ws.daily()
    ws.write("jd.txt", "Write documentation for a developer tool.")
    proc = ws.run("add_to_shortlist.py", "--file", "jd.txt", "--title", "Technical Writer", "--company", "Example Co", "--date", DAY)
    assert proc.returncode == 0, proc.all_output
    note = ws.note(DAY, "Example-Co_Technical-Writer")  # no rank prefix
    assert 'source: "other"' in note and "jobId:" not in note and "Write documentation for a developer tool." in note


def test_a_non_linkedin_job_needs_a_title_and_company(ws):
    ws.write("jd.txt", "text")
    proc = ws.run("add_to_shortlist.py", "--file", "jd.txt")
    assert proc.returncode != 0 and "--title" in proc.all_output


# ---- replay_triage ----
def test_replay_with_the_same_deterministic_model_agrees_with_itself(ws):
    ws.daily()
    proc = ws.run("replay_triage.py", "--file", DAY_FILE, "--positives", "4", "--others", "4", "--seed", "1", today=DAY)
    assert proc.returncode == 0, proc.all_output
    report = ws.read(f"feedback/triage_replay_claude-haiku-4-5-20251001_{DAY}.md")
    assert "Identical best score: 7/7 (100%)" in report and "Baseline >= 4 and still >= 4 on replay**: 4/4" in report
    # replaying never changes the scored data or the cache
    cached = ws.db().execute("SELECT COUNT(*) FROM triage_scores").fetchone()[0]
    assert cached == 17


def test_replay_needs_a_file_name(ws):
    proc = ws.run("replay_triage.py")
    assert proc.returncode != 0 and "--file" in proc.all_output


# ---- backfill ----
def test_backfill_dry_run_prints_the_plan_and_calls_nothing(ws):
    proc = ws.run("backfill.py", "--dry-run", "--run-date", "20261005")
    assert proc.returncode == 0, proc.all_output
    assert "Backfill batch 20261005: location Austin, TX, past 14 days, cap 1000 per query" in proc.all_output
    assert "to fetch: A, B" in proc.all_output and "dry-run: no API calls made" in proc.all_output
    assert ws.calls() == []


def test_backfill_rejects_unknown_queries(ws):
    proc = ws.run("backfill.py", "--dry-run", "--queries", "Z")
    assert proc.returncode != 0 and "No such queries: Z (available: A, B)" in proc.all_output


def test_a_backfill_writes_its_own_shortlist_folder(ws):
    proc = ws.run("backfill.py", "--run-date", "20261005", "--cap", "20", "--queries", "A", today=DAY)
    assert proc.returncode == 0, proc.all_output
    assert ws.exists("data/1_raw/raw_backfill_20261005.json")
    folders = [p.name for p in (ws.path / "data" / "7_shortlist").iterdir()]
    assert folders == [f"{DAY}-backfill"]
    assert ws.shortlist(f"{DAY}-backfill")


def test_a_backfill_can_be_resumed_without_fetching_finished_queries_again(ws):
    ws.run("backfill.py", "--run-date", "20261005", "--cap", "20", "--queries", "A", "--fetch-only", today=DAY)
    searches = len(ws.calls("apify"))
    proc = ws.run("backfill.py", "--run-date", "20261005", "--cap", "20", "--queries", "A", "--fetch-only", today=DAY)
    assert proc.returncode == 0 and len(ws.calls("apify")) == searches
    assert re.search(r"Done \(skipped\): A", proc.all_output)
