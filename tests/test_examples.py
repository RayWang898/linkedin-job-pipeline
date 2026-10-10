"""The shipped examples must stay in step with the code, and every script must at least start."""

import re

import pytest
import yaml

import prompts  # noqa: E402  (conftest puts the scripts folder on sys.path)
import texts  # noqa: E402
from conftest import ENGINE, SCRIPTS

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def placeholders(text):
    return set(PLACEHOLDER.findall(text)) - {"language"}  # {language} only appears in the English defaults


@pytest.mark.parametrize("module,folder", [(prompts, "prompts.example"), (texts, "texts.example")])
def test_the_chinese_example_has_exactly_the_built_in_keys(module, folder):
    example = yaml.safe_load((ENGINE / folder / "zh-TW.yaml").read_text(encoding="utf-8"))
    assert set(example) == set(module.DEFAULTS), (set(example) ^ set(module.DEFAULTS))


@pytest.mark.parametrize("module,folder", [(prompts, "prompts.example"), (texts, "texts.example")])
def test_the_chinese_example_uses_the_same_placeholders(module, folder):
    example = yaml.safe_load((ENGINE / folder / "zh-TW.yaml").read_text(encoding="utf-8"))
    for key, text in example.items():
        assert isinstance(text, str) and text != "", key  # a separator may be only whitespace
        assert placeholders(text) == placeholders(module.DEFAULTS[key]), key


def test_every_built_in_text_is_a_non_empty_string():
    for module in (prompts, texts):
        for key, text in module.DEFAULTS.items():
            assert isinstance(text, str) and text, key


def test_built_in_text_has_no_cjk():
    cjk = re.compile(r"[一-鿿　-〿＀-￯]")
    for module in (prompts, texts):
        assert [k for k, v in module.DEFAULTS.items() if cjk.search(v)] == []


def test_the_prompt_text_names_the_tools_the_code_defines():
    assert "submit_triage" in prompts.DEFAULTS["rubric"] and "submit_detail" in prompts.DEFAULTS["detail_instructions"]
    assert "submit_summary" in prompts.DEFAULTS["summary_instructions"]


def test_every_example_profile_named_by_the_example_config_exists():
    config = yaml.safe_load((ENGINE / "config.example.yaml").read_text(encoding="utf-8"))
    for track in config["tracks"]:
        assert (ENGINE / "profiles.example" / track["profile"]).exists()
    assert (ENGINE / "profiles.example" / "shared.md").exists()
    assert "Job types worth a custom resume" in (ENGINE / "profiles.example" / "shared.md").read_text(encoding="utf-8")  # the prompt refers to it


@pytest.mark.parametrize("script", sorted(p.name for p in SCRIPTS.glob("*.py") if p.name not in ("sitecustomize.py",)))
def test_every_script_starts_and_prints_help(ws, script):
    if script in ("workspace.py", "overrides.py", "prompts.py", "texts.py"):
        pytest.skip("a library, not a command")
    args = ["--help"] if script == "init.py" else ["--help"]
    proc = ws.run(script, *args, workspace_arg=script != "init.py")
    assert proc.returncode == 0 and "usage:" in proc.stdout, proc.all_output
