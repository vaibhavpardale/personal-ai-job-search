"""SQLite persistence for the job tracker.

All SQL uses parameter placeholders (?) so user input is never interpolated into queries.
"""

import json
import os
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH_ENV_VAR = "JOB_TRACKER_DB"

STATUSES = ("Saved", "Applied", "Interviewing", "Offer", "Rejected", "Withdrawn")

BOARD_PROVIDERS = ("Greenhouse", "Lever")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    location    TEXT,
    url         TEXT,
    status      TEXT NOT NULL DEFAULT 'Saved'
                CHECK (status IN ({", ".join(f"'{s}'" for s in STATUSES)})),
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profile (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    full_name         TEXT NOT NULL DEFAULT '',
    headline          TEXT NOT NULL DEFAULT '',
    summary           TEXT NOT NULL DEFAULT '',
    years_experience  TEXT NOT NULL DEFAULT '',
    education         TEXT NOT NULL DEFAULT '',
    skills            TEXT NOT NULL DEFAULT '',
    achievements      TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
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

CREATE TABLE IF NOT EXISTS watched_boards (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    provider  TEXT NOT NULL CHECK (provider IN ({", ".join(f"'{p}'" for p in BOARD_PROVIDERS)})),
    token     TEXT NOT NULL,
    label     TEXT NOT NULL,
    added_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider, token)
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('Resume', 'Other')),
    original_name TEXT NOT NULL,
    stored_name  TEXT NOT NULL UNIQUE,
    uploaded_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


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
}


def init_db(conn):
    """Create tables and add any missing columns. Safe to call on every start."""
    conn.executescript(SCHEMA)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for column, column_type in JOB_COLUMN_ADDITIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {column_type}")
    # One saved copy of each discovered posting. Manually added jobs have NULLs here, which
    # SQLite does not treat as duplicates.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_external ON jobs (source, external_id)"
    )
    conn.commit()


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
    """Save a discovered posting as a job. Returns True if it was new, False if already saved."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO jobs (title, company, location, url, description, source, external_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            job["title"],
            job["company"],
            job.get("location"),
            job.get("url"),
            job.get("description"),
            job["source"],
            job["external_id"],
        ),
    )
    conn.commit()
    return cursor.rowcount == 1


def saved_external_keys(conn):
    """Return (source, external_id) pairs already in the tracker, to mark duplicates."""
    rows = conn.execute("SELECT source, external_id FROM jobs WHERE source IS NOT NULL").fetchall()
    return {(row["source"], row["external_id"]) for row in rows}


def add_job(conn, title, company, location=None, url=None, notes=None, description=None):
    """Insert a job with status 'Saved' and return its id."""
    cursor = conn.execute(
        "INSERT INTO jobs (title, company, location, url, notes, description) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (title, company, location, url, notes, description),
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
    """Change a job's status. Raises ValueError for unknown statuses."""
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status!r}")
    conn.execute(
        "UPDATE jobs SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, job_id),
    )
    conn.commit()
