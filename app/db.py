"""SQLite persistence for the job tracker.

All SQL uses parameter placeholders (?) so user input is never interpolated into queries.
Schema changes are applied in init_db. Existing databases are backed up before any table rebuild.
"""

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH_ENV_VAR = "JOB_TRACKER_DB"

STATUSES = ("Saved", "In preparation", "Submitted", "Interviewing", "Offer", "Rejected", "Withdrawn")
EVENT_KINDS = ("Submitted", "Recruiter response", "Interview", "Follow-up", "Offer", "Rejection", "Note")
BOARD_PROVIDERS = ("Greenhouse", "Lever")

# Statuses that mean the job is still open for you to act on.
ACTIVE_STATUSES = ("Saved", "In preparation")

FOLLOW_UP_DAYS_AFTER_SUBMISSION = 7

JOB_COLUMNS = (
    "id",
    "title",
    "company",
    "location",
    "url",
    "status",
    "notes",
    "created_at",
    "updated_at",
    "description",
    "fit_analysis",
    "source",
    "external_id",
    "submitted_at",
    "last_seen_at",
    "expired_at",
    "dedupe_key",
)

def _quoted_list(values):
    """Render ('a', 'b') as the SQL text 'a', 'b' for use in CHECK constraints."""
    return ", ".join(f"'{value}'" for value in values)


def jobs_table_sql(name):
    """CREATE TABLE statement for the jobs table under the given name."""
    return f"""
CREATE TABLE IF NOT EXISTS {name} (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT,
    url           TEXT,
    status        TEXT NOT NULL DEFAULT 'Saved'
                  CHECK (status IN ({_quoted_list(STATUSES)})),
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    description   TEXT,
    fit_analysis  TEXT,
    source        TEXT,
    external_id   TEXT,
    submitted_at  TEXT,
    last_seen_at  TEXT,
    expired_at    TEXT,
    dedupe_key    TEXT
);
"""


SCHEMA = (
    jobs_table_sql("jobs")
    + f"""
CREATE TABLE IF NOT EXISTS profile (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    full_name         TEXT NOT NULL DEFAULT '',
    headline          TEXT NOT NULL DEFAULT '',
    summary           TEXT NOT NULL DEFAULT '',
    years_experience  TEXT NOT NULL DEFAULT '',
    education         TEXT NOT NULL DEFAULT '',
    skills            TEXT NOT NULL DEFAULT '',
    achievements      TEXT NOT NULL DEFAULT '',
    target_roles      TEXT NOT NULL DEFAULT '',
    industries        TEXT NOT NULL DEFAULT '',
    preferred_locations TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS experience (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    location     TEXT,
    started_on   TEXT,
    finished_on  TEXT,
    description  TEXT,
    source       TEXT NOT NULL DEFAULT 'Manual',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS job_drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('Tailored resume', 'Cover letter')),
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS company_research (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    research    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prep_drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    contact_id  INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('Referral request', 'Networking message', 'Interview prep')),
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    name         TEXT NOT NULL,
    role         TEXT,
    company      TEXT,
    profile_url  TEXT,
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS job_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ({", ".join(f"'{k}'" for k in EVENT_KINDS)})),
    occurred_at  TEXT NOT NULL,
    due_date     TEXT,
    done         INTEGER NOT NULL DEFAULT 0,
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watched_boards (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    provider  TEXT NOT NULL CHECK (provider IN ({", ".join(f"'{p}'" for p in BOARD_PROVIDERS)})),
    token     TEXT NOT NULL,
    label     TEXT NOT NULL,
    added_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider, token)
);

CREATE TABLE IF NOT EXISTS review_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    fit_score   INTEGER NOT NULL,
    verdict     TEXT NOT NULL,
    output_dir  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'Pending'
                CHECK (status IN ('Pending', 'Approved', 'Rejected')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at  TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('Resume', 'Other')),
    original_name TEXT NOT NULL,
    stored_name  TEXT NOT NULL UNIQUE,
    uploaded_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
)


def default_db_path():
    """Return JOB_TRACKER_DB if set (used by tests), else data/jobs.db in the project."""
    return Path(os.environ.get(DB_PATH_ENV_VAR, PROJECT_ROOT / "data" / "jobs.db"))


def connect(path=None):
    """Open a connection, creating the database file and its folder if needed."""
    path = Path(path) if path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


# Columns added after the first version of the jobs table. Existing databases get them
# through ALTER TABLE, because CREATE TABLE IF NOT EXISTS does not change an existing table.
JOB_COLUMN_ADDITIONS = {
    "description": "TEXT",
    "fit_analysis": "TEXT",
    "source": "TEXT",
    "external_id": "TEXT",
    "submitted_at": "TEXT",
    "last_seen_at": "TEXT",
    "expired_at": "TEXT",
    "dedupe_key": "TEXT",
}


# Profile columns added after the first version of the profile table.
PROFILE_COLUMN_ADDITIONS = {
    "target_roles": "TEXT NOT NULL DEFAULT ''",
    "industries": "TEXT NOT NULL DEFAULT ''",
    "preferred_locations": "TEXT NOT NULL DEFAULT ''",
}


def init_db(conn):
    """Create tables and apply any schema upgrades. Safe to call on every start."""
    conn.executescript(SCHEMA)
    _add_missing_profile_columns(conn)
    _add_missing_job_columns(conn)
    if _jobs_status_is_outdated(conn):
        _rebuild_jobs_table(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_external ON jobs (source, external_id)"
    )
    _backfill_dedupe_keys(conn)
    conn.commit()


def _add_missing_profile_columns(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(profile)")}
    for column, column_type in PROFILE_COLUMN_ADDITIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE profile ADD COLUMN {column} {column_type}")
    conn.commit()


def replace_linkedin_experience(conn, rows):
    """Replace all experience imported from a LinkedIn export. Manual entries are kept.

    Replacing rather than appending makes re-importing the same export harmless.
    """
    conn.execute("DELETE FROM experience WHERE source = 'LinkedIn export'")
    for row in rows:
        conn.execute(
            "INSERT INTO experience (company, title, location, started_on, finished_on, description, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'LinkedIn export')",
            (row["company"], row["title"], row.get("location"), row.get("started_on"),
             row.get("finished_on"), row.get("description")),
        )
    conn.commit()


def list_experience(conn):
    """Return all experience entries in the order they were added."""
    rows = conn.execute("SELECT * FROM experience ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def _add_missing_job_columns(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for column, column_type in JOB_COLUMN_ADDITIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {column_type}")
    conn.commit()


def _jobs_status_is_outdated(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()
    return row is not None and "'In preparation'" not in row["sql"]


def _backup_database(conn):
    """Copy the database file to data/backups/ before a rebuild. Returns the backup path."""
    file_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    if not file_path:
        return None
    source = Path(file_path)
    backup_dir = source.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{source.stem}-before-lifecycle-{stamp}.db"
    with closing(sqlite3.connect(target)) as backup:
        conn.backup(backup)
    return target


def _rebuild_jobs_table(conn):
    """Rebuild the jobs table to adopt the new status list, keeping every row and link.

    SQLite cannot change a CHECK constraint in place. The procedure is the documented one:
    turn off foreign keys, copy the rows, swap the tables, and check foreign keys before commit.
    The old 'Applied' status becomes 'Submitted'.
    """
    _backup_database(conn)
    conn.commit()
    old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    columns = [column for column in JOB_COLUMNS if column in old_columns]
    select_list = ", ".join(
        "CASE status WHEN 'Applied' THEN 'Submitted' ELSE status END" if column == "status" else column
        for column in columns
    )
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(jobs_table_sql("jobs_new"))
        conn.execute(f"INSERT INTO jobs_new ({', '.join(columns)}) SELECT {select_list} FROM jobs")
        conn.execute("DROP TABLE jobs")
        conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"Foreign key check failed after rebuild: {len(problems)} problem(s)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def make_dedupe_key(company, title, location):
    """Build a key that matches the same posting listed on two boards."""

    def normalize(text):
        return re.sub(r"\s+", " ", (text or "").strip().lower())

    return "|".join(normalize(part) for part in (company, title, location))


def _backfill_dedupe_keys(conn):
    rows = conn.execute("SELECT id, company, title, location FROM jobs WHERE dedupe_key IS NULL").fetchall()
    for row in rows:
        key = make_dedupe_key(row["company"], row["title"], row["location"])
        conn.execute("UPDATE jobs SET dedupe_key = ? WHERE id = ?", (key, row["id"]))


def add_watched_board(conn, provider, token, label):
    """Add a job board to the watchlist. Returns its id, or None if it is already there."""
    # Checked here because INSERT OR IGNORE would silently skip a row that breaks the CHECK.
    if provider not in BOARD_PROVIDERS:
        raise ValueError(f"Unknown job board type: {provider!r}")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO watched_boards (provider, token, label) VALUES (?, ?, ?)",
        (provider, token.strip(), label.strip()),
    )
    conn.commit()
    return cursor.lastrowid if cursor.rowcount else None


def list_watched_boards(conn):
    """Return the watchlist, ordered by label."""
    rows = conn.execute("SELECT * FROM watched_boards ORDER BY label COLLATE NOCASE, id").fetchall()
    return [dict(row) for row in rows]


def remove_watched_board(conn, board_id):
    """Remove a board from the watchlist."""
    conn.execute("DELETE FROM watched_boards WHERE id = ?", (board_id,))
    conn.commit()


def import_discovered_job(conn, job):
    """Save a discovered posting. Returns 'new', 'duplicate', or 'existing'.

    'duplicate' means the same company, title, and location is already tracked from another
    board. 'existing' means this exact posting was saved before.
    """
    if find_job_id(conn, job["source"], job["external_id"]):
        return "existing"
    key = make_dedupe_key(job["company"], job["title"], job.get("location"))
    if find_duplicate(conn, key):
        return "duplicate"
    cursor = conn.execute(
        "INSERT OR IGNORE INTO jobs (title, company, location, url, description, source, external_id, "
        "dedupe_key, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (
            job["title"],
            job["company"],
            job.get("location"),
            job.get("url"),
            job.get("description"),
            job["source"],
            job["external_id"],
            key,
        ),
    )
    conn.commit()
    return "new" if cursor.rowcount == 1 else "existing"


def find_duplicate(conn, dedupe_key):
    """Return the id of a tracked job with this key, or None."""
    row = conn.execute("SELECT id FROM jobs WHERE dedupe_key = ? LIMIT 1", (dedupe_key,)).fetchone()
    return row["id"] if row else None


def find_job_id(conn, source, external_id):
    """Return the id of a discovered job by its source and external id, or None."""
    row = conn.execute(
        "SELECT id FROM jobs WHERE source = ? AND external_id = ?", (source, external_id)
    ).fetchone()
    return row["id"] if row else None


def record_board_scan(conn, read_sources, seen):
    """Update last-seen and expiry for discovered postings after a scan.

    read_sources: board keys (for example 'Lever:beta') that were read successfully.
    seen: set of (source, external_id) pairs returned by those boards.
    A posting on a successfully read board that is no longer listed is marked expired.
    Boards that failed to read are left alone, so an outage never expires postings.
    """
    for source in read_sources:
        rows = conn.execute(
            "SELECT id, external_id, expired_at FROM jobs WHERE source = ?", (source,)
        ).fetchall()
        for row in rows:
            if (source, row["external_id"]) in seen:
                conn.execute(
                    "UPDATE jobs SET last_seen_at = datetime('now'), expired_at = NULL WHERE id = ?",
                    (row["id"],),
                )
            elif row["expired_at"] is None:
                conn.execute("UPDATE jobs SET expired_at = datetime('now') WHERE id = ?", (row["id"],))
    conn.commit()


def jobs_needing_fit(conn):
    """Discovered jobs without a fit analysis that are not already in the review queue."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE source IS NOT NULL AND fit_analysis IS NULL "
        "AND expired_at IS NULL AND id NOT IN (SELECT job_id FROM review_queue) ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def add_queue_item(conn, job_id, fit_score, verdict, output_dir):
    """Put a job in the review queue. Returns the queue id."""
    cursor = conn.execute(
        "INSERT INTO review_queue (job_id, fit_score, verdict, output_dir) VALUES (?, ?, ?, ?)",
        (job_id, fit_score, verdict, str(output_dir)),
    )
    conn.commit()
    return cursor.lastrowid


def list_queue(conn, status=None):
    """Return queue items with their job details, highest fit score first."""
    sql = (
        "SELECT q.*, j.title, j.company, j.location, j.url, j.fit_analysis "
        "FROM review_queue q JOIN jobs j ON j.id = q.job_id "
    )
    if status is None:
        rows = conn.execute(sql + "ORDER BY q.fit_score DESC, q.id").fetchall()
    else:
        rows = conn.execute(
            sql + "WHERE q.status = ? ORDER BY q.fit_score DESC, q.id", (status,)
        ).fetchall()
    return [dict(row) for row in rows]


def set_queue_status(conn, queue_id, status):
    """Record a decision on a queue item. Only Approved or Rejected are allowed."""
    if status not in ("Approved", "Rejected"):
        raise ValueError(f"Unknown queue decision: {status!r}")
    conn.execute(
        "UPDATE review_queue SET status = ?, decided_at = datetime('now') WHERE id = ?",
        (status, queue_id),
    )
    conn.commit()


def saved_external_keys(conn):
    """Return (source, external_id) pairs already in the tracker, to mark duplicates."""
    rows = conn.execute("SELECT source, external_id FROM jobs WHERE source IS NOT NULL").fetchall()
    return {(row["source"], row["external_id"]) for row in rows}


def add_job(conn, title, company, location=None, url=None, notes=None, description=None):
    """Insert a job with status 'Saved' and return its id."""
    cursor = conn.execute(
        "INSERT INTO jobs (title, company, location, url, notes, description, dedupe_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (title, company, location, url, notes, description, make_dedupe_key(company, title, location)),
    )
    conn.commit()
    return cursor.lastrowid


def get_job(conn, job_id):
    """Return one job as a dict, or None if it does not exist."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def save_drafts(conn, job_id, drafts):
    """Store generated drafts for a job. drafts maps kind -> text."""
    for kind, content in drafts.items():
        conn.execute(
            "INSERT INTO job_drafts (job_id, kind, content) VALUES (?, ?, ?)",
            (job_id, kind, content),
        )
    conn.commit()


def list_drafts(conn, job_id):
    """Return a job's drafts, newest first."""
    rows = conn.execute(
        "SELECT * FROM job_drafts WHERE job_id = ? ORDER BY created_at DESC, id DESC",
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def save_research(conn, job_id, research):
    """Store a company research result (a dict) for a job."""
    conn.execute(
        "INSERT INTO company_research (job_id, research) VALUES (?, ?)",
        (job_id, json.dumps(research)),
    )
    conn.commit()


def list_research(conn, job_id):
    """Return a job's research results, newest first, with the research decoded."""
    rows = conn.execute(
        "SELECT * FROM company_research WHERE job_id = ? ORDER BY created_at DESC, id DESC",
        (job_id,),
    ).fetchall()
    return [{**dict(row), "research": json.loads(row["research"])} for row in rows]


def add_contact(conn, name, role=None, company=None, profile_url=None, notes=None, job_id=None):
    """Save a contact you entered yourself. Returns the new id."""
    cursor = conn.execute(
        "INSERT INTO contacts (job_id, name, role, company, profile_url, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, name, role, company, profile_url, notes),
    )
    conn.commit()
    return cursor.lastrowid


def list_contacts(conn, job_id=None):
    """Return contacts, newest first. Filter to one job if job_id is given."""
    if job_id is None:
        rows = conn.execute("SELECT * FROM contacts ORDER BY created_at DESC, id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM contacts WHERE job_id = ? ORDER BY created_at DESC, id DESC",
            (job_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_contact(conn, contact_id):
    """Return one contact as a dict, or None."""
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    return dict(row) if row else None


def save_prep_draft(conn, job_id, kind, content, contact_id=None):
    """Store an outreach draft or interview prep for a job. Returns the new id."""
    cursor = conn.execute(
        "INSERT INTO prep_drafts (job_id, contact_id, kind, content) VALUES (?, ?, ?, ?)",
        (job_id, contact_id, kind, content),
    )
    conn.commit()
    return cursor.lastrowid


def list_prep_drafts(conn, job_id):
    """Return a job's outreach and prep drafts, newest first."""
    rows = conn.execute(
        "SELECT * FROM prep_drafts WHERE job_id = ? ORDER BY created_at DESC, id DESC",
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def save_fit_analysis(conn, job_id, analysis):
    """Store a fit analysis (a dict) as JSON on the job."""
    conn.execute(
        "UPDATE jobs SET fit_analysis = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(analysis), job_id),
    )
    conn.commit()


def list_jobs(conn, status=None):
    """Return jobs as dicts, newest first. Filter by status if given."""
    if status is None:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC, id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC, id DESC",
            (status,),
        ).fetchall()
    return [dict(row) for row in rows]


def update_status(conn, job_id, status):
    """Change a job's status. Moving to 'Submitted' records the date and a follow-up reminder."""
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status!r}")
    conn.execute(
        "UPDATE jobs SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, job_id),
    )
    if status == "Submitted":
        job = get_job(conn, job_id)
        if job["submitted_at"] is None:
            conn.execute(
                "UPDATE jobs SET submitted_at = datetime('now') WHERE id = ?", (job_id,)
            )
            conn.execute(
                "INSERT INTO job_events (job_id, kind, occurred_at, notes) "
                "VALUES (?, 'Submitted', datetime('now'), 'Submitted by you')",
                (job_id,),
            )
            conn.execute(
                "INSERT INTO job_events (job_id, kind, occurred_at, due_date, notes) "
                "VALUES (?, 'Follow-up', datetime('now'), date('now', ?), 'Check for a response')",
                (job_id, f"+{FOLLOW_UP_DAYS_AFTER_SUBMISSION} days"),
            )
    conn.commit()


def add_event(conn, job_id, kind, occurred_at, due_date=None, notes=None):
    """Record a lifecycle event for a job: a response, interview, follow-up, offer, and so on."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"Unknown event kind: {kind!r}")
    cursor = conn.execute(
        "INSERT INTO job_events (job_id, kind, occurred_at, due_date, notes) VALUES (?, ?, ?, ?, ?)",
        (job_id, kind, occurred_at, due_date, notes),
    )
    conn.commit()
    return cursor.lastrowid


def list_events(conn, job_id):
    """Return a job's events, oldest first."""
    rows = conn.execute(
        "SELECT * FROM job_events WHERE job_id = ? ORDER BY occurred_at, id", (job_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def complete_event(conn, event_id):
    """Mark a follow-up as done."""
    conn.execute("UPDATE job_events SET done = 1 WHERE id = ?", (event_id,))
    conn.commit()


def list_open_followups(conn):
    """Return follow-ups not yet done, earliest due date first, with job details."""
    rows = conn.execute(
        "SELECT e.*, j.title, j.company, j.url FROM job_events e JOIN jobs j ON j.id = e.job_id "
        "WHERE e.kind = 'Follow-up' AND e.done = 0 ORDER BY e.due_date, e.id"
    ).fetchall()
    return [dict(row) for row in rows]


def recommend(score):
    """Turn a fit score into a plain recommendation for the shortlist."""
    if score >= 85:
        return "Prioritize"
    if score >= 70:
        return "Review"
    return "Low priority"


def shortlist(conn):
    """Open, unexpired jobs with a fit score, ranked best first."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE fit_analysis IS NOT NULL AND expired_at IS NULL "
        "AND status IN ('Saved', 'In preparation')"
    ).fetchall()
    ranked = []
    for row in rows:
        job = dict(row)
        analysis = json.loads(job["fit_analysis"])
        job["score"] = analysis["score"]
        job["verdict"] = analysis["verdict"]
        job["recommendation"] = recommend(analysis["score"])
        ranked.append(job)
    ranked.sort(key=lambda job: (-job["score"], job["id"]))
    return ranked
