"""Tests for application lifecycle, duplicates, expiry, filters, and the shortlist (phases 1 and 2).

Includes a migration test that builds a database in the old format, with linked rows, and checks
that upgrading keeps every row and link. No network calls are made.

Run from the project root with:
    python -m unittest discover -s tests -v
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import db  # noqa: E402
import discovery  # noqa: E402
import profile_store  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

def events_for(conn, job_id):
    """Read a job's events straight from the table, since the events UI was removed."""
    rows = conn.execute("SELECT * FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
    return [dict(row) for row in rows]


# The jobs table as it was before this change: old status list, no lifecycle columns.
OLD_JOBS_TABLE = """
CREATE TABLE jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    location    TEXT,
    url         TEXT,
    status      TEXT NOT NULL DEFAULT 'Saved'
                CHECK (status IN ('Saved', 'Applied', 'Interviewing', 'Offer', 'Rejected', 'Withdrawn')),
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE job_drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('Tailored resume', 'Cover letter')),
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def build_old_database(path):
    """Create a database in the pre-change format with rows that link to jobs."""
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("PRAGMA foreign_keys = ON")
        raw.executescript(OLD_JOBS_TABLE)
        raw.execute("INSERT INTO jobs (title, company, status) VALUES ('Role A', 'Acme', 'Saved')")
        raw.execute("INSERT INTO jobs (title, company, status) VALUES ('Role B', 'Beta', 'Applied')")
        raw.execute("INSERT INTO jobs (title, company, status) VALUES ('Role C', 'Gamma', 'Rejected')")
        raw.execute("INSERT INTO job_drafts (job_id, kind, content) VALUES (2, 'Cover letter', 'Keep me')")
        raw.commit()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "old.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_upgrade_keeps_every_row_and_link(self):
        build_old_database(self.path)
        with closing(db.connect(self.path)) as conn:
            jobs = db.list_jobs(conn)
            self.assertEqual(sorted(j["title"] for j in jobs), ["Role A", "Role B", "Role C"])
            draft = conn.execute("SELECT job_id, content FROM job_drafts").fetchone()
            self.assertEqual((draft["job_id"], draft["content"]), (2, "Keep me"))
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_applied_becomes_submitted_during_upgrade(self):
        build_old_database(self.path)
        with closing(db.connect(self.path)) as conn:
            statuses = {j["title"]: j["status"] for j in db.list_jobs(conn)}
        self.assertEqual(statuses, {"Role A": "Saved", "Role B": "Submitted", "Role C": "Rejected"})

    def test_upgrade_writes_a_backup_first(self):
        build_old_database(self.path)
        with closing(db.connect(self.path)):
            pass
        backups = list((self.path.parent / "backups").glob("*.db"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            count = backup.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            applied = backup.execute("SELECT COUNT(*) FROM jobs WHERE status = 'Applied'").fetchone()[0]
        self.assertEqual(count, 3)
        self.assertEqual(applied, 1)

    def test_new_statuses_are_accepted_after_upgrade(self):
        build_old_database(self.path)
        with closing(db.connect(self.path)) as conn:
            db.update_status(conn, 1, "In preparation")
            self.assertEqual(db.get_job(conn, 1)["status"], "In preparation")

    def test_upgrade_runs_once(self):
        build_old_database(self.path)
        with closing(db.connect(self.path)):
            pass
        with closing(db.connect(self.path)):
            pass
        self.assertEqual(len(list((self.path.parent / "backups").glob("*.db"))), 1)

    def test_fresh_database_needs_no_backup(self):
        with closing(db.connect(self.path)):
            pass
        self.assertFalse((self.path.parent / "backups").exists())

    def test_foreign_keys_are_back_on_after_upgrade(self):
        build_old_database(self.path)
        with closing(db.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "l.db")
        self.job_id = db.add_job(self.conn, "Role", "Acme")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_marking_submitted_records_date_and_a_follow_up(self):
        db.update_status(self.conn, self.job_id, "Submitted")
        job = db.get_job(self.conn, self.job_id)
        self.assertIsNotNone(job["submitted_at"])
        kinds = [e["kind"] for e in events_for(self.conn, self.job_id)]
        self.assertEqual(kinds, ["Submitted", "Follow-up"])
        followup = db.list_open_followups(self.conn)[0]
        self.assertEqual(followup["title"], "Role")
        self.assertIsNotNone(followup["due_date"])

    def test_resubmitting_does_not_duplicate_events(self):
        db.update_status(self.conn, self.job_id, "Submitted")
        db.update_status(self.conn, self.job_id, "Submitted")
        self.assertEqual(len(events_for(self.conn, self.job_id)), 2)

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            db.update_status(self.conn, self.job_id, "Applied")


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "d.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_dedupe_key_ignores_case_and_extra_spaces(self):
        self.assertEqual(
            db.make_dedupe_key(" Acme  Corp ", "Director of  Product", "Bengaluru"),
            db.make_dedupe_key("acme corp", "director of product", "bengaluru"),
        )

    def test_same_role_on_another_board_is_a_duplicate(self):
        greenhouse = {"title": "Director of Product", "company": "Acme", "location": "Bengaluru",
                      "source": "Greenhouse:acme", "external_id": "1"}
        lever = {"title": "director of product", "company": "ACME", "location": "Bengaluru",
                 "source": "Lever:acme", "external_id": "x9"}
        self.assertEqual(db.import_discovered_job(self.conn, greenhouse), "new")
        self.assertEqual(db.import_discovered_job(self.conn, lever), "duplicate")
        self.assertEqual(len(db.list_jobs(self.conn)), 1)

    def test_same_posting_again_is_existing_not_duplicate(self):
        job = {"title": "Role", "company": "Acme", "location": None, "source": "Lever:b", "external_id": "1"}
        db.import_discovered_job(self.conn, job)
        self.assertEqual(db.import_discovered_job(self.conn, job), "existing")


class ExpiryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "e.db")
        self.kept = {"title": "Role A", "company": "Acme", "location": None, "source": "Lever:b", "external_id": "1"}
        self.gone = {"title": "Role B", "company": "Beta", "location": None, "source": "Lever:b", "external_id": "2"}
        self.other_board = {"title": "Role C", "company": "Gamma", "location": None,
                            "source": "Greenhouse:g", "external_id": "3"}
        for job in (self.kept, self.gone, self.other_board):
            db.import_discovered_job(self.conn, job)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def job_by_title(self, title):
        return next(j for j in db.list_jobs(self.conn) if j["title"] == title)

    def test_missing_posting_on_a_read_board_is_expired(self):
        db.record_board_scan(self.conn, {"Lever:b"}, {("Lever:b", "1")})
        self.assertIsNotNone(self.job_by_title("Role B")["expired_at"])
        self.assertIsNone(self.job_by_title("Role A")["expired_at"])

    def test_postings_on_a_board_that_failed_are_not_expired(self):
        db.record_board_scan(self.conn, set(), set())
        self.assertIsNone(self.job_by_title("Role B")["expired_at"])
        self.assertIsNone(self.job_by_title("Role C")["expired_at"])

    def test_reappearing_posting_is_restored(self):
        db.record_board_scan(self.conn, {"Lever:b"}, {("Lever:b", "1")})
        db.record_board_scan(self.conn, {"Lever:b"}, {("Lever:b", "1"), ("Lever:b", "2")})
        self.assertIsNone(self.job_by_title("Role B")["expired_at"])

    def test_expired_posting_is_hidden_from_the_shortlist(self):
        for job in db.list_jobs(self.conn):
            db.save_fit_analysis(self.conn, job["id"], {"score": 80, "verdict": "Strong fit"})
        db.record_board_scan(self.conn, {"Lever:b"}, {("Lever:b", "1")})
        titles = [job["title"] for job in db.shortlist(self.conn)]
        self.assertNotIn("Role B", titles)
        self.assertIn("Role A", titles)


class FilterTests(unittest.TestCase):
    JOBS = [
        {"title": "Director of Product", "location": "Bengaluru, India"},
        {"title": "Director of Product", "location": "New York"},
        {"title": "Product Intern", "location": "Dubai"},
        {"title": "Head of Product", "location": None},
    ]

    def test_location_filter_keeps_matching_places_and_unknown_locations(self):
        kept = discovery.filter_jobs(self.JOBS, ["product"], locations=["india", "dubai"])
        self.assertEqual([(j["title"], j["location"]) for j in kept],
                         [("Director of Product", "Bengaluru, India"), ("Product Intern", "Dubai"),
                          ("Head of Product", None)])

    def test_exclude_terms_drop_junior_titles(self):
        kept = discovery.filter_jobs(self.JOBS, ["product"], exclude=["intern"])
        self.assertNotIn("Product Intern", [j["title"] for j in kept])

    def test_failed_board_is_reported_and_others_still_read(self):
        boards = [
            {"provider": "Lever", "token": "bad", "label": "Bad"},
            {"provider": "Lever", "token": "good", "label": "Good"},
        ]

        def fetch(provider, token, label):
            if token == "bad":
                raise discovery.DiscoveryError("Board not found.")
            return [{"title": "Role", "company": "Good", "location": None,
                     "source": "Lever:good", "external_id": "1"}]

        jobs, errors, read, seen = discovery.collect_boards(boards, fetch=fetch, pause=lambda s: None)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(read, {"Lever:good"})
        self.assertEqual(len(errors), 1)


class ShortlistTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "s.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def scored(self, title, score, status="Saved"):
        job_id = db.add_job(self.conn, title, "Co")
        db.save_fit_analysis(self.conn, job_id, {"score": score, "verdict": "x"})
        if status != "Saved":
            db.update_status(self.conn, job_id, status)
        return job_id

    def test_ranks_best_first_with_recommendations(self):
        self.scored("Low", 50)
        self.scored("High", 90)
        self.scored("Mid", 75)
        ranked = db.shortlist(self.conn)
        self.assertEqual([j["title"] for j in ranked], ["High", "Mid", "Low"])
        self.assertEqual([j["recommendation"] for j in ranked], ["Prioritize", "Review", "Low priority"])

    def test_shortlist_excludes_unscored_and_finished_jobs(self):
        db.add_job(self.conn, "Unscored", "Co")
        self.scored("Applied already", 95, status="Submitted")
        self.scored("Rejected", 95, status="Rejected")
        self.scored("Still open", 80)
        self.assertEqual([j["title"] for j in db.shortlist(self.conn)], ["Still open"])


class PageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.dict(
            os.environ,
            {
                db.DB_PATH_ENV_VAR: str(Path(self._tmp.name) / "ui.db"),
                profile_store.DOCS_DIR_ENV_VAR: str(Path(self._tmp.name) / "documents"),
            },
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

if __name__ == "__main__":
    unittest.main()
