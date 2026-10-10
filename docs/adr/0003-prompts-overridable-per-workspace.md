# Prompts live in one module and a workspace can override them

All text the scoring models see (instructions, tool descriptions, request lines, headings) is in `prompts.py` with English defaults. A workspace may add a `prompts.yaml` that replaces any of them, and `scoring.output_language` sets the language of the model's answers.

Why: the maintainer's scoring was tuned and checked with Chinese prompts, and the public tool should default to English. Forcing one language on everyone would either change the maintainer's scoring or ship Chinese defaults. With an override, the maintainer's workspace loads the Chinese set (`prompts.example/zh-TW.yaml`, extracted unchanged from the old code) and produces byte-identical prompts and cache hashes, so nothing is re-scored. Other users can supply any language.

Measured while doing this (100 jobs, same seed, Haiku): running the same Chinese prompt twice agreed on the best score for 84% of jobs, so that is the noise floor. The English default agreed with the Chinese run for 68% to 71%, and kept 68% of the jobs that were previously at 4 or above (Chinese rerun: 72%, three of the lost ones were intended visa caps). English is a little stricter, mostly about years of experience. We did not measure which is more accurate; that needs labeled data (applied / skipped jobs).

Consequences:
- Changing a prompt invalidates the cached scores (hashes include the text), as before.
- Compare prompt changes against a rerun of the old prompts, never against stored scores: stored scores predate features and come from a sample picked for being high, which biases the comparison.
- Static labels in the output notes (headings such as the detailed-review section) are not part of this yet. They still need an English default and the same override, and existing Chinese notes must keep working.
