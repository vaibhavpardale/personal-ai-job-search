"""Tests for the daily review queue (Stage 10).

These tests use a fake AI client and fake job-board responses. They make no network calls.
Nothing here submits or sends anything.

Run from the project root with:
    python -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import ai  # noqa: E402
import db  # noqa: E402
import discovery  # noqa: E402
import profile_store  # noqa: E402
import review_queue  # noqa: E402
import run_daily  # noqa: E402

from docx import Document  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

BOARD_DATA = {
    "jobs": [
        {
            "id": 101,
            "title": "Director of Product, Platform",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
            "location": {"name": "Bengaluru"},
            "content": "&lt;p&gt;Own the API platform roadmap.&lt;/p&gt;",
        },
        {
            "id": 102,
            "title": "Principal Product Manager, Payments",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/102",
            "location": {"name": "Remote"},
            "content": "&lt;p&gt;Payments roadmap.&lt;/p&gt;",
        },
    ]
}

BOARDS = [{"provider": "Greenhouse", "token": "acme", "label": "Acme"}]


def analysis(score):
    verdict = "Strong fit" if score >= 70 else "Weak fit"
    return {
        "score": score,
        "verdict": verdict,
        "strengths": ["Platform product background"],
        "gaps": ["No payments experience stated"],
        "summary": f"Scored {score}.",
    }


def tailored():
    return {
        "headline": "Principal Technical Product Manager",
        "summary": "Product leader for enterprise platforms.",
        "bullets": ["Led an event-driven platform roadmap"],
        "cover_letter": "Dear Hiring Manager, I am writing about the role.",
        "unsupported_requirements": ["Payments domain experience"],
        "items_to_confirm": ["Rephrased platform bullet"],
    }


class RoutingResponses:
    """Answers each request by its schema name, using a queue of replies per name."""

    def __init__(self, replies):
        self.replies = {name: list(values) for name, values in replies.items()}
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        name = kwargs["text"]["format"]["name"]
        reply = self.replies[name].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(output_text=json.dumps(reply))


class FakeClient:
    def __init__(self, **replies):
        self.responses = RoutingResponses(replies)


def fake_fetch(provider, token, label):
    return discovery.normalize_greenhouse(token, label, BOARD_DATA)


class DailyRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "queue"
        self.conn = db.connect(Path(self._tmp.name) / "q.db")
        db.add_watched_board(self.conn, "Greenhouse", "acme", "Acme")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def run_once(self, client, **kwargs):
        return review_queue.run_daily(
            self.conn,
            db.list_watched_boards(self.conn),
            ["product"],
            client,
            profile_store.SEED_PROFILE,
            "Résumé text",
            fetch=fake_fetch,
            pause=lambda s: None,
            root=self.root,
            **kwargs,
        )

    def test_strong_match_is_queued_with_files_and_weak_match_is_not(self):
        client = FakeClient(job_fit=[analysis(85), analysis(40)], tailored_application=[tailored()])
        summary = self.run_once(client)
        self.assertEqual(summary["found"], 2)
        self.assertEqual(summary["new"], 2)
        self.assertEqual(summary["analyzed"], 2)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["errors"], [])

        item = db.list_queue(self.conn, "Pending")[0]
        self.assertEqual(item["fit_score"], 85)
        folder = Path(item["output_dir"])
        self.assertTrue((folder / "tailored_resume.docx").exists())
        self.assertIn("Dear Hiring Manager", (folder / "cover_letter.txt").read_text(encoding="utf-8"))
        notes = (folder / "review_notes.txt").read_text(encoding="utf-8")
        self.assertIn("Payments domain experience", notes)
        self.assertIn("Rephrased platform bullet", notes)

    def test_resume_docx_is_readable_and_contains_tailored_text(self):
        client = FakeClient(job_fit=[analysis(90), analysis(90)], tailored_application=[tailored(), tailored()])
        self.run_once(client)
        docx = Path(db.list_queue(self.conn, "Pending")[0]["output_dir"]) / "tailored_resume.docx"
        text = "\n".join(p.text for p in Document(docx).paragraphs)
        self.assertIn("Principal Technical Product Manager", text)
        self.assertIn("Led an event-driven platform roadmap", text)

    def test_rerun_does_not_analyze_or_queue_again(self):
        client = FakeClient(job_fit=[analysis(85), analysis(40)], tailored_application=[tailored()])
        self.run_once(client)
        second = self.run_once(FakeClient())
        self.assertEqual(second["new"], 0)
        self.assertEqual(second["analyzed"], 0)
        self.assertEqual(len(db.list_queue(self.conn)), 1)

    def test_analysis_cap_limits_ai_calls_and_leaves_the_rest_for_next_run(self):
        self.add_extra_postings()
        client = FakeClient(job_fit=[analysis(40), analysis(40)])
        summary = self.run_once(client, max_analyses=2)
        self.assertEqual(summary["analyzed"], 2)
        self.assertEqual(len(db.jobs_needing_fit(self.conn)), 2)

    def test_one_failing_posting_does_not_stop_the_run(self):
        client = FakeClient(
            job_fit=[RuntimeError("quota exceeded"), analysis(85)],
            tailored_application=[tailored()],
        )
        summary = self.run_once(client)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertIn("quota exceeded", summary["errors"][0])

    def add_extra_postings(self):
        """Save two more postings already in the tracker, so the run has four candidates."""
        extra = {"jobs": [
            {"id": 201, "title": "Director of Product, Growth", "absolute_url": "u1", "location": None, "content": "x"},
            {"id": 202, "title": "Head of Product, Data", "absolute_url": "u2", "location": None, "content": "y"},
        ]}
        for job in discovery.normalize_greenhouse("acme", "Acme", extra):
            db.import_discovered_job(self.conn, job)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "d.db")
        db.import_discovered_job(
            self.conn,
            {"title": "Role", "company": "Acme", "source": "Greenhouse:acme", "external_id": "1"},
        )
        self.job_id = db.find_job_id(self.conn, "Greenhouse:acme", "1")
        self.queue_id = db.add_queue_item(self.conn, self.job_id, 80, "Strong fit", Path(self._tmp.name))

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_approve_records_decision_without_changing_job_status(self):
        db.set_queue_status(self.conn, self.queue_id, "Approved")
        self.assertEqual(len(db.list_queue(self.conn, "Approved")), 1)
        self.assertEqual(db.list_queue(self.conn, "Pending"), [])
        self.assertEqual(db.get_job(self.conn, self.job_id)["status"], "Saved")

    def test_reject_removes_item_from_pending(self):
        db.set_queue_status(self.conn, self.queue_id, "Rejected")
        self.assertEqual(db.list_queue(self.conn, "Pending"), [])

    def test_unknown_decision_is_rejected(self):
        with self.assertRaises(ValueError):
            db.set_queue_status(self.conn, self.queue_id, "Submitted")

    def test_a_job_can_only_be_queued_once(self):
        with self.assertRaises(Exception):
            db.add_queue_item(self.conn, self.job_id, 90, "Strong fit", Path("."))


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.dict(
            os.environ,
            {
                db.DB_PATH_ENV_VAR: str(Path(self._tmp.name) / "cli.db"),
                profile_store.DOCS_DIR_ENV_VAR: str(Path(self._tmp.name) / "documents"),
            },
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_without_api_key_it_exits_with_an_error(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(run_daily.main([]), 1)

    def test_empty_watchlist_exits_with_an_error(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            self.assertEqual(run_daily.main([]), 1)


class QueuePageTests(unittest.TestCase):
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

    def test_queue_page_renders_and_explains_nothing_is_submitted(self):
        at = AppTest.from_file(str(APP_DIR / "main.py"), default_timeout=30).run()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(any("Nothing is submitted" in c.value for c in at.caption))

    def test_queue_page_without_boards_asks_for_watchlist(self):
        at = AppTest.from_file(str(APP_DIR / "main.py"), default_timeout=30).run()
        self.assertTrue(any("Add company boards" in i.value for i in at.info))


if __name__ == "__main__":
    unittest.main()
