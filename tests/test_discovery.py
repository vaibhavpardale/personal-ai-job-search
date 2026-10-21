"""Tests for job discovery from public job boards (Stage 9).

These tests make no network calls. HTTP is replaced by fakes.

Run from the project root with:
    python -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
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

GREENHOUSE_DATA = {
    "jobs": [
        {
            "id": 101,
            "title": "Director of Product, Platform",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
            "location": {"name": "Bengaluru, India"},
            "content": "&lt;p&gt;Own the &lt;b&gt;API platform&lt;/b&gt; roadmap.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Lead teams&lt;/li&gt;&lt;/ul&gt;",
        },
        {
            "id": 102,
            "title": "Warehouse Associate",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/102",
            "location": None,
            "content": "",
        },
    ]
}

LEVER_DATA = [
    {
        "id": "abc-1",
        "text": "Principal Product Manager",
        "hostedUrl": "https://jobs.lever.co/beta/abc-1",
        "categories": {"location": "Remote - India", "team": "Product"},
        "descriptionPlain": "Shape the developer platform.",
        "description": "<p>ignored when plain text exists</p>",
    }
]


class FakeHttpResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NormalizeTests(unittest.TestCase):
    def test_strip_html_turns_markup_into_readable_lines(self):
        text = discovery.strip_html("&lt;p&gt;Own the &lt;b&gt;API&lt;/b&gt; roadmap.&lt;/p&gt;<ul><li>Lead teams</li></ul>")
        self.assertEqual(text, "Own the API roadmap.\nLead teams")

    def test_strip_html_handles_empty_input(self):
        self.assertEqual(discovery.strip_html(None), "")
        self.assertEqual(discovery.strip_html(""), "")

    def test_greenhouse_postings_are_normalized(self):
        jobs = discovery.normalize_greenhouse("acme", "Acme Corp", GREENHOUSE_DATA)
        self.assertEqual(len(jobs), 2)
        first = jobs[0]
        self.assertEqual(first["title"], "Director of Product, Platform")
        self.assertEqual(first["company"], "Acme Corp")
        self.assertEqual(first["location"], "Bengaluru, India")
        self.assertEqual(first["source"], "Greenhouse:acme")
        self.assertEqual(first["external_id"], "101")
        self.assertIn("Own the API platform roadmap.", first["description"])
        self.assertIsNone(jobs[1]["location"])
        self.assertIsNone(jobs[1]["description"])

    def test_lever_postings_are_normalized_and_prefer_plain_text(self):
        jobs = discovery.normalize_lever("beta", "Beta Inc", LEVER_DATA)
        job = jobs[0]
        self.assertEqual(job["title"], "Principal Product Manager")
        self.assertEqual(job["location"], "Remote - India")
        self.assertEqual(job["url"], "https://jobs.lever.co/beta/abc-1")
        self.assertEqual(job["description"], "Shape the developer platform.")
        self.assertEqual(job["source"], "Lever:beta")
        self.assertEqual(job["external_id"], "abc-1")


class KeywordTests(unittest.TestCase):
    def test_parse_keywords_cleans_and_lowercases(self):
        self.assertEqual(discovery.parse_keywords(" Product, AI ,, "), ["product", "ai"])
        self.assertEqual(discovery.parse_keywords(""), [])

    def test_filter_keeps_matching_titles_case_insensitively(self):
        jobs = [{"title": "Director of PRODUCT"}, {"title": "Warehouse Associate"}]
        self.assertEqual(discovery.filter_by_keywords(jobs, ["product"]), [jobs[0]])

    def test_empty_keywords_keep_everything(self):
        jobs = [{"title": "Anything"}]
        self.assertEqual(discovery.filter_by_keywords(jobs, []), jobs)


class FetchTests(unittest.TestCase):
    def test_fetch_json_parses_a_successful_response(self):
        body = json.dumps({"ok": True}).encode()
        with mock.patch("discovery.urllib.request.urlopen", return_value=FakeHttpResponse(body)):
            self.assertEqual(discovery.fetch_json("https://example.test"), {"ok": True})

    def test_fetch_json_sends_a_user_agent(self):
        with mock.patch("discovery.urllib.request.urlopen", return_value=FakeHttpResponse(b"[]")) as opener:
            discovery.fetch_json("https://example.test")
        request = opener.call_args.args[0]
        self.assertIn("personal-job-search", request.get_header("User-agent"))

    def test_missing_board_gives_readable_error(self):
        error = urllib.error.HTTPError("https://x", 404, "Not Found", {}, None)
        with mock.patch("discovery.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(discovery.DiscoveryError) as ctx:
                discovery.fetch_json("https://x")
        self.assertIn("Board not found", str(ctx.exception))

    def test_network_failure_gives_readable_error(self):
        with mock.patch("discovery.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(discovery.DiscoveryError) as ctx:
                discovery.fetch_json("https://x")
        self.assertIn("Could not reach", str(ctx.exception))

    def test_non_json_response_gives_readable_error(self):
        with mock.patch("discovery.urllib.request.urlopen", return_value=FakeHttpResponse(b"<html>")):
            with self.assertRaises(discovery.DiscoveryError):
                discovery.fetch_json("https://x")

    def test_fetch_board_routes_to_the_right_api(self):
        seen = []

        def fake_fetch(url):
            seen.append(url)
            return GREENHOUSE_DATA if "greenhouse" in url else LEVER_DATA

        discovery.fetch_board("Greenhouse", "acme", "Acme", fetch=fake_fetch)
        discovery.fetch_board("Lever", "beta", "Beta", fetch=fake_fetch)
        self.assertEqual(
            seen,
            [
                "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
                "https://api.lever.co/v0/postings/beta?mode=json",
            ],
        )


class DiscoverTests(unittest.TestCase):
    def test_one_failing_board_does_not_stop_the_others(self):
        boards = [
            {"provider": "Greenhouse", "token": "acme", "label": "Acme"},
            {"provider": "Lever", "token": "missing", "label": "Missing Co"},
            {"provider": "Lever", "token": "beta", "label": "Beta"},
        ]

        def fake_fetch(provider, token, label):
            if token == "missing":
                raise discovery.DiscoveryError("Board not found. Check the board name.")
            return discovery.normalize_greenhouse("acme", label, GREENHOUSE_DATA) if token == "acme" \
                else discovery.normalize_lever("beta", label, LEVER_DATA)

        pauses = []
        jobs, errors = discovery.discover(boards, [], fetch=fake_fetch, pause=pauses.append)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(errors, ["Missing Co (Lever): Board not found. Check the board name."])
        self.assertEqual(len(pauses), 2)

    def test_keywords_apply_to_discovered_jobs(self):
        boards = [{"provider": "Greenhouse", "token": "acme", "label": "Acme"}]
        fake_fetch = lambda p, t, l: discovery.normalize_greenhouse("acme", l, GREENHOUSE_DATA)  # noqa: E731
        jobs, errors = discovery.discover(boards, ["product"], fetch=fake_fetch, pause=lambda s: None)
        self.assertEqual([j["title"] for j in jobs], ["Director of Product, Platform"])
        self.assertEqual(errors, [])


class WatchlistStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "w.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_add_list_and_remove_boards(self):
        board_id = db.add_watched_board(self.conn, "Greenhouse", " acme ", "Acme")
        boards = db.list_watched_boards(self.conn)
        self.assertEqual(boards[0]["token"], "acme")
        db.remove_watched_board(self.conn, board_id)
        self.assertEqual(db.list_watched_boards(self.conn), [])

    def test_same_board_twice_is_ignored(self):
        self.assertIsNotNone(db.add_watched_board(self.conn, "Lever", "beta", "Beta"))
        self.assertIsNone(db.add_watched_board(self.conn, "Lever", "beta", "Beta again"))
        self.assertEqual(len(db.list_watched_boards(self.conn)), 1)

    def test_board_provider_is_restricted(self):
        with self.assertRaises(ValueError):
            db.add_watched_board(self.conn, "Indeed", "x", "X")
        self.assertEqual(db.list_watched_boards(self.conn), [])


class ImportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "i.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_importing_the_same_posting_twice_saves_it_once(self):
        job = discovery.normalize_lever("beta", "Beta Inc", LEVER_DATA)[0]
        self.assertEqual(db.import_discovered_job(self.conn, job), "new")
        self.assertEqual(db.import_discovered_job(self.conn, job), "existing")
        self.assertEqual(len(db.list_jobs(self.conn)), 1)

    def test_imported_job_keeps_source_and_description(self):
        job = discovery.normalize_lever("beta", "Beta Inc", LEVER_DATA)[0]
        db.import_discovered_job(self.conn, job)
        saved = db.list_jobs(self.conn)[0]
        self.assertEqual(saved["status"], "Saved")
        self.assertEqual(saved["source"], "Lever:beta")
        self.assertEqual(saved["description"], "Shape the developer platform.")
        self.assertIn(("Lever:beta", "abc-1"), db.saved_external_keys(self.conn))

    def test_manual_jobs_are_not_treated_as_duplicates(self):
        db.add_job(self.conn, "Same", "Co")
        db.add_job(self.conn, "Same", "Co")
        self.assertEqual(len(db.list_jobs(self.conn)), 2)


class SetupBoardsPageTests(unittest.TestCase):
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

    def run_app(self):
        return AppTest.from_file(str(APP_DIR / "main.py"), default_timeout=30).run()

    def test_empty_watchlist_says_boards_are_needed(self):
        at = self.run_app()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(any("No boards yet" in i.value for i in at.info))

    def test_adding_a_board_through_the_setup_page_saves_it(self):
        at = self.run_app()
        next(t for t in at.text_input if t.label.startswith("Board name")).input("acme")
        next(t for t in at.text_input if t.label.startswith("Company name")).input("Acme Corp")
        next(b for b in at.button if b.label == "Add board").click().run()
        self.assertEqual(len(at.exception), 0)
        with closing(db.connect()) as conn:
            boards = db.list_watched_boards(conn)
        self.assertEqual([(b["provider"], b["token"], b["label"]) for b in boards],
                         [("Greenhouse", "acme", "Acme Corp")])

    def test_board_name_is_required(self):
        at = self.run_app()
        next(t for t in at.text_input if t.label.startswith("Company name")).input("Only a label")
        next(b for b in at.button if b.label == "Add board").click().run()
        self.assertTrue(any("required" in e.value for e in at.error))


if __name__ == "__main__":
    unittest.main()
