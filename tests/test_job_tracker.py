"""Tests for the job tracker: database layer and Streamlit page.

Run from the project root with:
    python -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import db  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "nested" / "test.db"
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_connect_creates_folder_and_table(self):
        self.assertTrue(self.path.exists())
        tables = [
            row[0]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        self.assertIn("jobs", tables)

    def test_add_job_defaults_to_saved_and_returns_id(self):
        job_id = db.add_job(self.conn, "Director of Product", "Acme", location="Bengaluru")
        jobs = db.list_jobs(self.conn)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], job_id)
        self.assertEqual(jobs[0]["status"], "Saved")
        self.assertEqual(jobs[0]["location"], "Bengaluru")
        self.assertIsNone(jobs[0]["url"])

    def test_list_jobs_filters_by_status(self):
        first = db.add_job(self.conn, "Role A", "Co A")
        db.add_job(self.conn, "Role B", "Co B")
        db.update_status(self.conn, first, "Submitted")
        applied = db.list_jobs(self.conn, status="Submitted")
        self.assertEqual([job["title"] for job in applied], ["Role A"])

    def test_update_status_changes_status_and_updated_at(self):
        job_id = db.add_job(self.conn, "Role", "Co")
        db.update_status(self.conn, job_id, "Interviewing")
        job = db.list_jobs(self.conn)[0]
        self.assertEqual(job["status"], "Interviewing")
        self.assertIsNotNone(job["updated_at"])

    def test_update_status_rejects_unknown_status(self):
        job_id = db.add_job(self.conn, "Role", "Co")
        with self.assertRaises(ValueError):
            db.update_status(self.conn, job_id, "Ghosted")

    def test_database_rejects_invalid_status_even_without_python_check(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO jobs (title, company, status) VALUES ('R', 'C', 'Bogus')"
            )

    def test_user_input_is_stored_literally(self):
        tricky = "Robert'); DROP TABLE jobs;--"
        db.add_job(self.conn, tricky, "Co")
        self.assertEqual(db.list_jobs(self.conn)[0]["title"], tricky)
        self.assertEqual(len(db.list_jobs(self.conn)), 1)

    def test_init_db_is_idempotent_and_keeps_data(self):
        db.add_job(self.conn, "Role", "Co")
        db.init_db(self.conn)
        self.assertEqual(len(db.list_jobs(self.conn)), 1)




if __name__ == "__main__":
    unittest.main()
