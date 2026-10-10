"""The shortlist notes and the daily summary file: what is written, and what a rewrite keeps."""

import pytest

import score  # noqa: E402  (conftest puts the scripts folder on sys.path)

LEGACY_USAGE_LINE = "- 初評 API 用量：19 calls"  # the label older summary files used (see LEGACY_USAGE_PREFIX)


def make_job(job_id="1", title="Marketing Associate", company="Acme", best=5, custom=False, **extra):
    triage = score.finalize_triage({"scores": {"A": best, "B": 1}, "custom_family": custom, "reason": "because", "visa_blocked": False})
    return {"jobId": job_id, "title": title, "company": company, "location": "Austin, TX", "postedDate": "2026-10-08",
            "jobUrl": f"https://example.com/{job_id}", "description": "Do things.",
            "applicantsDetails": {"text": "12 applicants", "kind": "exact", "count": 12}, "triage": triage, **extra}


def usages(calls=0):
    result = {key: score.Usage() for key in ("triage", "detail", "summary")}
    for u in result.values():
        u.calls = calls
    return result


def write(root, jobs, calls=0, day="2026-10-09"):
    selection = {"main": jobs, "custom": []}
    score.write_shortlist(selection, jobs, day, "2026-10-08", [], usages(calls), [("Fetched", len(jobs))], [], root=root, folder_name=day)
    return root / day


def note_of(day_dir, name):
    return (day_dir / name / f"{name}.md").read_text(encoding="utf-8")


def test_folder_and_note_names_have_rank_company_and_title(tmp_path):
    day = write(tmp_path, [make_job("1"), make_job("2", title="Brand Specialist", company="Beta LLC")])
    assert sorted(p.name for p in day.iterdir() if p.is_dir()) == ["01_Acme_Marketing-Associate", "02_Beta-LLC_Brand-Specialist"]
    assert (day / "_summary.md").exists()


def test_front_matter_properties(tmp_path):
    job = make_job(detail={"resume": "A", "emphasize": ["x", "y"], "gaps": ["g"]}, summary="A short summary.")
    text = note_of(write(tmp_path, [job]), "01_Acme_Marketing-Associate")
    head = text.split("---")[1]
    for line in ('title: "Marketing Associate"', 'company: "Acme"', 'source: "linkedin"', "rank: 1", "date_recommended: 2026-10-09",
                 "date_scraped: 2026-10-08", 'best_track: "A"', "best_score: 5", "score_A: 5", "score_B: 1",
                 'resume: "Resume_Marketing.docx"', 'location: "Austin, TX"', 'salary: "Not disclosed"', 'applicants: "12 applicants"',
                 'url: "https://example.com/1"', 'jobId: "1"', "status:\n  - backlog", "interest:", "tags:\n  - track/A-Marketing", "user note:"):
        assert line in head, line


def test_body_sections(tmp_path):
    job = make_job(detail={"resume": "A", "emphasize": ["x", "y"], "gaps": ["g"]}, summary="A short summary.")
    text = note_of(write(tmp_path, [job]), "01_Acme_Marketing-Associate")
    assert "# Marketing Associate — Acme" in text
    assert "- Best: A Marketing 5" in text and "- Triage reason: because" in text
    assert "## SUMMARY\n\nA short summary." in text
    assert "## Detailed review" in text and "- Emphasize: x; y" in text and "- Gaps: g" in text
    assert text.rstrip().endswith("Do things.")


def test_sections_without_data_are_left_out(tmp_path):
    text = note_of(write(tmp_path, [make_job(description="")]), "01_Acme_Marketing-Associate")
    assert "## SUMMARY" not in text and "## Detailed review" not in text
    assert "(no description)" in text


def test_needs_custom_adds_a_tag(tmp_path):
    text = note_of(write(tmp_path, [make_job(best=3, custom=True)]), "01_Acme_Marketing-Associate")
    assert "  - needs-custom" in text


def test_a_job_without_a_link_or_count(tmp_path):
    text = note_of(write(tmp_path, [make_job(jobUrl=None, applicantsDetails=None)]), "01_Acme_Marketing-Associate")
    assert "- Link: none" in text and 'applicants: "Unknown"' in text


def test_non_linkedin_jobs_have_no_job_id_or_rank(tmp_path):
    job = make_job(source="other", jobId="other-abc")
    text = score.format_job_note(None, job, "2026-10-09", "2026-10-08", "backlog")
    assert 'source: "other"' in text and "jobId:" not in text and "rank:" not in text


# ---- reading notes back ----
def test_notes_can_be_read_back(tmp_path):
    day = write(tmp_path, [make_job(summary="Kept summary.")])
    md = day / "01_Acme_Marketing-Associate" / "01_Acme_Marketing-Associate.md"
    assert score.read_frontmatter(md)["jobId"] == "1"
    assert score.read_status(md) == ["backlog"]
    assert score.read_summary(md) == "Kept summary."
    assert score.find_job_folder(day, "1") == md.parent
    assert score.find_job_folder(day, "nope") is None


def test_a_rewrite_keeps_what_the_user_changed(tmp_path):
    day = write(tmp_path, [make_job("1"), make_job("2", company="Beta")])
    md = day / "01_Acme_Marketing-Associate" / "01_Acme_Marketing-Associate.md"
    edited = md.read_text(encoding="utf-8").replace("status:\n  - backlog", "status:\n  - applied").replace(
        "interest:\n", "interest: true\n").replace("user note:\n", 'user note: "called the recruiter"\n')
    md.write_text(edited, encoding="utf-8")
    write(tmp_path, [make_job("1"), make_job("2", company="Beta")])
    again = md.read_text(encoding="utf-8")
    assert "status:\n  - applied" in again and "interest: true" in again and 'user note: "called the recruiter"' in again
    assert score.find_job_folder(day, "1") == md.parent  # the folder is found by jobId, not renamed


def test_an_existing_summary_is_reused_without_calling_the_model(tmp_path):
    day = write(tmp_path, [make_job(summary="Edited by hand.")])
    job = make_job()
    score.add_summaries(None, {"scoring": {}}, score.Usage(), {"main": [job], "custom": []}, day)  # client None would crash if used
    assert job["summary"] == "Edited by hand."


# ---- the summary file ----
def test_summary_file_has_counts_tables_and_usage(tmp_path):
    day = write(tmp_path, [make_job("1"), make_job("2", company="Beta", best=4)], calls=2)
    text = (day / "_summary.md").read_text(encoding="utf-8")
    assert text.startswith("# Daily shortlist summary 2026-10-09")
    assert "- Shortlist: main 2, needs custom resume 0" in text
    assert "| Stage | A Marketing | B Project management | Total |" in text
    assert "- Triage API usage: 2 calls" in text


def test_a_cached_rerun_keeps_the_usage_the_day_really_cost(tmp_path):
    day = write(tmp_path, [make_job()], calls=3)
    write(tmp_path, [make_job()], calls=0)  # nothing was called this time
    text = (day / "_summary.md").read_text(encoding="utf-8")
    assert text.count("API usage") == 3 and "3 calls" in text


def test_usage_lines_written_with_the_old_label_are_still_kept(tmp_path):
    day = write(tmp_path, [make_job()], calls=1)
    (day / "_summary.md").write_text("# old\n" + LEGACY_USAGE_LINE + "\n", encoding="utf-8")
    write(tmp_path, [make_job()], calls=0)
    assert LEGACY_USAGE_LINE in (day / "_summary.md").read_text(encoding="utf-8")


def test_visa_blocked_jobs_are_listed_in_the_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(score, "VISA_ENABLED", True)
    blocked = make_job("9", company="Blocked Co", best=5)
    blocked["triage"] = score.finalize_triage({"scores": {"A": 5, "B": 4}, "custom_family": False, "visa_blocked": True, "reason": "no sponsorship"})
    day = write(tmp_path, [make_job("1"), blocked])
    text = (day / "_summary.md").read_text(encoding="utf-8")
    assert "visa-blocked (scores capped at 1): 1" in text and "Blocked Co | Marketing Associate: no sponsorship" in text
