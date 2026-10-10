"""Workspace resolution: --workspace, then JOBSEARCH_HOME, then the current directory."""

import subprocess
import sys

import pytest

from conftest import SCRIPTS, _clean_env

PROBE = "import sys, os; sys.path.insert(0, %r); import workspace; print(os.getcwd()); print(sys.argv[1:]); print(os.environ.get('FROM_DOTENV'))" % str(SCRIPTS)


def probe(cwd, *args, env=None):
    proc = subprocess.run([sys.executable, "-c", PROBE, *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          env=_clean_env(env))
    return proc


@pytest.fixture
def two(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for p in (a, b, c):
        p.mkdir()
    return a.resolve(), b.resolve(), c.resolve()


def test_argument_beats_environment_beats_current_directory(two):
    a, b, c = two
    assert probe(c).stdout.splitlines()[0] == str(c)  # current directory
    assert probe(c, env={"JOBSEARCH_HOME": str(b)}).stdout.splitlines()[0] == str(b)  # environment
    assert probe(c, "--workspace", str(a), env={"JOBSEARCH_HOME": str(b)}).stdout.splitlines()[0] == str(a)  # argument


def test_equals_form_and_argument_is_removed_for_the_script(two):
    a, _, c = two
    out = probe(c, "--workspace=" + str(a), "--other", "x").stdout.splitlines()
    assert out[0] == str(a)
    assert out[1] == "['--other', 'x']"  # argparse in the real scripts never sees --workspace


def test_home_directory_and_relative_paths_are_expanded(two):
    a, _, c = two
    sub = a / "inner"
    sub.mkdir()
    assert probe(a, "--workspace", "inner").stdout.splitlines()[0] == str(sub)


def test_missing_folder_is_a_clear_error(two):
    _, _, c = two
    proc = probe(c, "--workspace", str(c / "nope"))
    assert proc.returncode != 0
    assert "Workspace folder not found" in proc.stderr


def test_argument_without_value_is_a_clear_error(two):
    _, _, c = two
    proc = probe(c, "--workspace")
    assert proc.returncode != 0
    assert "needs a path" in proc.stderr


def test_env_file_is_loaded_but_never_overrides_the_environment(two):
    a, _, c = two
    (a / ".env").write_text("FROM_DOTENV=from-file\n", encoding="utf-8")
    assert probe(c, "--workspace", str(a)).stdout.splitlines()[2] == "from-file"
    assert probe(c, "--workspace", str(a), env={"FROM_DOTENV": "from-env"}).stdout.splitlines()[2] == "from-env"
