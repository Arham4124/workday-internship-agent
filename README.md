# Workday Internship Agent

An agent that finds internships on company **Workday** career sites, ranks them against your profile with
**Claude** (through your own Claude Code login), and fills in the applications for you — creating Workday
accounts, handling verification e-mails, answering each company's questions from your profile, and either
stopping at the Review page for you or submitting. It runs once a day in the background on your Mac.

> **Start in review mode.** Every application certifies that its information is true. The agent only uses
> facts you give it, pauses on anything it can't answer, and Claude re-checks the Review page before any
> Submit — but you are responsible for what is submitted in your name. Watch the first few runs.

---

## Contents

1. [Requirements](#requirements)
2. [How it works](#how-it-works)
3. [Setup, step by step](#setup-step-by-step)
4. [Customizing the agent](#customizing-the-agent)
5. [Everyday commands](#everyday-commands)
6. [Privacy and safety design](#privacy-and-safety-design)
7. [Limitations](#limitations)
8. [Troubleshooting](#troubleshooting)
9. [Files](#files)

Also read: **[QUESTIONNAIRE.md](QUESTIONNAIRE.md)** (everything the agent needs to know about you) and
**[SECRETS.md](SECRETS.md)** (Gmail app password and how passwords are kept away from Claude).

---

## Requirements

- **macOS** (uses the Keychain for passwords and `launchd` for the daily run)
- **Python 3.11+**
- **Claude Code** installed and logged in with a Claude subscription (Pro or Max) — `claude auth status`
  should show `"loggedIn": true`. No API key is needed; the agent calls the `claude` command.
- A **Gmail** address for applications, with **2-Step Verification** and an **app password** (see SECRETS.md)
- Your **resume as a PDF**; optionally an unofficial **transcript PDF**
- About 500 MB of disk for the bundled Chromium browser

## How it works

| Stage | What happens | Uses Claude? |
|---|---|---|
| **Search** | Asks every company in `config/companies.yaml` for internship postings through Workday's public job API. Keeps U.S. internship titles that match your role types. | No |
| **Rank** | Claude (Haiku) reads each new posting's description and scores 0–100 against your profile, seasons, locations and hard no's. Postings at or above `min_score` are queued. | Yes, cheap |
| **Pick** | Each day picks up to `max_applications_per_day`: already-approved first, then your priority industries, then best fit. Per-company limits and duplicate-role checks apply. | No |
| **Apply** | Opens the posting in a private Chromium, signs in (or creates the Workday account, reads the verification e-mail from Gmail, or resets a forgotten password), then fills each page. Claude (Sonnet) decides each field's value **only** from `profile.yaml` and `answers.yaml`; the code types it and checks it stuck. | Yes, ~18 calls per application |
| **Check & submit** | On Workday's Review page Claude re-checks everything against your profile. In `review` mode it stops there; in `auto` mode it submits if the check passes. Problems → held for you (or fixed automatically on the next run). | Yes |
| **Report** | `Dashboard.html`, a macOS notification per result, and a daily e-mail summary. Every value typed is logged in `data/runs/`. | No |

Questions it can't answer from your files (e.g. "Do you have a driver's license?") pause that one application;
answer them once with `./agent.sh answer` and the answer is reused on every future form.

## Setup, step by step

**1. Get the code and install**

```bash
git clone <your-repo-url> workday-internship-agent
cd workday-internship-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

**2. Create your private config files**

```bash
./agent.sh init
```

This copies the blank templates to `config/settings.yaml`, `config/profile.yaml` and `config/answers.yaml`
(all git-ignored; the last two are readable only by you).

**3. Fill in your information** — answer **[QUESTIONNAIRE.md](QUESTIONNAIRE.md)**. The easiest way: open
Claude Code in this folder and paste the prompt at the end of the questionnaire together with your answers;
Claude fills the three files for you. Then read them over — they are what gets submitted.

**4. Add your documents**

```
documents/resume.pdf        (required)
documents/transcript.pdf    (optional; only uploaded when a form requires one)
```

**5. Store your passwords privately** — follow **[SECRETS.md](SECRETS.md)**. In short:

```bash
./agent.sh secrets-template      # creates ~/.jobagent_secrets (only you can read it)
nano ~/.jobagent_secrets         # type the Gmail app password and your usual Workday password
./agent.sh import-secrets        # moves them into the macOS Keychain, tests Gmail, deletes the file
```

**6. First run, supervised**

```bash
./agent.sh discover                       # scan all career sites (no Claude usage)
./agent.sh triage                         # Claude ranks what was found
./agent.sh status                         # see the queue; open Dashboard.html
./agent.sh apply <job-id-fragment> --headed --mode review   # watch one application; stops at Review
```

Check what it entered in `data/runs/<date>_<company>/actions.jsonl` and the `review.png` screenshot.
Repeat for 2–3 companies. Fix anything wrong in your config files.

**7. Go automatic**

```bash
./agent.sh approve all       # submit the reviewed ones on the next run (optional)
./agent.sh mode auto         # submit automatically from now on (or keep "review")
./agent.sh install           # run once a day in the background (also runs right away)
```

The daily run happens at `schedule.daily_run_time`; if the Mac was asleep it runs on wake, and if it was off
it runs at the next login. It never runs more than once a day. It needs you logged in, and closing the lid
pauses it. `./agent.sh uninstall` removes it.

## Customizing the agent

Everything personal lives in three git-ignored files. Edit them any time; changes apply on the next run.

| I want to change… | Where | Example |
|---|---|---|
| **How many applications per day** | `settings.yaml → limits.max_applications_per_day` | `5` (also the main lever for Claude usage) |
| **Applications per company** | `limits.max_per_company_per_season`, `max_per_priority_company_per_season` | `3`, `5` |
| **Submit automatically or review first** | `mode` or `./agent.sh mode auto/review` | `review` |
| **Type of role** | `targeting.role_types`, `never_apply` | `["Software Engineering", "Backend"]` |
| **Title keywords** (non-data/AI fields) | `targeting.title_keywords`, `strong_title_keywords`, `exclude_title_words` | `title_keywords: ["software", "engineer", "developer"]` |
| **Industry priority** | `targeting.priority_sectors`, `priority_mode` | `[pharma]` + `strict` = only pharma/biotech while any remain |
| **Which companies** | `config/companies.yaml` | paste any `…myworkdayjobs.com/<Site>` URL with a `sector:` |
| **Season / dates** | `targeting.seasons`; `profile.availability` | `["Fall 2027"]` |
| **Locations** | `targeting.locations` (plain words) | `"Remote or Boston only"` |
| **How strict the ranking is** | `targeting.min_score`, `targeting.scoring_guidance` | `70`; your own 0–100 rubric |
| **A different resume** | replace `documents/resume.pdf` (or change `documents.resume`) | |
| **Cover letters** | `documents.cover_letter` | `when_required` / `always` / `never` |
| **Daily run time** | `schedule.daily_run_time`, then `./agent.sh install` | `"08:30"` |
| **Claude models / speed** | `claude.*` | `apply_model: opus`, `effort.apply: medium` |
| **Watch the browser** | `browser.headless: false`, or `--headed` on `apply` / `cycle` | |
| **Notifications** | `notifications.*` | turn off the e-mail digest |
| **Your answers to common questions** | `answers.yaml` or `./agent.sh answer` | |

**Finding a company's Workday URL:** open its careers site, click any job's Apply button, and copy the address
up to the site name, e.g. `https://acme.wd5.myworkdayjobs.com/AcmeCareers`. Or search the web for
`site:myworkdayjobs.com <company>`. Add it to `companies.yaml` with a `sector:`; the next scan checks it.

## Everyday commands

| Command | What it does |
|---|---|
| `./agent.sh status` | Summary + refresh **Dashboard.html** |
| `./agent.sh answer` | Answer questions the agent paused on (saved for all future forms) |
| `./agent.sh approve all` | Submit everything waiting at the Review page (on the next run) |
| `./agent.sh mode auto` / `mode review` | Submit automatically / stop at Review |
| `./agent.sh pause` / `resume` | Stop / restart everything |
| `./agent.sh cycle --force --headed` | Run today's batch now, with the browser visible |
| `./agent.sh apply <id> [--headed] [--mode review]` | Apply to one posting now |
| `./agent.sh discover` / `triage` | Scan sites / rank new postings |
| `./agent.sh login <company>` | Show the Workday login it uses there (only when you type it in Terminal) |
| `./agent.sh install` / `uninstall` | Turn the daily background run on / off |
| `./agent.sh init` / `secrets-template` / `import-secrets` / `setup` | First-time setup helpers |

## Privacy and safety design

- **Passwords never reach Claude.** They live in the macOS Keychain; the code types them straight into Workday
  login boxes. Page snapshots sent to Claude exclude password fields. See SECRETS.md.
- **What Claude does see:** your profile and answer bank, the job description, and each form page's questions.
  That is necessary to fill the forms. Review your Claude account's privacy / data-use settings.
- **No tools for Claude.** Every Claude call runs with tools disabled and must return JSON. Text inside a job
  posting cannot make the agent run commands or visit other sites.
- **Truthfulness.** Claude may only use your files; required questions it can't answer pause the job. Lists are
  matched strictly and any wrong selection is removed. Essays are written from your profile and logged.
- **Submit is guarded.** No code path can press a button labelled Submit except the final step, and only in
  `auto` mode after Claude's Review-page check passes. Clicks are never retried once the page has changed.
- **Everything is logged** in `data/runs/` (pages seen, values typed, essays, screenshots) and stays on your Mac.
- **No CAPTCHA solving.** If a site shows a "prove you're human" check, the job is flagged for you.

## Limitations

- **Workday only.** Companies on Greenhouse, Lever, Ashby, iCIMS, Taleo, SuccessFactors or their own
  systems (most startups, AI labs, and big tech) are not covered.
- **U.S.-oriented** forms and filters; **Gmail** only (verification e-mails are read over IMAP); **macOS** only.
- Runs only while the Mac is on, awake and logged in.
- Your Claude plan's usage limits apply; when reached, the rest of the day's batch waits for the next run.
- Workday sometimes changes its pages; when something new appears the job is held, not guessed. Check each
  site's terms of use before automating applications.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `config/profile.yaml is missing` | run `./agent.sh init` |
| `Gmail login FAILED` | 2-Step Verification must be on; create a new app password; re-run `import-secrets` |
| A company never loads ("422") | its Workday address moved — search `site:myworkdayjobs.com <company>` and update the URL |
| Jobs held with "needs your answer" | `./agent.sh answer` |
| Jobs held with "needs your attention" | see the reason on the dashboard and the screenshot in `data/runs/` |
| Nothing ran today | `cat ~/Library/Logs/InternshipAgent/launchd.log`; was the Mac asleep/closed? |
| "Claude usage limit" | lower `max_applications_per_day`; the agent resumes on the next run |

## Files

```
agent/            code (Python + dom.js page reader)
prompts/          instructions given to Claude (ranking, form filling)
config/
  companies.yaml          Workday sites to scan (shared)
  settings.example.yaml   -> settings.yaml  (your behaviour settings, git-ignored)
  profile.example.yaml    -> profile.yaml   (your facts, git-ignored)
  answers.example.yaml    -> answers.yaml   (your standard answers, git-ignored)
documents/        resume.pdf / transcript.pdf (git-ignored)
data/             database, logs, per-application records, browser profile (created at runtime, git-ignored)
agent.sh          command-line entry point
```
