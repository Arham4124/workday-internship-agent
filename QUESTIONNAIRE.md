# Questionnaire — everything the agent needs to know about you

Answer these once. Your answers go into three private, git-ignored files:
`config/profile.yaml` (facts about you), `config/answers.yaml` (standard answers to form questions) and
`config/settings.yaml` (what to apply to and how). The fastest way is to answer in a text file, then paste it
into Claude Code with the prompt at the bottom — Claude fills the files, you check them.

**Rules**
- **Never include passwords** in your answers or in a chat. Passwords go through SECRETS.md only.
- Answer truthfully; every application certifies that its information is true.
- "Decline to self-identify" is always an acceptable answer to the voluntary questions in section I.
- Anything you leave blank makes the agent stop and ask you when a form requires it (`./agent.sh answer`).

---

## A. Identity and contact → `profile.yaml: name, contact, address, links`

1. Legal first name, middle name (if any), last name.
2. Preferred first name (if different).
3. Full name as you sign it.
4. Email address for all applications — **must be Gmail** (verification e-mails are read from it).
5. Phone number (digits), phone type (Mobile / Home / Work), country code.
6. Home address: street, apt/unit, city, state, ZIP, county, country.
7. LinkedIn URL, GitHub URL, personal website/portfolio, link to your best paper (all optional).

## B. Work authorization → `profile.yaml: work_authorization`, `answers.yaml`

8. Are you a U.S. citizen? If not: permanent resident, F-1 (CPT/OPT/STEM OPT), H-1B, other?
9. Are you legally authorized to work in the U.S. (for the internship dates)?
10. Will you now or in the future require visa sponsorship (e.g. H-1B)?
11. Are you a "U.S. person" for export-control (ITAR/EAR) questions? (citizens and permanent residents are)
12. Do you hold a security clearance? Level, active or inactive?

## C. Education → `profile.yaml: education, gpa_policy`, `answers.yaml`

For **each** degree (current one first):

13. School's official name — and any other true names its list might use (joint schools, old names).
14. Degree (e.g. Ph.D., M.S., B.S.) and level (Doctorate / Masters / Bachelors).
15. Major / field of study as on your transcript — and acceptable close alternatives if a form's list lacks it.
16. Start month/year and end (or expected) month/year; completed or in progress.
17. GPA: report it, or keep it private? If private, how should "Is your GPA at least X?" questions be answered
    (e.g. "Yes for any cutoff of 3.5 or lower")?

Also:

18. Are you currently enrolled? Will you return to school after the internship?
19. Class standing if asked (rising sophomore / junior / senior / graduate student).

## D. Experience → `profile.yaml: experience, previous_employers, max_experience_entries_on_forms`

For each job / research position / internship (most relevant first):

20. Title, organization, city/state, start and end month/year (or current).
21. Two to four factual sentences: what you did, tools, results (copy from your resume).

Also:

22. How many entries should forms get (the resume covers the rest)? Default 4.
23. Every organization you've ever worked for (for "Have you worked for us before?").

## E. Skills, publications, languages, summary → `profile.yaml`

24. Technical skills and tools (grouped any way you like).
25. Publications / preprints (status: published, under review, preprint).
26. Languages and fluency.
27. A 1–3 sentence research/work summary (Claude uses it to rank postings and write short answers).

## F. Availability and logistics → `profile.yaml: availability`, `answers.yaml`

28. Which internship season(s)? (e.g. Summer 2027)
29. Earliest start date and latest end date.
30. Hours per week (full-time 40?) and whether part-time works during the semester.
31. Willing to relocate? Remote / hybrid / on-site all OK?
32. Location preferences — and how to answer "which locations are you open to" or "rank your locations".
33. Do you have a valid driver's license? (some forms ask)

## G. Compensation → `profile.yaml: compensation`

34. When a form asks for desired pay: match the posted range? Default hourly rate if none is posted?
    (Annual = hourly × 2080 if a yearly figure is required.)

## H. Legal and background questions → `answers.yaml`

35. Are you bound by a non-compete, non-solicitation, or other agreement limiting where you can work?
36. Will you have any other job or outside business during the internship (e.g. an assistantship that continues)?
37. Are you, or were you, a government employee or official (public universities and state agencies can count)?
38. Do you have relatives or close relationships with employees of companies you may apply to?
39. Do you have a close friend or relative who is a government official?
40. Have you ever been convicted of a felony or crime (other than minor traffic violations)?
41. Have you ever been debarred, suspended or excluded by the FDA or a government health program?
42. Have you ever been terminated or asked to resign?
43. Willing to undergo a background check and drug screening?
44. Able to perform the essential functions of the job with or without reasonable accommodation?
45. Any competing offers or offer deadlines?
46. Are you the spouse/partner of someone serving (or who served) in the U.S. military?
47. Are you at least 18?

## I. Voluntary self-identification (EEO) → `profile.yaml: self_identification`

Each question allows "Decline to self-identify".

48. Gender.
49. Hispanic or Latino? (Yes / No / Decline)
50. Race (American Indian or Alaska Native, Asian, Black or African American, Native Hawaiian or Other Pacific
    Islander, White, Two or More Races, or Decline).
51. Veteran status (not a protected veteran / protected veteran / decline).
52. Disability status (form CC-305): "Yes, I have a disability (or previously had one)", "No, I do not have a
    disability and have not had one in the past", or "I do not want to answer".
53. Sexual orientation, transgender identity, pronouns (some companies ask).

## J. Documents → `documents/`, `settings.yaml: documents`

54. Path to your resume PDF (it is copied to `documents/resume.pdf`).
55. Unofficial transcript PDF, if forms should get one when required (`documents/transcript.pdf`).
    Note: transcripts usually show your grades and date of birth.
56. Cover letters: only when a form requires one, always when there's a slot, or never?

## K. What to apply to → `settings.yaml: targeting`, `config/companies.yaml`

57. Role types you want (e.g. Data Science, ML research, Software Engineering, Quant, Product).
58. Roles you never want (e.g. statistics-only roles, sales, undergraduate-only, full-time).
59. Title words that should disqualify a posting (e.g. "biostat", "marketing").
60. Industries to prioritise (pharma, tech, finance, medtech, health, energy, aerospace, media, industrial) —
    and strictly (only those while any remain) or just first?
61. Companies to add (their Workday URLs) or skip.
62. Minimum fit score to apply (0–100; default 60) and, optionally, your own scoring rubric
    (what counts as a 90 vs a 60 for you).
63. Degree level the roles must accept (e.g. PhD students) and your expected graduation date
    (used to skip "must graduate by …" postings) — taken from your education entries.

## L. Volume and behaviour → `settings.yaml`

64. Maximum applications per day (default 10; each uses ~18 Claude calls).
65. Maximum per company per season (default 3; priority industries 5). Do your own past applications count?
    (They do — the agent detects "already applied".)
66. Review first (`mode: review`) or submit automatically (`mode: auto`)?
67. Daily run time (default 10:00).
68. Notifications: macOS notifications? daily e-mail summary? what hour?
69. Which Claude plan do you have (Pro / Max)? On Pro, keep the daily count modest.

## M. Accounts → SECRETS.md (do not write passwords anywhere else)

70. Do you already have Workday accounts at some companies with this e-mail? With one common password?
    (If so, you'll type that password into the private secrets file.)
71. For new Workday accounts: a unique random password per company (default, stored in Keychain) or your
    common password?
72. Have you created the Gmail app password (SECRETS.md, step 1)?

---

## Prompt for Claude Code (copy everything below, paste your answers where marked)

Run `./agent.sh init` first, then open Claude Code in this folder and paste:

```text
I'm setting up the Workday internship agent in this folder. Using ONLY the answers below and my resume at
<PATH TO MY RESUME PDF>, fill in config/profile.yaml, config/answers.yaml and config/settings.yaml.

Rules:
- Keep the structure and keys of each file exactly as in the .example.yaml templates.
- Do not invent or embellish anything. If an answer is missing, leave the field "" / null
  (in answers.yaml leave the answer "" so the agent asks me later).
- Dates: MM/YYYY for months, MM/DD/YYYY for days. Phone digits only in `phone`.
- Copy my resume into documents/resume.pdf (and my transcript into documents/transcript.pdf if I give one).
- Do NOT ask for, read, or write any password. Passwords are handled separately via SECRETS.md.
- When done, show me a short summary of what you filled and list every field you left blank.

My answers to QUESTIONNAIRE.md:
<PASTE YOUR NUMBERED ANSWERS HERE>
```

After Claude is done: read `config/profile.yaml` and `config/answers.yaml` yourself, then follow SECRETS.md and
the "First run, supervised" step in the README.
