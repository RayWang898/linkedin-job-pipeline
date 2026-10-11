# 0005. The model's visa judgment is a warning, not an exclusion

With `visa.enabled`, the triage call returned `visa_blocked` next to the scores, and the program capped every score at 1 when it was true. A job capped to 1 falls below the shortlist threshold and is mentioned only in one list in the daily summary. In a real run the model returned `visa_blocked: true` for a posting with no visa-related text at all (its own reason said "visa sponsorship not mentioned"), and the best New York match of the day disappeared from the shortlist.

Decision: excluding a job for sponsorship belongs to the deterministic filter (`visa.exclude_patterns`, step 4): its drops are auditable in `data/5_excluded` and never come from a guess. The model's `visa_blocked` stays in the triage call, but it only produces a warning line in the job's note and a list in the daily summary. It never changes a score or `needs_custom`. A flag with no visa-related text in the posting is also dropped before it is stored (`drop_unsupported_visa_block`).

Trade-off: a posting that really refuses sponsorship in words the filter does not know can now reach the shortlist, with a warning. We accept that: a wrong inclusion costs the user a minute of reading, a wrong exclusion costs a missed job and is invisible. Users add phrases to `visa.exclude_patterns` when they see a warning that should have been a drop.

The field keeps the name `visa_blocked` so that stored triage results and the tool schema stay compatible; it now means "the model thinks the posting refuses sponsorship". Scores cached before this change keep their old capped values (their raw scores are in `scores_raw`) until the day is scored again.

Not checked here: the rubric still says the candidate is on OPT. That wording does not fit every user and is a separate prompt change that needs `replay_triage.py`.
