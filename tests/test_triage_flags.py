"""Triage flags (config `triage_flags.enabled`, default off): the triage model reports level_too_high, not_interested, suspicious
and interest_match next to the scores; the program caps a flagged job's scores so it stays off the shortlist and lists it in the
daily summary. interest_match waives level_too_high only. The rules for each flag are prompt text (prompts.yaml) and the lists
in shared.md, so the engine holds only the mechanism.

Requirements come from the maintainer's original feature (old repository, commits fb4ae5f and d7b864e), ported as "approach A":
mechanism in the engine, judgment rules in the workspace.
"""

import json

import pytest

import prompts  # noqa: E402  (conftest puts the scripts folder on sys.path)
import score  # noqa: E402

DAY = "2026-10-09"
DAY_FILE = "raw_20261009.json"
CAPPED = ("level_too_high", "not_interested", "suspicious")


def raw(a=5, b=1, custom=False, **flags):
    answer = {"scores": {"A": a, "B": b}, "custom_family": custom, "reason": "r"}
    answer.update({f: False for f in (*CAPPED, "interest_match")})
    answer.update(flags)
    return answer


@pytest.fixture
def flags_on(monkeypatch):
    monkeypatch.setattr(score, "FLAGS_ENABLED", True)


# ---- the cap ----
@pytest.mark.parametrize("flag", CAPPED)
def test_each_flag_caps_every_track_at_two_and_keeps_the_raw_scores(flags_on, flag):
    result = score.finalize_triage(raw(a=5, b=4, **{flag: True}))
    assert result["scores"] == {"A": 2, "B": 2} and result["best_score"] == 2
    assert result["scores_raw"] == {"A": 5, "B": 4}
    assert result["flags"] == [flag]


def test_a_cap_never_raises_a_low_score(flags_on):
    assert score.finalize_triage(raw(a=1, b=0, suspicious=True))["scores"] == {"A": 1, "B": 0}


def test_a_flagged_job_is_never_needs_custom(flags_on):
    """Otherwise a capped job could still enter the needs-custom block (its best score 2 is < 4)."""
    assert score.finalize_triage(raw(a=3, custom=True, level_too_high=True))["needs_custom"] is False


def test_several_flags_are_all_recorded(flags_on):
    assert score.finalize_triage(raw(suspicious=True, not_interested=True))["flags"] == ["not_interested", "suspicious"]


def test_no_flag_means_no_change(flags_on):
    result = score.finalize_triage(raw(a=5, b=4))
    assert result["scores"] == {"A": 5, "B": 4} and result["flags"] == [] and "scores_raw" not in result


# ---- interest_match ----
def test_interest_match_waives_level_too_high(flags_on):
    """The user still wants to try a job they are interested in when it asks for more years than they have."""
    result = score.finalize_triage(raw(a=5, level_too_high=True, interest_match=True))
    assert result["scores"]["A"] == 5 and "scores_raw" not in result
    assert result["flags"] == [] and result["waived"] == ["level_too_high"] and result["interest_match"] is True


@pytest.mark.parametrize("flag", ["not_interested", "suspicious"])
def test_interest_match_does_not_waive_the_other_flags(flags_on, flag):
    result = score.finalize_triage(raw(a=5, interest_match=True, **{flag: True}))
    assert result["scores"]["A"] == 2 and result["flags"] == [flag] and result["waived"] == []


def test_interest_match_alone_changes_no_score(flags_on):
    result = score.finalize_triage(raw(a=4, interest_match=True))
    assert result["scores"]["A"] == 4 and result["flags"] == [] and result["waived"] == []


# ---- off by default ----
def test_with_flags_off_the_model_flags_are_ignored_and_not_stored():
    result = score.finalize_triage(raw(a=5, suspicious=True, level_too_high=True))
    assert result["scores"]["A"] == 5 and "scores_raw" not in result
    assert not {"flags", "waived", "interest_match"} & set(result)


# ---- validation ----
@pytest.mark.parametrize("flag", [*CAPPED, "interest_match"])
def test_a_flag_that_is_not_a_boolean_is_rejected_when_on(flags_on, flag):
    with pytest.raises((ValueError, TypeError, KeyError)):
        score.validate_triage(raw(**{flag: "yes"}))


def test_a_missing_flag_is_rejected_when_on(flags_on):
    answer = raw()
    del answer["suspicious"]
    with pytest.raises((ValueError, KeyError)):
        score.validate_triage(answer)


def test_flags_are_not_required_when_off():
    score.validate_triage({"scores": {"A": 1, "B": 1}, "custom_family": False, "reason": "r"})


# ---- years requirements past the cut ----
def test_years_after_the_cut_are_passed_to_the_model_when_on(flags_on):
    """A posting often states its years requirement near the end, past what the triage model sees."""
    job = {"title": "T", "description": "a" * 300 + " Requires 5+ years of agency experience. " + "b" * 300}
    prompt = score.triage_user_prompt(job, 200)
    assert "<years_excerpts>" in prompt and "5+ years of agency experience" in prompt


def test_years_inside_the_visible_part_are_not_repeated(flags_on):
    job = {"title": "T", "description": "Requires 3 years. " + "a" * 500}
    assert "<years_excerpts>" not in score.triage_user_prompt(job, 200)


def test_no_years_excerpts_when_off():
    job = {"title": "T", "description": "a" * 300 + " Requires 5+ years of agency experience. " + "b" * 300}
    assert "<years_excerpts>" not in score.triage_user_prompt(job, 200)


# ---- end to end: schema, prompt, shortlist and summary ----
def job_like(base: dict, job_id: str, title: str, company: str, description: str) -> dict:
    return {**base, "jobId": job_id, "title": title, "company": company, "companyId": f"c-{job_id}",
            "description": description, "jobUrl": f"https://example.com/jobs/{job_id}"}


@pytest.fixture
def flagged_jobs(ws, job_by_id):
    base = job_by_id["1001"]
    jobs = [
        job_like(base, "2001", "Marketing Associate", "Suspect Co", "Ad copy work. FLAG:suspicious"),
        job_like(base, "2002", "Marketing Coordinator", "Loved Co", "Events. FLAG:level_too_high FLAG:interest_match"),
        job_like(base, "2003", "Marketing Coordinator", "Boring Co", "Claims. FLAG:not_interested FLAG:interest_match"),
        job_like(base, "2004", "Marketing Associate", "Plain Co", "Social media work for a consumer brand."),
        job_like(base, "2005", "Immersive Experience Producer", "Senior Studio", "Shows. FLAG:level_too_high"),
    ]
    ws.use_jobs(jobs)
    return jobs


def scored_by_company(ws) -> dict:
    return {j["company"]: j["triage"] for j in json.loads(ws.read(f"data/6_scored/{DAY_FILE}"))}


def picked(ws) -> str:
    return " ".join(ws.shortlist(DAY))


def test_with_flags_off_the_schema_and_prompt_have_no_flag_content(ws, flagged_jobs):
    assert ws.daily().returncode == 0
    call = ws.calls("anthropic", "submit_triage")[0]
    assert not {*CAPPED, "interest_match"} & set(call["schema"]["properties"])
    assert score.P["flags_rubric"] not in call["system"]
    assert "Suspect-Co" in picked(ws)  # nothing is capped


def test_with_flags_on_the_schema_requires_all_four_and_the_rubric_is_sent(ws, flagged_jobs):
    ws.update_config({"triage_flags": {"enabled": True}})
    proc = ws.daily()
    assert proc.returncode == 0, proc.all_output
    call = ws.calls("anthropic", "submit_triage")[0]
    for flag in (*CAPPED, "interest_match"):
        assert call["schema"]["properties"][flag]["type"] == "boolean" and flag in call["schema"]["required"]
    assert score.P["flags_rubric"] in call["system"]


def test_with_flags_on_flagged_jobs_leave_the_shortlist_and_are_listed_in_the_summary(ws, flagged_jobs):
    ws.update_config({"triage_flags": {"enabled": True}})
    assert ws.daily().returncode == 0
    triage = scored_by_company(ws)
    assert triage["Suspect Co"]["best_score"] == 2 and triage["Suspect Co"]["scores_raw"]["A"] == 5
    assert triage["Boring Co"]["flags"] == ["not_interested"]
    assert triage["Loved Co"]["best_score"] == 4 and triage["Loved Co"]["waived"] == ["level_too_high"]
    assert triage["Senior Studio"]["needs_custom"] is False

    names = picked(ws)
    assert "Plain-Co" in names and "Loved-Co" in names
    assert "Suspect-Co" not in names and "Boring-Co" not in names and "Senior-Studio" not in names

    summary = ws.read(f"data/7_shortlist/{DAY}/_summary.md")
    fill, tx = score.fill, score.TX
    assert fill(tx["summary_flag_capped"], label=tx["flag_suspicious"], cap=2, n=1) in summary
    assert fill(tx["summary_flag_capped"], label=tx["flag_not_interested"], cap=2, n=1) in summary
    assert fill(tx["summary_flag_capped"], label=tx["flag_level_too_high"], cap=2, n=1) in summary
    assert fill(tx["summary_flag_item"], company="Suspect Co", title="Marketing Associate", reason="Fake reason: Marketing Associate") in summary
    assert fill(tx["summary_flag_waived"], n=1) in summary
    assert fill(tx["summary_flag_item"], company="Loved Co", title="Marketing Coordinator", reason="Fake reason: Marketing Coordinator") in summary


def test_the_flag_rules_come_from_prompts_yaml(ws, flagged_jobs):
    """The judgment rules belong to the workspace; the engine only ships a generic default."""
    ws.update_config({"triage_flags": {"enabled": True}})
    ws.write("prompts.yaml", 'flags_rubric: "MY OWN FLAG RULES: five or more years is too high."\n')
    assert ws.daily().returncode == 0
    system = ws.calls("anthropic", "submit_triage")[0]["system"]
    assert "MY OWN FLAG RULES" in system and prompts.DEFAULTS["flags_rubric"][:60] not in system


def test_turning_flags_on_rescores_cached_jobs(ws, flagged_jobs):
    assert ws.daily().returncode == 0
    ws.update_config({"triage_flags": {"enabled": True}})
    before = len(ws.calls("anthropic", "submit_triage"))
    assert ws.run("score.py", "--only", DAY_FILE, "--run-date", DAY).returncode == 0
    assert len(ws.calls("anthropic", "submit_triage")) > before


# ---- config and dry run ----
def test_triage_flags_must_be_a_section_with_a_boolean_switch(ws):
    ws.update_config({"triage_flags": "yes"})
    proc = ws.run("score.py", "--help")
    assert proc.returncode != 0 and "triage_flags" in proc.all_output


def test_dry_run_reports_the_switch(ws):
    ws.update_config({"triage_flags": {"enabled": True}})
    proc = ws.run("run_daily.py", "--dry-run", "--json")
    assert json.loads(proc.stdout)["plan"]["triage_flags_enabled"] is True


def test_dry_run_warns_when_the_cap_would_not_keep_flagged_jobs_off_the_shortlist(ws):
    ws.update_config({"triage_flags": {"enabled": True}, "scoring": {"shortlist_min_score": 2}})
    checks = json.loads(ws.run("run_daily.py", "--dry-run", "--json").stdout)["checks"]
    assert any(c["name"] == "triage_flags" and c["status"] == "warn" for c in checks)
