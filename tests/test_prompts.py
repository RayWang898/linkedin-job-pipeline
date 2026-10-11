"""What the models are told, and the score cache. A fake Anthropic client records every request (tests/fakes), so these check the
real prompts the real code builds, and that the cache stops repeat calls."""

import json

import score  # noqa: E402  (for the built-in prompt texts)

DAY = "2026-10-09"
DAY_FILE = "raw_20261009.json"


def triage_calls(ws):
    return ws.calls("anthropic", "submit_triage")


def rescore(ws):
    """Run only the scoring step again on the day's filtered file; returns how many new model calls it made."""
    before = len(ws.calls("anthropic"))
    proc = ws.run("score.py", "--only", DAY_FILE, "--run-date", DAY)
    assert proc.returncode == 0, proc.all_output
    return len(ws.calls("anthropic")) - before


def add_track(ws, track_id, name, profile):
    tracks = ws.read_config()["tracks"] + [{"id": track_id, "name": name, "tag": name, "profile": profile, "resume": f"{name}.docx"}]
    ws.update_config({"tracks": tracks})
    ws.write(f"profiles/{profile}", f"# {name}\nPositioning for {name}.\n")


# ---- the visa switch ----
def test_with_the_visa_feature_off_the_prompts_and_schema_have_no_visa_content(ws):
    assert ws.daily().returncode == 0
    call = triage_calls(ws)[0]
    assert set(call["schema"]["properties"]) == {"scores", "custom_family", "reason"}
    assert call["schema"]["required"] == ["scores", "custom_family", "reason"]
    assert score.P["visa_rubric"] not in call["system"] and "visa" not in call["system"].lower()
    assert "visa_excerpts" not in call["user"]


def test_with_the_visa_feature_on_the_prompts_schema_and_scores_change(ws):
    ws.update_config({"visa": {"enabled": True, "skip_locations": [], "exclude_patterns": []}})
    proc = ws.daily()
    assert proc.returncode == 0, proc.all_output
    call = triage_calls(ws)[0]
    assert call["schema"]["required"] == ["scores", "custom_family", "visa_blocked", "reason"]
    assert score.P["visa_rubric"] in call["system"]
    assert [c for c in triage_calls(ws) if "visa_excerpts" in c["user"]]  # jobs that mention sponsorship or OPT carry their passages
    scored = {j["jobId"]: j["triage"] for j in json.loads(ws.read(f"data/6_scored/{DAY_FILE}"))}
    assert scored["1010"]["visa_blocked"] is True and scored["1010"]["scores"]["A"] == 4  # flagged, but the score is the model's own
    assert "scores_raw" not in scored["1010"]
    assert scored["1001"]["visa_blocked"] is False and scored["1001"]["scores"]["A"] == 4
    assert "refuse sponsorship (scores unchanged)" in ws.read(f"data/7_shortlist/{DAY}/_summary.md")


def test_visa_filter_phrases_apply_only_when_enabled(ws):
    ws.daily()
    assert "1024" in {j["jobId"] for j in json.loads(ws.read(f"data/4_filtered/{DAY_FILE}"))}
    ws.update_config({"visa": {"enabled": True, "skip_locations": [], "exclude_patterns": ["will not sponsor"]}})
    ws.daily("--force")
    assert "1024" not in {j["jobId"] for j in json.loads(ws.read(f"data/4_filtered/{DAY_FILE}"))}


# ---- tracks ----
def test_four_tracks_reach_the_schema_the_prompt_and_the_notes(ws):
    add_track(ws, "C", "Hybrid", "c.md")
    add_track(ws, "D", "Creative", "d.md")
    assert ws.daily().returncode == 0
    call = triage_calls(ws)[0]
    assert list(call["schema"]["properties"]["scores"]["properties"]) == ["A", "B", "C", "D"]
    for heading in ("## Track A (Marketing)", "## Track B (Project management)", "## Track C (Hybrid)", "## Track D (Creative)"):
        assert heading in call["system"]
    folder = next(f for f in ws.shortlist(DAY) if "Marketing-Associate" in f)
    note = ws.note(DAY, folder)
    assert all(f"score_{t}:" in note for t in "ABCD")
    assert "| A Marketing | B Project management | C Hybrid | D Creative | Total |" in ws.read(f"data/7_shortlist/{DAY}/_summary.md")


def test_one_track_is_enough(ws):
    ws.update_config({"tracks": [ws.read_config()["tracks"][0]]})
    (ws.path / "profiles" / "b_pm.md").unlink()
    assert ws.daily().returncode == 0
    props = triage_calls(ws)[0]["schema"]["properties"]["scores"]["properties"]
    assert list(props) == ["A"]
    assert "score_B" not in ws.note(DAY, ws.shortlist(DAY)[0])


def test_profiles_and_track_headings_are_in_the_system_prompt(ws):
    ws.write("profiles/shared.md", "# shared\nLevel: unique-shared-marker\n")
    ws.write("profiles/a_marketing.md", "# A\nunique-track-a-marker\n")
    ws.daily()
    system = triage_calls(ws)[0]["system"]
    assert "unique-shared-marker" in system and "unique-track-a-marker" in system
    assert system.startswith(score.P["rubric"].split("\n")[0])


# ---- settings that reach the prompts ----
def test_models_come_from_the_config(ws):
    ws.update_config({"scoring": {"triage_model": "triage-x", "detail_model": "detail-x", "summary_model": "summary-x"}})
    ws.daily()
    assert {c["model"] for c in ws.calls("anthropic", "submit_triage")} == {"triage-x"}
    assert {c["model"] for c in ws.calls("anthropic", "submit_detail")} == {"detail-x"}
    assert {c["model"] for c in ws.calls("anthropic", "submit_summary")} == {"summary-x"}


def test_description_length_setting_limits_what_the_triage_model_sees(ws, job_by_id):
    ws.use_jobs([{**job_by_id["1001"], "description": "w" * 3000}])
    ws.update_config({"scoring": {"triage_description_chars": 120}})
    ws.daily()
    user = triage_calls(ws)[0]["user"]
    assert "w" * 120 in user and "w" * 121 not in user


def test_detail_count_and_detail_custom_limit_the_detailed_reviews(ws):
    ws.update_config({"scoring": {"detail_count": 2, "detail_custom": False}})
    ws.daily()
    assert len(ws.calls("anthropic", "submit_detail")) == 2


def test_output_language_is_part_of_the_prompts(ws):
    ws.update_config({"scoring": {"output_language": "Japanese"}})
    ws.daily()
    call = triage_calls(ws)[0]
    assert "in Japanese" in call["system"]
    assert "Japanese" in call["schema"]["properties"]["reason"]["description"]
    assert "Write everything in Japanese" in ws.calls("anthropic", "submit_detail")[0]["system"]
    assert "in Japanese" in ws.calls("anthropic", "submit_summary")[0]["system"]


def test_prompts_yaml_replaces_text_and_keeps_the_rest(ws):
    ws.write("prompts.yaml", 'triage_request: "Please submit the triage now."\nrubric: "CUSTOM RUBRIC {language}"\n')
    ws.daily()
    call = triage_calls(ws)[0]
    assert call["user"].endswith("Please submit the triage now.")
    assert call["system"].startswith("CUSTOM RUBRIC English")  # {language} is still filled in
    assert ws.calls("anthropic", "submit_detail")[0]["system"].startswith(score.P["detail_instructions"][:40])  # untouched keys stay English


# ---- the score cache ----
def test_a_rescore_of_the_same_day_makes_no_model_calls(ws):
    ws.daily()
    assert rescore(ws) == 0


def test_changing_a_profile_makes_triage_run_again(ws):
    ws.daily()
    ws.write("profiles/shared.md", ws.read("profiles/shared.md") + "\nAn extra line.\n")
    assert rescore(ws) >= len(json.loads(ws.read(f"data/4_filtered/{DAY_FILE}")))  # every filtered job is triaged again


def test_changing_a_prompt_makes_triage_run_again(ws):
    ws.daily()
    ws.write("prompts.yaml", 'rubric: "A different rubric."\n')
    assert rescore(ws) > 0


def test_changing_the_triage_model_makes_triage_run_again(ws):
    ws.daily()
    ws.update_config({"scoring": {"triage_model": "another-model"}})
    assert rescore(ws) > 0


def test_a_failing_model_skips_jobs_instead_of_crashing(ws):
    proc = ws.daily(env={"JOBSEARCH_TEST_MODEL_ERRORS": "1"})
    assert proc.returncode == 0
    assert "Skipping" in proc.all_output
    assert ws.shortlist(DAY) == []
    assert ws.runs()[DAY][0] == "success"  # scoring nothing during an outage is not a crash
