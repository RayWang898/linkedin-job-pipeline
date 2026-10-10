# What a good profile looks like

Profiles are sent to the scoring model with every job, so each line is read thousands of times. Short and specific beats long and general. Aim for roughly one page for `shared.md` and half a page per track file.

## Principles

1. **Say the level in one sentence.** Name the titles that fit and the ones that do not, and give the years of experience. The scoring prompt asks the model to judge "level and seniority"; this sentence is what it measures against. Hard filters (`exclude_title_patterns`, `min_years_exclude`) catch most senior roles, but the ones that slip through are scored down using this sentence.
2. **Facts with numbers.** Employer, title, place, dates, what you did, and one number each (people, budget, reach, events, users, results). "Managed a $250k campaign budget across 5 agencies" scores better than "experienced in campaign management". Never invent a number; leave it out.
3. **Be honest about gaps.** Every track file has a Gaps section: what these jobs usually require that the user lacks. The model uses it to stop itself from over-scoring. A profile with no gaps produces a shortlist full of 4s that the user then rejects.
4. **Red lines as actions.** "Lower the score for pure sales or lead-generation roles" works; "I dislike sales" gives the model nothing to do.
5. **Share, then differ.** Put what applies to every track in `shared.md`, and in each track file only what is different: the positioning, the titles, what to lead with, the gaps and red lines of that direction. Repeating the same text in several files costs money on every call.
6. **Mark what is not confirmed.** If you inferred a fact (from a resume line, or from a guess about level), write `(unconfirmed)` next to it while drafting and clear every marker with the user before saving. A confident wrong fact is worse than a missing one.
7. **No contact details.** No phone, email, address or links to private accounts. The profile goes to a third-party API on every call.
8. **Language.** Write in the language the user is most precise in. Job postings are mostly English; a profile in another language works, but the model reads English most reliably, so for important terms (titles, tools) keep the English words.

## Checklist before saving

For `shared.md`:
- [ ] A level sentence that names fitting and non-fitting levels
- [ ] Each experience entry has employer or context, title, place, dates and at least one concrete fact
- [ ] Numbers appear only where the user gave them
- [ ] Skills are things the user can defend in an interview
- [ ] The section "Job types worth a custom resume" exists (even if it says "none")
- [ ] Hard rules are written as actions
- [ ] No contact details
- [ ] No `(unconfirmed)` markers left

For each track file:
- [ ] Positioning in one or two sentences
- [ ] Target titles use the words job boards use
- [ ] Strengths are chosen for this track, not a copy of `shared.md`
- [ ] At least two honest gaps
- [ ] The file name matches `profile:` in the `tracks:` entry, and exists in `profiles/`

Across files:
- [ ] Nothing contradicts anything else (levels, locations, work authorization)
- [ ] Total length is reasonable (if `shared.md` runs past two pages, cut the oldest or least relevant entries)

## Patterns that work

These examples are invented. Use the user's own facts.

A level line:
> Recent graduate with a one-year internship. Fits entry-level Analyst, Coordinator and Associate roles. Not Senior, Lead or Manager, and not roles that ask for three or more years.

An experience entry:
> **Operations Coordinator, Company X, City (2021.03 - 2023.08)**
> - Scheduled and tracked about 30 weekly deliveries across 3 warehouses with a spreadsheet and a routing tool.
> - Cut late deliveries from 12% to 4% by changing the pickup order and agreeing cut-off times with two carriers.
> - Trained 5 new coordinators.

A gap and a red line in a track file:
> **Gaps**: No hands-on experience with an ERP system; has not managed a team.
> **Red lines**: Lower the score for roles that are mainly night-shift floor work.
