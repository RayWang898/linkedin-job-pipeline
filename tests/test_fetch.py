"""Step 1 and 2 logic that needs no Apify: query labels, caps, the actor input, merging and slimming."""

import json

import fetch  # noqa: E402  (conftest puts the scripts folder on sys.path)

APIFY = {"actor": "x/y", "queries": ["q1", "q2", "q3"], "locations": ["Austin, TX"], "max_jobs_per_search": 50,
         "max_jobs_by_location": {}, "max_jobs_by_query": {}, "date_posted": "past_24h", "sort_by": "recent", "fetch_details": True}


def cfg(**patch):
    return {**APIFY, **patch}


def test_queries_are_labeled_a_b_c_in_order():
    assert fetch.query_labels(APIFY) == ["A", "B", "C"]


def test_the_default_cap_and_a_per_query_cap():
    assert fetch.max_jobs_for(APIFY, "A") == 50
    assert fetch.max_jobs_for(cfg(max_jobs_by_query={"B": 150}), "B") == 150
    assert fetch.max_jobs_for(cfg(max_jobs_by_query={"B": 150}), "A") == 50


def test_a_location_cap_applies_only_to_a_single_location_search_and_only_lowers():
    one = cfg(max_jobs_by_location={"Austin, TX": 20})
    assert fetch.max_jobs_for(one, "A") == 20
    assert fetch.max_jobs_for(cfg(max_jobs_by_location={"Austin, TX": 500}), "A") == 50
    two = cfg(locations=["Austin, TX", "Remote"], max_jobs_by_location={"Austin, TX": 20})
    assert fetch.max_jobs_for(two, "A") == 50  # the engine searches one location at a time and passes a one-location config


def test_only_the_first_location_goes_untagged():
    both = cfg(locations=["Austin, TX", "Remote"])
    assert fetch.location_tag(both, "Austin, TX") == "" and fetch.location_tag(both, "Remote") == "@Remote"
    assert fetch.location_tag(cfg(locations=["Austin, TX", "Round Rock, TX"]), "Round Rock, TX") == "@Round Rock"


def test_actor_input():
    run_input = fetch.build_run_input(APIFY, "q1", None, "A")
    assert run_input == {"queries": ["q1"], "locations": ["Austin, TX"], "datePosted": "past_24h", "sortBy": "recent",
                         "fetchDetails": True, "includeSimilar": False, "maxJobsPerSearch": 50, "maxJobs": 50}


def test_max_jobs_override_and_multi_location_total():
    assert fetch.build_run_input(APIFY, "q1", 5, "A")["maxJobs"] == 5
    assert fetch.build_run_input(cfg(locations=["a", "b"]), "q1", None, "A")["maxJobs"] == 100  # actor default would be only 25


def test_results_are_merged_by_job_id_and_remember_every_query():
    merged = fetch.merge_results({"A": [{"jobId": "1", "t": "x"}, {"jobId": "2"}], "B": [{"jobId": "1"}, {"jobId": "3"}]})
    assert {j["jobId"]: j["matched_queries"] for j in merged} == {"1": ["A", "B"], "2": ["A"], "3": ["B"]}
    assert [j["jobId"] for j in merged] == ["1", "2", "3"]


def test_slimming_keeps_only_the_needed_fields(job_by_id):
    raw = {**job_by_id["1001"], "extraField": "x", "badge": "y"}
    slim = fetch.slim_job(raw)
    assert set(slim) == set(fetch.ESSENTIAL_FIELDS) and "extraField" not in slim and slim["matched_queries"] is None


def test_field_completeness_counts_non_empty_values():
    counts = fetch.field_completeness([{"jobId": "1", "title": None}, {"jobId": "2", "title": "t"}])
    assert counts["jobId"] == 2 and counts["title"] == 1 and counts["company"] == 0


def test_raw_and_slim_files(tmp_path, monkeypatch, jobs):
    monkeypatch.setattr(fetch, "RAW_DIR", tmp_path / "1_raw")
    monkeypatch.setattr(fetch, "SLIM_DIR", tmp_path / "2_slim")
    path = fetch.save_raw(jobs[:3], "20261009")
    assert path.name == "raw_20261009.json" and len(json.loads(path.read_text(encoding="utf-8"))) == 3
    slimmed = fetch.process_raw_file(path)
    assert len(slimmed) == 3
    assert json.loads((tmp_path / "2_slim" / "raw_20261009.json").read_text(encoding="utf-8")) == slimmed
