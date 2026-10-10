"""Mistakes in config.yaml, profiles, prompts.yaml and texts.yaml stop with a message that says what to fix."""

import pytest

ONE_TRACK = {"id": "A", "name": "Ops", "tag": "Ops", "profile": "a_marketing.md", "resume": "R.docx"}


def load(ws):
    return ws.run("score.py", "--help")  # importing score loads and validates the whole config


def test_valid_example_config_loads(ws):
    proc = load(ws)
    assert proc.returncode == 0, proc.all_output


def test_missing_config_file(ws):
    (ws.path / "config.yaml").unlink()
    proc = load(ws)
    assert proc.returncode != 0 and "config.yaml" in proc.all_output


@pytest.mark.parametrize("tracks,message", [
    (None, "must be a list with at least one track"),
    ([], "must be a list with at least one track"),
    ([ONE_TRACK, {**ONE_TRACK, "name": "Again"}], "track id A is used twice"),
    ([{**ONE_TRACK, "id": "ab"}], "must be a single uppercase letter"),
    ([{**ONE_TRACK, "id": "a"}], "must be a single uppercase letter"),
    ([{k: v for k, v in ONE_TRACK.items() if k != "resume"}], "is missing resume"),
    ([{**ONE_TRACK, "tag": "  "}], "is missing tag"),
])
def test_invalid_tracks(ws, tracks, message):
    config = ws.read_config()
    config["tracks"] = tracks
    if tracks is None:
        del config["tracks"]
    import yaml
    (ws.path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    proc = load(ws)
    assert proc.returncode != 0 and message in proc.all_output


def test_profile_file_named_in_tracks_must_exist(ws):
    (ws.path / "profiles" / "b_pm.md").unlink()
    proc = ws.run("score.py", "--db", "x.db", "--only", "nothing.json")
    assert proc.returncode != 0
    assert "Missing profile for track B" in proc.all_output


def test_shared_profile_is_required(ws):
    (ws.path / "profiles" / "shared.md").unlink()
    proc = ws.run("score.py", "--db", "x.db", "--only", "nothing.json")
    assert proc.returncode != 0 and "shared" in proc.all_output


def test_old_visa_keys_are_refused_with_the_new_names(ws):
    ws.update_config({"visa_skip_locations": []})
    proc = load(ws)
    assert proc.returncode != 0
    assert "moved under `visa:`" in proc.all_output and "exclude_patterns" in proc.all_output


@pytest.mark.parametrize("name,content,message", [
    ("prompts.yaml", "rubrik: text\n", "unknown names: rubrik"),
    ("prompts.yaml", 'rubric: ""\n', "must be text (not empty)"),
    ("prompts.yaml", "- a\n- b\n", "expected a mapping"),
    ("texts.yaml", "note_bestt: x\n", "unknown names: note_bestt"),
    ("texts.yaml", "note_best:\n", "must be text (not empty)"),
])
def test_override_files_are_validated(ws, name, content, message):
    ws.write(name, content)
    proc = load(ws)
    assert proc.returncode != 0 and message in proc.all_output


def test_tracker_labels_only_accept_the_two_statuses(ws):
    ws.update_config({"tracker_labels": {"bogus": "x"}})
    proc = load(ws)
    assert proc.returncode != 0 and "tracker_labels may only set" in proc.all_output
