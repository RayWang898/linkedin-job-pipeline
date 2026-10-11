"""init.py: create a workspace without overwriting anything."""

import os
import stat
import tempfile
from pathlib import Path

from conftest import ENGINE, run_init


def test_creates_the_expected_files(tmp_path):
    target = tmp_path / "ws"
    proc = run_init(target)
    assert proc.returncode == 0, proc.stderr
    for name in ("config.yaml", ".env", ".gitignore"):
        assert (target / name).exists(), name
    assert (target / "profiles").is_dir() and (target / "resumes").is_dir()
    assert "created  config.yaml" in proc.stdout
    assert "--dry-run" in proc.stdout  # points at the next step


def test_example_profiles_are_not_copied_into_the_workspace(tmp_path):
    """The workspace holds only the user's own profiles: example text left behind (b_pm.md) looks like finished work."""
    target = tmp_path / "ws"
    assert run_init(target).returncode == 0
    assert (target / "profiles").is_dir()
    assert list((target / "profiles").iterdir()) == []


def test_env_file_is_private_and_empty_of_secrets(tmp_path):
    target = tmp_path / "ws"
    run_init(target)
    mode = stat.S_IMODE(os.stat(target / ".env").st_mode)
    assert mode == 0o600
    text = (target / ".env").read_text(encoding="utf-8")
    assert "APIFY_TOKEN=" in text and "ANTHROPIC_API_KEY=" in text


def test_gitignore_keeps_keys_and_data_out_of_git(tmp_path):
    target = tmp_path / "ws"
    run_init(target)
    lines = (target / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", "config.yaml", "profiles/", "resumes/", "data/", "logs/", "*.db"):
        assert entry in lines


def test_a_second_run_never_overwrites(tmp_path):
    target = tmp_path / "ws"
    run_init(target)
    (target / "profiles" / "shared.md").write_text("my own text\n", encoding="utf-8")
    (target / "config.yaml").write_text("my: config\n", encoding="utf-8")
    proc = run_init(target)
    assert proc.returncode == 0
    assert (target / "profiles" / "shared.md").read_text(encoding="utf-8") == "my own text\n"
    assert (target / "config.yaml").read_text(encoding="utf-8") == "my: config\n"
    assert "kept     config.yaml" in proc.stdout


def test_adds_only_what_is_missing(tmp_path):
    target = tmp_path / "ws"
    run_init(target)
    (target / ".env").unlink()
    proc = run_init(target)
    assert "created  .env" in proc.stdout and "kept     config.yaml" in proc.stdout


def test_language_option_copies_prompts_and_texts(tmp_path):
    target = tmp_path / "ws"
    proc = run_init(target, "--language", "zh-TW")
    assert proc.returncode == 0
    assert (target / "prompts.yaml").exists() and (target / "texts.yaml").exists()


def test_unknown_language_is_refused_before_anything_is_created(tmp_path):
    target = tmp_path / "ws"
    proc = run_init(target, "--language", "fr")
    assert proc.returncode != 0
    assert "Available: zh-TW" in proc.stderr
    assert not target.exists()


def test_target_that_is_a_file_is_refused(tmp_path):
    target = tmp_path / "afile"
    target.write_text("x")
    proc = run_init(target)
    assert proc.returncode != 0 and "not a folder" in proc.stderr


def test_a_workspace_inside_the_engine_repository_gets_an_honest_warning():
    """Requirement: inside the repository some user files (prompts.yaml, an applied-jobs list) are not ignored by git, so the note
    must say to keep the workspace outside, or to never use git add -A, and must not promise that all files are kept out of git."""
    with tempfile.TemporaryDirectory(dir=ENGINE) as inside:
        out = run_init(Path(inside) / "ws").stdout
    note = next(line for line in out.splitlines() if line.startswith("Note:"))
    assert "outside" in note and "git add -A" in note
    assert "keeps your files out of git" not in out


def test_a_workspace_outside_the_repository_gets_no_such_note(tmp_path):
    assert "inside the engine repository" not in run_init(tmp_path / "ws").stdout
