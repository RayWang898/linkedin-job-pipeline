"""Scoring logic that needs no model: validation, the best track, the shortlist, the triage prompt.

These import score with the boot workspace (the example config: tracks A Marketing and B Project management, visa off).
"""

import pytest

import score  # noqa: E402  (conftest puts the scripts folder on sys.path)


def raw(a=1, b=1, custom=False, visa=False, reason="r"):
    return {"scores": {"A": a, "B": b}, "custom_family": custom, "visa_blocked": visa, "reason": reason}


# ---- best track and needs_custom ----
def test_best_track_is_the_highest_score():
    assert score.finalize_triage(raw(a=2, b=4))["best_track"] == "B"
    assert score.finalize_triage(raw(a=5, b=4))["best_score"] == 5


def test_a_tie_goes_to_the_earlier_track():
    assert score.finalize_triage(raw(a=3, b=3))["best_track"] == "A"


@pytest.mark.parametrize("a,custom,expected", [(3, True, True), (4, True, False), (3, False, False), (5, True, False)])
def test_needs_custom_means_a_custom_type_job_that_no_track_covers(a, custom, expected):
    assert score.finalize_triage(raw(a=a, custom=custom))["needs_custom"] is expected


def test_visa_flag_never_changes_the_scores(monkeypatch):
    """Requirement (ADR 0005): the model's visa judgment is a warning for the user, never a silent demotion; only the filter's phrases exclude."""
    monkeypatch.setattr(score, "VISA_ENABLED", True)
    result = score.finalize_triage(raw(a=5, b=4, visa=True))
    assert result["scores"] == {"A": 5, "B": 4} and result["best_score"] == 5
    assert result["visa_blocked"] is True and "scores_raw" not in result
    assert score.finalize_triage(raw(a=3, custom=True, visa=True))["needs_custom"] is True


def test_visa_blocked_is_ignored_when_the_feature_is_off():
    result = score.finalize_triage(raw(a=5, visa=True))
    assert result["scores"] == {"A": 5, "B": 1} and result["visa_blocked"] is False and "scores_raw" not in result


# ---- validation of what the model returns ----
@pytest.mark.parametrize("bad", [raw(a=6), raw(a=-1), raw(a="4"), raw(a=4.0), raw(custom="yes"), raw(reason="  ")])
def test_invalid_triage_answers_are_rejected(bad):
    with pytest.raises((ValueError, TypeError)):
        score.validate_triage(bad)


def test_valid_triage_answer_is_accepted():
    score.validate_triage(raw(a=0, b=5))


def test_detail_must_name_a_real_track_and_keep_lists_short():
    score.validate_detail({"resume": "A", "emphasize": ["x"], "gaps": []})
    with pytest.raises(ValueError):
        score.validate_detail({"resume": "Z", "emphasize": [], "gaps": []})
    with pytest.raises(ValueError):
        score.validate_detail({"resume": "A", "emphasize": ["1", "2", "3", "4"], "gaps": []})


def test_summary_must_not_be_empty():
    score.validate_summary({"summary": "ok"})
    with pytest.raises(ValueError):
        score.validate_summary({"summary": "   "})


# ---- the shortlist ----
def scored(job_id, best, custom=False, applicants=None):
    details = {"kind": "exact", "count": applicants} if applicants is not None else None
    return {"jobId": job_id, "applicantsDetails": details, "triage": {"best_score": best, "needs_custom": custom}}


def ids(jobs):
    return [j["jobId"] for j in jobs]


def test_only_scores_at_or_above_the_threshold_are_shortlisted_and_the_list_is_not_padded():
    picks = score.select_shortlist([scored("a", 5), scored("b", 4), scored("c", 3), scored("d", 0)], size=10, min_score=4)
    assert ids(picks["main"]) == ["a", "b"]


def test_size_caps_the_main_list():
    jobs = [scored(str(i), 5) for i in range(6)]
    assert len(score.select_shortlist(jobs, size=4)["main"]) == 4


def test_order_is_score_then_fewer_applicants_with_unknown_last():
    jobs = [scored("many", 4, applicants=90), scored("few", 4, applicants=5), scored("unknown", 4), scored("top", 5, applicants=300)]
    assert ids(score.select_shortlist(jobs, size=10)["main"]) == ["top", "few", "many", "unknown"]


def test_needs_custom_jobs_have_their_own_block_with_their_own_threshold():
    jobs = [scored("c3", 3, custom=True), scored("c2", 2, custom=True), scored("m4", 4), scored("c5", 5, custom=True)]
    picks = score.select_shortlist(jobs, size=1, min_score=4, custom_min_score=3)
    assert ids(picks["custom"]) == ["c5", "c3"]  # c2 is below custom_min_score
    assert ids(picks["main"]) == ["m4"]  # custom jobs never take a main slot


# ---- the triage prompt ----
DESCRIPTION = "Intro. " + "x" * 800 + " We cannot sponsor visas now or later. " + "y" * 800


def test_triage_prompt_truncates_the_description():
    job = {"title": "T", "company": "C", "location": "L", "description": "d" * 5000}
    prompt = score.triage_user_prompt(job, 100)
    assert "d" * 100 in prompt and "d" * 101 not in prompt
    assert prompt.endswith(score.P["triage_request"])


def test_no_visa_text_in_the_prompt_when_the_feature_is_off():
    assert "visa_excerpts" not in score.triage_user_prompt({"title": "T", "description": DESCRIPTION}, 2500)


def test_visa_excerpts_are_added_when_on_except_in_skip_locations(monkeypatch):
    monkeypatch.setattr(score, "VISA_ENABLED", True)
    job = {"title": "T", "location": "Austin, TX", "description": DESCRIPTION}
    assert "<visa_excerpts>" in score.triage_user_prompt(job, 2500)
    assert "<visa_excerpts>" not in score.triage_user_prompt(job, 2500, ["austin"])


def test_visa_excerpts_find_passages_far_beyond_the_truncated_text():
    long_text = "Intro. " + "x" * 800 + " We cannot sponsor visas now or later. " + "y" * 800 + " Must be authorized to work in the US. " + "z" * 800 + " OPT candidates are welcome. " + "w" * 800
    found = score.visa_excerpts(long_text)
    assert len(found) == 3 and "sponsor" in found[0].lower()
    assert score.visa_excerpts("Nothing relevant here.") == []


def test_close_passages_are_merged_into_one():
    text = "Intro " + "a" * 100 + " no sponsorship offered. " + "b" * 50 + " authorized to work only. " + "c" * 100
    assert len(score.visa_excerpts(text)) == 1


# ---- a visa block needs visa text in the posting ----
def test_a_visa_block_without_any_visa_text_in_the_posting_is_dropped():
    """Requirement: the rubric blocks only a posting that explicitly refuses sponsorship. A description with no visa-related
    text cannot refuse anything, so the model's visa_blocked is not trusted (seen in a real run: score 4 capped to 1)."""
    job = {"title": "T", "description": "A role in luxury beauty PR. You will plan influencer campaigns. " * 20}
    fixed = score.drop_unsupported_visa_block(raw(a=4, visa=True), job)
    assert fixed["visa_blocked"] is False and fixed["scores"] == {"A": 4, "B": 1}


def test_a_visa_block_is_kept_when_the_posting_has_visa_text():
    job = {"title": "T", "description": "Great team. We cannot sponsor visas now or later. " + "x" * 3000}
    assert score.drop_unsupported_visa_block(raw(a=4, visa=True), job)["visa_blocked"] is True


def test_a_clear_visa_result_is_never_changed():
    job = {"title": "T", "description": "Nothing about work permits here."}
    assert score.drop_unsupported_visa_block(raw(a=4, visa=False), job) == raw(a=4, visa=False)


# ---- small helpers ----
def test_slug_and_yaml_value():
    assert score.slug('A/B: C?*"<>|#^[]  name') == "A-B-C-name"
    assert len(score.slug("x" * 100)) == 60
    assert (score.yaml_value(True), score.yaml_value(3), score.yaml_value('a "q"')) == ("true", "3", '"a \\"q\\""')


def test_salary_text():
    assert score.salary_text(None) == score.TX["salary_undisclosed"]
    assert score.salary_text({"text": "$50k"}) == "$50k"
    assert score.salary_text({"text": ""}) == score.TX["salary_undisclosed"]
