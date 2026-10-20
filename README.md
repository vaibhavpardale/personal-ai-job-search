# Personal AI Job Search Assistant

A personal, local-first tool that helps me run a better job search. It finds and ranks
relevant senior-level roles, explains the fit against my real experience, and prepares
(but never submits) applications, outreach, and interview material.

## Objective

Help me:

1. Discover relevant, real job vacancies.
2. Identify suitable companies and hiring opportunities.
3. Rank jobs by career fit and strategic value.
4. Explain why each job matches or does not match my profile.
5. Tailor my résumé to a specific job description.
6. Draft role-specific cover letters and application answers.
7. Research companies and relevant hiring managers.
8. Draft networking messages and referral requests.
9. Prepare for interviews using the job description and my actual experience.
10. Track saved jobs, applications, interviews, follow-ups, and outcomes.

Quality over volume: senior-relevant opportunities matter more than long lists of weak matches.

## Principles and Safety

- **Human in the loop.** The AI researches, analyzes, drafts, and organizes. I review and
  submit applications myself.
- **No autonomous outbound actions.** The system never submits applications, sends messages,
  or contacts recruiters without my explicit approval.
- **Verified facts only.** Application documents use only verified career facts. No invented
  qualifications, employers, skills, metrics, or achievements.
- **Legitimate data sources.** Respect website terms, access restrictions, privacy, and rate limits.
- **Secrets stay local.** API keys live in a `.env` file that is git-ignored.

## Architecture (initial)

| Layer          | Choice                 | Why                                              |
|----------------|------------------------|--------------------------------------------------|
| Logic / AI     | Python                 | Primary language; strong AI ecosystem            |
| UI             | Streamlit              | Fast single-user interface, pure Python          |
| Persistence    | SQLite                 | Zero-infrastructure local database               |
| AI processing  | OpenAI API             | Initial model provider; swappable later          |
| Documents      | Local file storage     | Personal résumé and documents stay on my machine |
| Dev environment| Claude Code            | Incremental, reviewed development                |

Deliberately excluded for now: multi-agent orchestration, live job scraping, paid
third-party integrations, and cloud infrastructure. They are added only after the core
workflow is validated.

## Planned Layout

This is the intended structure. Folders are created in the step that first needs them.

```
personal-ai-job-search/
├── README.md          # This file: objective, architecture, milestones, setup
├── requirements.txt   # Essential Python dependencies
├── .gitignore         # Keeps secrets, envs, databases, and personal docs out of git
├── app/               # Application code (Streamlit entry point, modules)
├── data/              # Local SQLite database (git-ignored)
└── documents/         # Personal documents such as résumé versions (git-ignored)
```

## Development Stages

| # | Stage                                   | Status   |
|---|-----------------------------------------|----------|
| 1 | Project setup (this document, deps, git-ignore) | Done     |
| 2 | Minimal Streamlit shell                 | Done     |
| 3 | SQLite schema and job tracker           | Done     |
| 4 | Profile and document storage            | Done     |
| 5 | OpenAI integration and job-fit analysis | Done     |
| 6 | Résumé tailoring and cover letters      | Done     |
| 7 | Company and contact research            | Done     |
| 8 | Networking drafts and interview prep    | Done     |
| 9 | Job discovery from legitimate sources   | Done     |
| 10 | Review queue: daily search, match, tailor | In review |

Each stage is built, tested, and reviewed before the next one begins.

## Setup

Requires Python 3.10 or later.

```bash
cd personal-ai-job-search
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root for secrets. It is git-ignored:

```bash
cp .env.example .env
```

Then open `.env` and replace `your-openai-api-key-here` with your key. Never commit `.env`
or paste the key into a chat or document.

Optionally set `OPENAI_MODEL` in `.env` to change the model used for fit analysis.

Run the app from the project root:

```bash
streamlit run app/main.py
```

Run the tests from the project root:

```bash
python -m unittest discover -s tests -v
```

The tests use a temporary database (set through the `JOB_TRACKER_DB` environment variable),
so they never touch your real `data/jobs.db`.

## Milestones Completed

- **Stage 1:** Project setup. README, `requirements.txt`, and `.gitignore` created. Dependencies
  installed and import-checked in a local virtual environment.
- **Stage 2:** Minimal Streamlit shell at `app/main.py`. Renders a title, caption, and a sidebar
  with the stage roadmap. No database or AI calls yet. Verified by starting the server (health
  endpoint returns `ok`) and by running the script through Streamlit's `AppTest` with no exceptions.
- **Stage 3:** SQLite job tracker. `app/db.py` holds the `jobs` table (title, company, location, URL,
  status, notes, timestamps). Status is limited to Saved, Applied, Interviewing, Offer, Rejected,
  and Withdrawn, enforced by a database `CHECK` constraint as well as in Python. The Streamlit page
  adds a form to save jobs, a status filter, a table, and a control to update a job's status.
  Verified by 12 passing unit and UI tests.
- **Stage 4:** Profile and document storage. The profile is a single SQLite record (`app/profile_store.py`).
  Its starting values come only from your stated background (headline, ~12 years of experience,
  IIM PGDM). Everything else is blank for you to fill in, so no details are invented. Uploaded
  documents (PDF, DOCX, TXT, MD) are saved to the git-ignored `documents/` folder under a unique
  name, with metadata in SQLite. File names are sanitized so uploads cannot escape that folder.
  Verified by 23 passing tests across Stages 3 and 4.
- **Stage 5:** OpenAI integration and job-fit analysis (`app/ai.py`). Each job can store its
  description. "Analyze fit" sends your saved profile and the job description to the model, using
  a strict JSON schema, and shows a 0–100 score, a verdict, strengths, and gaps. The model is told to
  use only the facts provided and to list missing requirements as gaps. Results are validated before
  they're stored. The API key is read from `.env` and never stored in the database. Existing databases
  are upgraded automatically with the new columns.
  Verified by 37 passing tests using a fake client, and by a live check: the default model
  `gpt-4.1-mini` returned a valid analysis, and the "Analyze fit" button saved and displayed it.
- **Stage 6:** Tailored résumé and cover letter. Pick a job and a source résumé (or profile only), and
  "Generate tailored draft" produces résumé headline, summary, bullets, and a cover letter. Drafts are
  saved per job in the `job_drafts` table. Résumé text is extracted from TXT, MD, PDF, and DOCX files.
  The model must use only the profile and résumé facts. It lists job requirements your materials don't
  support, so the draft doesn't claim them, and it lists inferred wording for you to confirm.
  Verified by 51 passing tests (fake client) and a live check on the seed profile, which correctly flagged
  three unsupported requirements. **Not yet tested** with a real résumé upload, since none has been
  uploaded yet.
- **Stage 7:** Company research and contacts. The Research tab runs a web search for public company
  information and saves each briefing per job (summary, key facts, recent news with dates, what could
  not be verified). Sources are the search results' own citations, not URLs the model wrote. The
  search and the structuring steps are separate calls, because citations are dropped when a JSON schema
  is requested in the same call. A contacts list lets you add people you find yourself, linked to a
  job or standalone. The app does not look up or scrape people's profiles.
  Verified by 66 passing tests (fake responses) and two live research runs that returned sources and
  clean text. A citation shows where information was found, not that every claim in the briefing is
  covered by it, so check important facts before relying on them.
- **Stage 8:** Networking drafts and interview prep, on the "Outreach & Prep" tab. Referral requests and
  networking messages are drafted for a saved contact. Interview prep lists likely questions, why each is
  asked, an answer drawn from your profile and latest résumé, gaps to prepare for, and questions to ask
  them. Nothing is sent. Drafts are saved per job for you to copy.
  Prompts forbid claimed shared history with contacts and invented experience. A live check caught the
  model inventing team-leadership details that weren't in the profile. The rule was tightened, and the
  next live run marked the gaps as "No evidence in profile or résumé". The rule is still prompt-based,
  so review every draft against your real experience before using it.
  Verified by 79 passing tests (fake responses) and a live check of outreach and interview prep.
- **Stage 9:** Job discovery from public job-board APIs, on the "Discover" tab. You add the boards of
  companies you want to watch: Greenhouse (`boards.greenhouse.io/<name>`) or Lever (`jobs.lever.co/<name>`).
  "Check watchlist" reads those public job lists, one board at a time with a pause between them, and
  keeps postings whose titles match your keywords. The default keywords are senior titles, because
  broad words such as "product" also match sales roles. You choose which postings to save, and a
  posting already saved is never added twice.
  No scraping, no logins, no paid services, and no AI calls.
  Verified by 103 passing tests (fake HTTP) and a live run: Stripe returned 121 matching postings with
  descriptions, a missing board gave a readable error without stopping the run, and a second import
  saved nothing new.
  **Before relying on this:** check each company's board and terms, and keep the watchlist to companies
  you actually want to follow. Discovery covers only companies that use Greenhouse or Lever.
- **Stage 10:** Review queue, on the "Review queue" tab, and from the command line. A run searches the
  watchlist, saves new postings, scores them against your profile and résumé, and for postings at or
  above the minimum score prepares a folder with a tailored résumé (DOCX), a cover letter, and review
  notes listing unsupported requirements and inferences. Each queued item shows its score, gaps, and
  downloads, with **Open posting**, **Approve**, and **Reject**. Approving only records your decision.
  **Nothing is submitted, sent, or messaged by the app.** You apply yourself and mark the job Applied.
  Limits: at most N AI analyses per run, and a posting is never queued twice.
  Scheduling: `python app/run_daily.py --keywords "..." --min-score 70 --max-analyses 10` from a cron or
  launchd job. Scheduling is not set up by the app.
  Verified by 116 passing tests (fake AI client and fake job-board responses). Not yet run against live
  APIs with your résumé and watchlist.

## Pending Work

- Confirm the setup (Stage 1) before starting Stage 2.
- Decide whether to add a profile source (LinkedIn export or résumé file) for Stage 4.
  LinkedIn pages generally need authentication, so a résumé or exported profile is the
  reliable source.
