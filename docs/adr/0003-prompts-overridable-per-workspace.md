# Prompts live in one module and a workspace can override them

All text the scoring models see (instructions, tool descriptions, request lines, headings) is in `prompts.py` with English defaults. A workspace may add a `prompts.yaml` that replaces any of them, and `scoring.output_language` sets the language of the model's answers.

Why: the maintainer's scoring was tuned and checked with Chinese prompts, and the public tool should default to English. Forcing one language on everyone would either change the maintainer's scoring or ship Chinese defaults. With an override, the maintainer's workspace loads the Chinese set (`prompts.example/zh-TW.yaml`, extracted unchanged from the old code) and produces byte-identical prompts and cache hashes, so nothing is re-scored. Other users can supply any language.

Measured while doing this (100 jobs, same seed, Haiku): running the same Chinese prompt twice agreed on the best score for 84% of jobs, so that is the noise floor. The English default agreed with the Chinese run for 68% to 71%, and kept 68% of the jobs that were previously at 4 or above (Chinese rerun: 72%, three of the lost ones were intended visa caps). English is a little stricter, mostly about years of experience. We did not measure which is more accurate; that needs labeled data (applied / skipped jobs).

Consequences:
- Changing a prompt invalidates the cached scores (hashes include the text), as before.
- Compare prompt changes against a rerun of the old prompts, never against stored scores: stored scores predate features and come from a sample picked for being high, which biases the comparison.
- Display text (note labels, the daily summary file, the phone notification) follows the same pattern in `texts.py` and an optional `texts.yaml`, kept separate because it never affects scoring or the cache. With `texts.example/zh-TW.yaml` loaded, notes, the summary file and the notification text match the old Chinese output byte for byte (checked on 339 scored jobs). Existing Chinese notes stay readable: the program only reads English front-matter keys, `## SUMMARY`, `interest` and `user note`, and a rerun still recognizes the old Chinese usage lines in a summary file.
- The words that start a tracker line (`Applied`, `Evaluated, skipped`) are `tracker_labels` in config.yaml, English by default.
- Console output, error messages and the details inside failure notifications are English only.
