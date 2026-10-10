"""Step 4 hard filters. These pin the current behavior, so a change to a rule shows up as a failing test."""

import os
import sqlite3
from pathlib import Path

import pytest
import yaml

import filter as flt  # noqa: E402  (conftest puts the scripts folder on sys.path)

BOOT = Path(os.environ["JOBSEARCH_HOME"])


def config(**patch) -> dict:
    cfg = yaml.safe_load((BOOT / "config.yaml").read_text(encoding="utf-8"))
    cfg.update(patch)
    return cfg


def job(**fields) -> dict:
    return {"title": "Marketing Coordinator", "company": "Acme", "location": "Austin, TX", "description": "Support campaigns.",
            "applicantsDetails": {"kind": "exact", "count": 10, "min": 10, "max": 10}, **fields}


# what the example config does to each fictional job in tests/fixtures/jobs.json (the tracker is a separate step)
EXPECTED = {
    "1001": None, "1002": None, "1003": None, "1004": None, "1005": None, "1006": None, "1007": None, "1008": None,
    "1009": None, "1010": None, "1011": "max_applicants", "1012": None, "1013": "exclude_title", "1014": "exclude_title",
    "1016": "location", "1017": "max_applicants", "1018": "min_years", "1019": None, "1020": "min_years",
    "1021": "exclude_unpaid", "1022": "exclude_unpaid", "1023": "staffing_agency", "1024": None, "1026": None,
    "1028": None, "1029": None, "1030": None, "1031": None,
}


def test_every_fixture_job_is_covered(jobs):
    assert {j["jobId"] for j in jobs} == set(EXPECTED)


@pytest.mark.parametrize("job_id,reason", EXPECTED.items())
def test_example_config_decision(job_by_id, job_id, reason):
    assert flt.filter_job(job_by_id[job_id], config()) == reason


# ---- location ----
def test_location_is_a_case_insensitive_substring():
    assert flt.passes_location(job(location="Greater AUSTIN, TX Area"), config())
    assert not flt.passes_location(job(location="Denver, CO"), config())


def test_empty_whitelist_excludes_everything():
    assert not flt.passes_location(job(), config(location_whitelist=[]))


# ---- applicants ----
@pytest.mark.parametrize("details,expected", [
    ({"kind": "exact", "count": 40, "min": 40, "max": 40}, 40),
    ({"kind": "upper_bound", "count": None, "min": None, "max": 25}, 25),
    ({"kind": "lower_bound", "count": None, "min": 200, "max": None}, 200),
    ({"kind": "unknown"}, None), (None, None), ({}, None),
])
def test_effective_applicant_count(details, expected):
    assert flt.effective_applicant_count(details) == expected


def test_applicant_cap_is_inclusive_and_missing_counts_pass():
    cfg = config(max_applicants=75)
    exact = lambda n: {"kind": "exact", "count": n}
    assert flt.passes_applicant_cap(job(applicantsDetails=exact(75)), cfg)
    assert not flt.passes_applicant_cap(job(applicantsDetails=exact(76)), cfg)
    assert flt.passes_applicant_cap(job(applicantsDetails=None), cfg)


# ---- title ----
def test_title_patterns_match_whole_words_only():
    cfg = config(exclude_title_patterns=["Lead"])
    assert not flt.passes_title(job(title="Lead Designer"), cfg)
    assert not flt.passes_title(job(title="Marketing lead"), cfg)
    assert flt.passes_title(job(title="Team Leader"), cfg)


def test_title_patterns_ignore_what_is_in_parentheses():
    cfg = config(exclude_title_patterns=["Senior"])
    assert flt.passes_title(job(title="(Senior) Product Builder"), cfg)
    assert not flt.passes_title(job(title="Senior Product Builder"), cfg)


def test_multi_word_title_patterns():
    cfg = config(exclude_title_patterns=["Head of", "Vice President"])
    assert not flt.passes_title(job(title="Head of Growth"), cfg)
    assert not flt.passes_title(job(title="Vice President, Brand"), cfg)


# ---- required years ----
@pytest.mark.parametrize("text,expected", [
    ("Requires 5+ years of experience.", 5),
    ("3-5 years of experience", 3),
    ("five to seven years of experience", 5),
    ("At least 5 years of experience", 5),
    ("5 years or more of experience", 5),
    ("minimum of 3 years' experience", 3),
    ("One year of experience", 1),
    ("2+ years of experience or 6+ years of experience", 2),  # alternatives: the smallest counts
    ("5+ years preferred.", None),  # a bonus, not a requirement
    ("We have 50 years of experience in the industry.", None),  # company history
    ("Serving customers for 20+ years", None),
    ("10 years of history", None),
    ("no experience needed", None),
    ("", None),
])
def test_required_years(text, expected):
    assert flt.required_years(text) == expected


def test_years_limit_is_a_minimum():
    cfg = config(min_years_exclude=5)
    assert not flt.passes_min_years(job(description="5+ years of experience"), cfg)
    assert flt.passes_min_years(job(description="4 years of experience"), cfg)
    assert flt.passes_min_years(job(), config(min_years_exclude=0))  # 0 turns the rule off


# ---- unpaid, keywords, staffing ----
def test_unpaid_rules():
    cfg = config()
    assert not flt.passes_unpaid(job(title="Volunteer Coordinator"), cfg)
    assert not flt.passes_unpaid(job(description="This is an unpaid position."), cfg)
    assert flt.passes_unpaid(job(description="Includes unpaid time off for holidays."), cfg)


def test_exclude_keywords_look_in_title_and_description():
    cfg = config(exclude_keywords=["Crypto"])
    assert not flt.passes_keywords(job(title="Crypto Marketer"), cfg)
    assert not flt.passes_keywords(job(description="We work in crypto."), cfg)
    assert flt.passes_keywords(job(), cfg)


def test_staffing_agencies_match_the_company_name():
    cfg = config(staffing_agency_blacklist=["Talent Bridge"])
    assert not flt.passes_staffing_agency(job(company="TALENT BRIDGE Inc"), cfg)
    assert flt.passes_staffing_agency(job(company="Acme"), cfg)
    assert flt.passes_staffing_agency(job(company="Talent Bridge"), config(staffing_agency_blacklist=["Talent Bridge"], exclude_staffing_agencies=False))


# ---- visa ----
def visa_cfg(**visa):
    return config(visa={"enabled": True, "skip_locations": [], "exclude_patterns": ["will not sponsor"], **visa})


def test_visa_rule_is_off_by_default(job_by_id):
    assert flt.filter_job(job_by_id["1024"], config()) is None


def test_visa_rule_when_enabled(job_by_id):
    assert flt.filter_job(job_by_id["1024"], visa_cfg()) == "exclude_visa"


def test_visa_phrases_ignore_case_and_whitespace():
    assert not flt.passes_visa(job(description="We WILL   NOT\nsponsor visas."), visa_cfg())


def test_visa_skip_locations_are_exempt():
    cfg = visa_cfg(skip_locations=["austin"])
    assert flt.passes_visa(job(description="We will not sponsor."), cfg)


def test_old_visa_keys_stop_the_program():
    with pytest.raises(SystemExit):
        flt.visa_settings({"visa_skip_locations": []})
    with pytest.raises(SystemExit):
        flt.visa_settings({"exclude_visa_patterns": []})


def test_visa_settings_defaults():
    assert flt.visa_settings({}) == {"enabled": False, "skip_locations": [], "exclude_patterns": []}


# ---- tracker (jobs already applied to or decided against) ----
def test_tracker_parsing(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("Applied: A1 · Acme | B1 · Beta｜C1 · Gamma\nEvaluated, skipped：D1 · Delta Inc\nnot a line\n", encoding="utf-8")
    entries = flt.load_tracker(f, flt.tracker_labels({}))
    assert [(e["title"], e["company"], e["status"]) for e in entries] == [
        ("A1", "Acme", "applied"), ("B1", "Beta", "applied"), ("C1", "Gamma", "applied"), ("D1", "Delta Inc", "evaluated_skip")]


def test_tracker_labels_can_be_other_words(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("已投遞：A1 · Acme\n", encoding="utf-8")
    assert flt.load_tracker(f, flt.tracker_labels({"tracker_labels": {"applied": "已投遞"}}))[0]["status"] == "applied"
    assert flt.load_tracker(f, flt.tracker_labels({})) == []  # the default English words do not match


def test_tracker_labels_reject_unknown_statuses():
    with pytest.raises(SystemExit):
        flt.tracker_labels({"tracker_labels": {"bogus": "x"}})


def test_missing_tracker_file_is_skipped_with_a_warning(tmp_path, capsys):
    assert flt.load_tracker(tmp_path / "nope.txt", flt.tracker_labels({})) == []
    assert "not found" in capsys.readouterr().out


def test_tracker_matches_company_ignoring_suffix_and_case(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("Applied: Marketing Coordinator · Acme Inc\n", encoding="utf-8")
    tracker = flt.load_tracker(f, flt.tracker_labels({}))
    exact, others = flt.match_tracker(job(title="marketing coordinator", company="ACME, LLC"), tracker)
    assert exact and not others
    exact, others = flt.match_tracker(job(title="Brand Manager", company="Acme"), tracker)
    assert exact is None and len(others) == 1


def test_batch_drops_exact_tracker_matches_and_annotates_same_company(tmp_path, monkeypatch, job_by_id):
    monkeypatch.setattr(flt, "geocode", lambda address, conn: None)  # never touch the network
    shutil_target = tmp_path / "tracker.txt"
    shutil_target.write_text((Path(__file__).parent / "fixtures" / "tracker.txt").read_text(encoding="utf-8"), encoding="utf-8")
    cfg = config(tracker_path=str(shutil_target))
    result = flt.filter_batch([job_by_id["1028"], job_by_id["1029"], job_by_id["1001"]], cfg, sqlite3.connect(":memory:"))
    assert [j["jobId"] for j in result["kept"]] == ["1029", "1001"]
    assert [j["jobId"] for j in result["excluded_by_reason"]["already_in_tracker"]] == ["1028"]
    assert result["kept"][0]["tracker_company_match"] == [{"title": "Brand Manager", "status": "applied"}]
    assert "tracker_company_match" not in result["kept"][1]
