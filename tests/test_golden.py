"""Golden files: the exact text of a few notes and of the daily summary, in English and in Traditional Chinese.

They pin the wording and layout users read, so an accidental change to a label, a heading or a table shows up as a diff.
After a change you meant to make, regenerate them with:  UPDATE_GOLDEN=1 python -m pytest tests/test_golden.py
and read the diff in git before committing.
"""

import os

from conftest import FIXTURES

DAY = "2026-10-09"
GOLDEN = FIXTURES / "golden"
PICKS = {"top-marketing": "Marketing-Associate", "needs-custom": "Immersive", "same-company-in-tracker": "Omega-Co"}

# the same entries as fixtures/tracker.txt, with the words a Chinese-speaking user would write
ZH_TRACKER = ("\u5df2\u6295\u905e\uff1aMarketing Coordinator \u00b7 Tracked Co | Brand Manager \u00b7 Omega Co\n"
              "\u5df2\u8a55\u4f30\u4e0d\u6295\uff1aAccount Manager \u00b7 Sigma Co\n")


def check(name, text):
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"no golden file {name}; create it with UPDATE_GOLDEN=1"
    assert text == path.read_text(encoding="utf-8"), f"{name} differs from the golden file (UPDATE_GOLDEN=1 regenerates it)"


def compare_day(ws, language):
    assert ws.daily().returncode == 0
    check(f"{language}/_summary.md", ws.read(f"data/7_shortlist/{DAY}/_summary.md"))
    for label, part in PICKS.items():
        folder = next(f for f in ws.shortlist(DAY) if part in f)
        check(f"{language}/note-{label}.md", ws.note(DAY, folder))


def test_english_notes_and_summary(ws):
    ws.use_tracker()
    compare_day(ws, "en")


def test_traditional_chinese_notes_and_summary(make_ws):
    ws = make_ws("zh", language="zh-TW")
    ws.write("tracker.txt", ZH_TRACKER.replace(":", "：", 2) if False else ZH_TRACKER)
    ws.update_config({"tracker_path": "tracker.txt", "tracker_labels": {"applied": "已投遞", "evaluated_skip": "已評估不投"}})
    compare_day(ws, "zh-TW")
