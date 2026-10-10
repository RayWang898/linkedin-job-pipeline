"""Step 3 dedup: what counts as seen, and the guarantee that jobs are marked seen only once the output file exists."""

import json

import pytest

import dedup  # noqa: E402  (conftest puts the scripts folder on sys.path)


@pytest.fixture
def conn(tmp_path):
    return dedup.init_db(tmp_path / "d.db")


def test_new_jobs_are_kept_and_remembered(conn, jobs):
    result = dedup.dedup_batch(conn, jobs[:3])
    assert [j["jobId"] for j in result["kept"]] == ["1001", "1002", "1003"]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_a_job_id_seen_before_is_skipped(conn, jobs):
    dedup.dedup_batch(conn, jobs[:3])
    again = dedup.dedup_batch(conn, jobs[:5])
    assert [j["jobId"] for j in again["kept"]] == ["1004", "1005"]
    assert again["skipped_seen_job_id"] == ["1001", "1002", "1003"]


def test_same_company_and_description_under_a_new_id_is_a_duplicate(conn, job_by_id):
    result = dedup.dedup_batch(conn, [job_by_id["1001"], job_by_id["1026"]])
    assert [j["jobId"] for j in result["kept"]] == ["1001"]  # the first one seen wins
    assert result["skipped_duplicate_content"] == [("1026", "1001")]
    assert conn.execute("SELECT duplicate_of FROM jobs WHERE job_id = '1026'").fetchone()[0] == "1001"


def test_duplicates_are_found_across_batches(conn, job_by_id):
    dedup.dedup_batch(conn, [job_by_id["1001"]])
    result = dedup.dedup_batch(conn, [job_by_id["1026"]])
    assert result["kept"] == [] and result["skipped_duplicate_content"] == [("1026", "1001")]


def test_company_key_prefers_the_company_id():
    assert dedup.company_key({"companyId": "123", "company": "Acme"}) == "id:123"
    assert dedup.company_key({"company": " ACME "}) == "name:acme"


def test_commit_false_leaves_the_marks_pending(conn, jobs, tmp_path):
    dedup.dedup_batch(conn, jobs[:3], commit=False)
    other = dedup.init_db(tmp_path / "d.db")  # a second connection sees only committed rows
    assert other.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    conn.commit()
    assert other.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_jobs_are_not_marked_seen_if_the_output_file_cannot_be_written(conn, jobs, tmp_path, monkeypatch):
    slim = tmp_path / "raw_x.json"
    slim.write_text(json.dumps(jobs[:5]), encoding="utf-8")
    monkeypatch.setattr(dedup, "DEDUPED_DIR", tmp_path / "3_deduped")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(dedup.json, "dump", boom)
    with pytest.raises(OSError):
        dedup.process_slim_file(conn, slim)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # a rerun finds them new again

    monkeypatch.undo()
    monkeypatch.setattr(dedup, "DEDUPED_DIR", tmp_path / "3_deduped")
    dedup.process_slim_file(conn, slim)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 5
    assert len(json.loads((tmp_path / "3_deduped" / "raw_x.json").read_text(encoding="utf-8"))) == 5
