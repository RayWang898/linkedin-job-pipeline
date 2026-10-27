"""
Create a workspace: the folder that holds your config, profiles, keys and data, apart from the engine.

Usage:
  venv/bin/python scripts/execution-python/init.py ~/jobsearch
  venv/bin/python scripts/execution-python/init.py ~/jobsearch --language zh-TW   # also copy the Traditional Chinese prompts.yaml and texts.yaml

Copies config.example.yaml and .env.example into the folder and creates empty profiles/ and resumes/ (never over a file that is already there).
The example profiles stay in the engine as templates: example text left in a workspace looks like finished work, so the user's own profiles are written from them, not copied.
Also adds a .gitignore that keeps your keys and data out of git, and prints what to do next. Makes no network calls.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
COPIES = (("config.example.yaml", "config.yaml"), (".env.example", ".env"))
GITIGNORE = """\
# Created by init.py: keep keys and personal data out of git
.env
config.yaml
profiles/
resumes/
data/
logs/
feedback/
*.db
*.db.bak
"""


def copy_in(src: Path, dst: Path) -> bool:
    """Copy a file or folder to dst unless dst already exists. Returns True if it copied."""
    if dst.exists():
        return False
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", nargs="?", default=".", help="folder to create or fill (default: the current folder)")
    parser.add_argument("--language", metavar="CODE", help="also copy prompts.example/CODE.yaml and texts.example/CODE.yaml (for example zh-TW)")
    args = parser.parse_args()

    target = Path(args.workspace).expanduser().resolve()
    if target.exists() and not target.is_dir():
        sys.exit(f"{target} exists and is not a folder.")
    for src_name, _ in COPIES:
        if not (ENGINE / src_name).exists():
            sys.exit(f"Engine file missing: {ENGINE / src_name}. Run this script from a complete clone of the repository.")

    extra = []
    if args.language:
        for kind in ("prompts", "texts"):
            src = ENGINE / f"{kind}.example" / f"{args.language}.yaml"
            if not src.exists():
                available = sorted(p.stem for p in (ENGINE / f"{kind}.example").glob("*.yaml"))
                sys.exit(f"No {kind} example for '{args.language}'. Available: {', '.join(available) or 'none'}.")
            extra.append((src, f"{kind}.yaml"))

    target.mkdir(parents=True, exist_ok=True)
    created, kept = [], []
    for src_name, dst_name in COPIES:
        (created if copy_in(ENGINE / src_name, target / dst_name) else kept).append(dst_name)
    for src, dst_name in extra:
        (created if copy_in(src, target / dst_name) else kept).append(dst_name)
    for folder in ("profiles", "resumes"):
        (target / folder).mkdir(exist_ok=True)
    gitignore = target / ".gitignore"
    if gitignore.exists():
        kept.append(".gitignore")
    else:
        gitignore.write_text(GITIGNORE, encoding="utf-8")
        created.append(".gitignore")
    if ".env" in created:
        os.chmod(target / ".env", 0o600)  # holds your API keys: readable by you only

    print(f"Workspace: {target}")
    for name in created:
        print(f"  created  {name}")
    for name in kept:
        print(f"  kept     {name} (already there, not touched)")
    if ENGINE in target.parents or target == ENGINE:
        print("Note: this folder is inside the engine repository. Keys, config, profiles, resumes, data and databases are ignored by git, but your own "
              "files (prompts.yaml, texts.yaml, an applied-jobs list, notes) are not. Keep the workspace outside the repository, or add files "
              "by name when you commit and never use git add -A.")
    python = sys.executable
    dry = f'"{python}" "{ENGINE}/scripts/execution-python/run_daily.py" --workspace "{target}" --dry-run'
    print(f"""
Next:
  1. Put your keys in {target / '.env'} (APIFY_TOKEN and ANTHROPIC_API_KEY; with scoring.provider "claude-code" no ANTHROPIC_API_KEY).
  2. Write your profiles in {target / 'profiles'} (use {ENGINE / 'profiles.example'} as the template) and your tracks, queries and filters in {target / 'config.yaml'}.
     config.yaml lists two example tracks until you replace them; --dry-run reports their missing profile files.
     In Claude Code, run /setup from the engine folder and it will interview you and write them for you.
  3. Check everything without spending anything:
       {dry}""")
    if args.language and args.language.lower().startswith("zh"):
        print("  Tracker files that start lines with Chinese words need tracker_labels in config.yaml (see config.example.yaml).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
