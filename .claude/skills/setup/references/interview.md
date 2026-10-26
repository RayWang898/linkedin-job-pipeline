# Interview guide

Questions for each part, why they matter, and where the answer goes. Ask in the user's language, a few at a time. The interview is resume-led: ask A0 first, draft from the resume, and ask only about what is missing or unclear. Questions a resume cannot answer (A1, A8, and the options in part D) are asked either way.

## Part A: background (becomes `profiles/shared.md`)

The example file has these headings: Background, Shared experience, Skills, Job types worth a custom resume, Hard rules. Keep them (the scoring prompt refers to "Job types worth a custom resume").

**A0. Resume first** (ask this before anything else in part A)
- "Do you have a resume, portfolio or profile page I can read? Paste it here, or give me a file path you agree to let me read." Read it only after they agree.
- From it, draft A2 (level, from years and last title), A3, A4 and A6 yourself. Show the draft and ask what is wrong, what is missing, and for the numbers the resume leaves out ("You led the referral program: how many users did it bring in?"). Do not re-ask what the resume already says.
- If they have no resume, say so and ask A2 to A6 as written below, three questions at a time.

**A1. Where and under what status** (Background; also `location_whitelist`, `visa`)
- Which city or cities would you work in, and is remote acceptable?
- Are you authorized to work there without sponsorship? If a visa is involved, what kind, and will you need an employer to sponsor it later? (This decides whether to turn on `visa.enabled`.)

**A2. Level** (the first line of Background; the most useful single line in the file)
- How many years of full-time work do you have, and what was your last title? Are you changing careers or coming back from study?
- Which job levels do you want, and which would be a stretch or a step down? Turn the answer into one sentence that names titles on both sides, for example: "Recent graduate with a one-year internship. Fits entry-level Analyst, Coordinator and Associate roles. Not Senior, Lead or Manager, or anything that asks for three or more years."

**A3. Education and languages** (Background)
- Degrees with field and year; spoken languages and working language.

**A4. Experience that goes on any resume** (Shared experience)
With a resume, take the three to six most relevant roles from it and ask only for missing numbers and results. Without one, for each of the three to six most relevant roles or projects ask:
- Employer or context, your title, place, dates.
- What did you do that you would put on a resume? Then ask for a number for each: how many people, how much budget, how many users, events, articles, sales, hours saved.
- Was there a result you are proud of?
Write one bullet per fact, with the number. If the user cannot give a number, write the fact without one; never estimate for them.

**A5. Projects not on any resume** (optional, keep under Shared experience or a short section)
- Anything relevant that you would mention in an interview but is not on a resume (side projects, competitions, volunteering)?

**A6. Skills you can defend** (Skills)
- Tools, software, languages and methods you could talk about for ten minutes. Group them (for example tools, analysis, languages). List only what they can back up.

**A7. Job types worth a custom resume** (that section)
Skip this by default and write `none` under the heading. Ask it only if the user says they would apply to a kind of job that their resume does not cover (for example a career change, or a second field they could write a separate resume for). In that case ask: "Which kinds of jobs would you apply to if you wrote a custom resume for them?"
- This is not the same as a track. A direction the user wants and has a resume for is a track (part B). A type listed here is for jobs worth applying to only with a rewritten resume: when no track scores such a job high, it is flagged `needs custom resume` and listed separately, instead of being dropped.
- Never propose types yourself, and do not turn guesses from the resume into types. Write only what the user names. Zero to four is normal; none is the usual answer for someone with one resume and one direction.

**A8. Hard rules** (Hard rules)
- What would make a job a bad fit no matter what the title says? (frequent travel, on-call, commission-only, a specific industry, a skill you do not want to use). Write each as an action the model can take: "lower the score for ...".

Never ask for, or write down, a phone number, email or home address.

## Part B: tracks (becomes `profiles/<file>.md` and `tracks:`)

Start with: "What kinds of jobs are you looking for? Name the directions, even roughly." Help them cut it to two to four. Signs that two directions should be one track: same titles, same resume. Signs they should be two: different resume, different strengths to lead with. One track is fine.

For each track, draft from what you already know and ask only for the gaps:

**B1. Positioning**: one or two sentences: what role they aim for and why they fit. 
**B2. Target titles**: a comma-separated list of the real titles on job boards (this also feeds the search queries in part C).
**B3. Strengths**: three to six bullets taken from the shared experience, selected and worded for this track. Do not repeat everything in `shared.md`; say what to lead with for this direction.
**B4. Gaps**: ask "What would a hiring manager worry about for this kind of job?" and "What do these jobs usually ask for that you do not have?" Write them plainly. The scoring model uses this list to avoid giving a high score where the user is weak, so softening it hurts them.
**B5. Red lines**: the track-specific no's, written as actions ("lower the score for ...").
**B6. Resume**: which resume file the user would send for this track (the file name; a placeholder is fine). Many tracks can share a resume.

Then record the track in `config.yaml`:

```yaml
tracks:
  - id: A                      # one capital letter, unique
    name: Marketing            # shown in notes
    tag: Marketing             # becomes the note tag track/A-Marketing
    profile: a_marketing.md    # file in profiles/
    resume: Resume_Marketing.docx
```

## Part C: search and filters

Follow `queries-and-filters.md`. Inputs to collect: locations, the titles from B2, industries or contexts they care about or avoid, seniority, salary or other deal-breakers the filters can express.

## Part D: options

- **Visa**: only if A1 said they need sponsorship. Offer `visa.enabled: true`; the default phrases in the example are a good start.
- **Triage flags**: offer `triage_flags.enabled: true` if B or C turned up dislikes the filters cannot express (a field they avoid, postings that ask for more years deep in the text, vague or agency postings). Write what they avoid under "Not interested" and what they are drawn to under "Interested" in `shared.md`. Leave it off when unsure; it can be turned on after a few days of shortlists.
- **Already-applied list**: if they keep a list of jobs they applied to or rejected, explain the `tracker_path` format from `config.example.yaml` and offer to convert it.
- **Language**: the language they want the reasons, advice and summaries in (`scoring.output_language`). The instructions the model sees stay English unless they supply `prompts.yaml` (see the README).
- **Notifications**: whether they want a phone notification (`NTFY_TOPIC`, optional).
- **Volume and cost**: how many jobs a day they can actually read. A shortlist of 10 to 30 a day is a lot; offer `scoring.shortlist_size` and `scoring.shortlist_min_score` accordingly.

## After the interview

List what you left as a default and what is still `(unconfirmed)`. Offer to revisit after the first test run, because the first shortlist usually shows what the profiles are missing.
