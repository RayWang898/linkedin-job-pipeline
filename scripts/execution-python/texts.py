"""Display text written to the shortlist notes, the daily summary file and the phone notification.

The defaults are English. A workspace can replace any of them with a `texts.yaml` file whose keys are the
names in DEFAULTS (see texts.example/ for a Traditional Chinese example). Keys it leaves out keep the default.
`{name}`-style placeholders are filled in by the program; keep them in your text. Unlike prompts.yaml, these
texts do not affect scoring or the score cache. Console output and error messages are English only.
"""

from pathlib import Path

from overrides import fill, load_overrides

TEXTS_PATH = Path("texts.yaml")

DEFAULTS = {
    # --- shortlist note (one .md per job) ---
    "scores_separator": "  ",
    "note_best": "- Best: {track} {name} {score}  All tracks: {scores}",
    "note_link": "- Link: [LinkedIn posting]({url})",
    "note_no_link": "- Link: none",
    "note_reason": "- Triage reason: {reason}",
    "note_same_company": "- Earlier at this company: {title} ({label})",
    "note_detail_heading": "## Detailed review",
    "note_detail_resume": "- Suggested resume: {track} {name} ({resume})",
    "note_detail_emphasize": "- Emphasize: {items}",
    "note_detail_gaps": "- Gaps: {items}",
    "list_separator": "; ",
    "note_description_heading": "## Original job description",
    "note_no_description": "(no description)",
    "salary_undisclosed": "Not disclosed",
    "applicants_unknown": "Unknown",
    # --- daily summary file (_summary.md) ---
    "summary_title": "# Daily shortlist summary {date}",
    "summary_shortlist": "- Shortlist: main {main}, needs custom resume {custom}",
    "summary_visa_blocked": "- Triage flagged as visa-blocked (scores capped at 1): {n}",
    "summary_visa_item": "  - {company} | {title}: {reason}",
    "summary_tracks_heading": "## Jobs per track",
    "summary_queries_heading": "## Jobs per search query",
    "stage_fetched": "Fetched (slimmed)",
    "stage_deduped": "After dedup",
    "stage_filtered": "After filter",
    "stat_line": "{label}: {n}",
    "stat_excluded": "Step 4 excluded: {items}",
    "stat_excluded_item": "{reason} {n}",
    "stat_excluded_separator": ", ",
    "usage_triage": "Triage",
    "usage_detail": "Detailed review",
    "usage_summary": "Summary",
    "usage_prefix": "API usage: ",
    "usage_item": "- {label} {prefix}{usage}",
    "usage_line": "{calls} calls{retries}; input {input}, output {output}, cache write {cache_write}, cache read {cache_read} tokens",
    "usage_retries": "; {n} failed",
    # --- track table ---
    "table_stage": "Stage",
    "table_total": "Total",
    "table_triage_best": "Triage (best track)",
    "table_min3": "Track score >= 3",
    "table_min4": "Track score >= 4",
    "table_shortlist_best": "Shortlist (best track)",
    "table_detail_best": "Detailed review (best track)",
    "table_detail_resume": "Detailed review: suggested resume",
    # --- search-query table ---
    "query_table_query": "Search query",
    "query_table_unique": "Unique (only this query found it)",
    "query_stage_fetched": "Fetched",
    "query_stage_deduped": "After dedup",
    "query_stage_filtered": "After filter",
    "query_stage_triage4": "Triage >= 4",
    "query_stage_shortlist": "Shortlist",
    "query_table_all": "All (distinct)",
    # --- phone notification (run_daily.py) ---
    "notify_shortlist": "Shortlist {n}",
    "notify_custom": "needs custom {n}",
    "notify_tokens": "tokens {tokens}",
    "notify_tokens_value": "input {input}k / output {output}k",
    "notify_scoring_failures": "{n} scoring failures",
    "notify_fetch_incomplete": "fetch incomplete, missing {queries}",
    "notify_queries_separator": ", ",
    "notify_gap": "{n} days since last success",
    "notify_tracks_separator": " / ",
    "notify_separator": " | ",
    "notify_catch_up": "Caught up {date}: {message}",
    "notify_failed": "Daily run failed: {step} ({detail})",
    "notify_unexpected": "Daily run hit an unexpected error: {error}",
}


def load() -> dict:
    """DEFAULTS with the workspace's texts.yaml applied."""
    return load_overrides(TEXTS_PATH, DEFAULTS)


__all__ = ["DEFAULTS", "fill", "load"]
