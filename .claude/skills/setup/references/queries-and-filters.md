# Search queries and filters

Everything here goes into the user's `config.yaml`. Show the YAML before writing it. After any change, the cheap check is `score.py --help` (loads the config); the real check is the small test run.

## Search queries (`apify.queries` and `apify.query_names`)

Each query is a Boolean search string sent to LinkedIn through the Apify actor. One query is fetched once per location, so cost grows with queries x locations x `max_jobs_per_search`.

**Shape**: a group of title variants, optionally narrowed by a group of context words:

```
("marketing coordinator" OR "marketing associate" OR "marketing specialist") AND (agency OR brand OR consumer)
```

- Quote multi-word phrases. Use `OR` inside parentheses and `AND` between the two groups.
- The title group should use the words employers actually put in titles. Take them from the track's target titles, and add the common synonyms ("coordinator", "associate", "specialist" for early-career roles).
- The context group narrows to industries or settings the user wants; leave it out when the titles alone are specific enough (a rare title needs no narrowing).
- **Queries follow job themes, not tracks.** A track can need two queries and two tracks can share one. Name each query for its theme (`query_names`), in the same order. Labels A, B, C ... are assigned by order and are separate from track ids.
- Start with three to six queries. More queries mean more jobs fetched and scored. A query that returns mostly irrelevant jobs is worse than no query; a query with a very common title ("manager") returns noise.
- The cap per query is `apify.max_jobs_per_search` (default 50). Raise it for one broad query with `apify.max_jobs_by_query: {A: 150}` keyed by label, or per location with `max_jobs_by_location`.
- `apify.date_posted`: `past_24h` is right for a daily run; use `past_week` for the first run if the user wants a larger first batch. `fetch_details` must stay `true` (descriptions are needed for filtering and scoring).

**Check**: after the test run, look at the "Jobs per search query" table in `_summary.md`. A query with many fetched and none after the filter is too broad or aimed at the wrong titles; one with very few fetched needs more title variants.

## Locations

- `apify.locations`: one entry per place to search, for example `"Austin, TX"`. The first is the main one.
- `location_whitelist`: a job passes only if its location text contains one of these strings (case-insensitive). LinkedIn writes the same area in many ways, so list the variants: the city, its boroughs or districts, nearby cities the user would commute to, the metro-area wording ("... Metropolitan Area"), and `"Remote"` if remote is acceptable. Too short a list silently drops good jobs; check `data/5_excluded` after the first run for the reason `location`.
- `location_center_address` only prints how far an excluded job is from the center, so the user can decide whether to widen the whitelist. It does not filter.

## Hard filters

| Setting | Meaning | Guidance |
| --- | --- | --- |
| `max_applicants` | Drop jobs with more applicants than this | 50 to 100 for early-career roles. Lower means fewer, fresher jobs. It uses the exact count when LinkedIn shows one, the N of "fewer than N" or "over N", and lets a job through when no count is shown |
| `exclude_title_patterns` | Drop if the title contains one of these as a whole word | Seniority words: `Senior`, `Sr`, `Lead`, `Director`, `Head of`, `Principal`, `VP`, `Vice President`, `Chief`, `Manager` (only if even manager roles are out of reach). Add title words for fields the user does not want. Whole-word matching: `Lead` does not drop `Leader` |
| `min_years_exclude` | Drop if the description states at least this many years | `5` suits early career; a range like "3-5 years" counts its lower bound |
| `exclude_keywords` | Drop if the title or description contains any of these | Use sparingly; a substring can match unexpectedly |
| `exclude_staffing_agencies` and `staffing_agency_blacklist` | Drop postings by staffing firms (matched on the company name) | Start with the agencies the user has actually met |
| `exclude_unpaid_*` | Unpaid and volunteer roles | The example patterns are a good default |
| `visa` | Sponsorship phrases | Only when the user needs sponsorship (`visa.enabled: true`) |

Tell the user that these filters are blunt: they exist to remove the obvious, cheaply and without model cost. The scoring model handles judgment calls. When unsure, keep a filter loose and let `calibrate.py` show what it removed.

## Volume and the shortlist

- `scoring.shortlist_min_score` (default 4): the best track score a job needs to be shortlisted. The list is never padded with weaker jobs, so a quiet day gives a short list.
- `scoring.shortlist_size`: the most jobs on one day's shortlist.
- `scoring.detail_count`: how many top jobs get the longer, dearer advice pass.
- `daily.max_total_jobs`: a hard stop on jobs per day after merging, which also caps cost.

If the first test shows too many shortlisted jobs the user would skip, ask why they would skip each one and change the matching lever (a filter, a gap or red line in the profile, or the threshold), not the prompts.
