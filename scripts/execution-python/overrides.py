"""Shared loader for workspace text overrides (prompts.yaml, texts.yaml)."""

import sys
from pathlib import Path

import yaml

import workspace  # noqa: F401  (sets the working directory to the workspace; keep before any relative path)


def load_overrides(path: Path, defaults: dict) -> dict:
    """defaults with the workspace file applied. Unknown names and empty or non-text values stop with a message."""
    texts = dict(defaults)
    if path.exists():
        with path.open(encoding="utf-8") as f:
            override = yaml.safe_load(f) or {}
        if not isinstance(override, dict):
            sys.exit(f"{path}: expected a mapping of names to text.")
        unknown = sorted(set(override) - set(defaults))
        if unknown:
            sys.exit(f"{path}: unknown names: {', '.join(unknown)}. Valid names: {', '.join(defaults)}.")
        bad = sorted(k for k, v in override.items() if not isinstance(v, str) or v == "")
        if bad:
            sys.exit(f"{path}: these must be text (not empty): {', '.join(bad)}.")
        texts.update(override)
    return texts


def fill(template: str, **values) -> str:
    """Fill {name} placeholders by plain replacement, so other braces in the text are left alone."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template
