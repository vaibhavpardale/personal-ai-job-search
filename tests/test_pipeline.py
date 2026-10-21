"""Tests for the automated pipeline: per-job PDF résumés, saved settings, and the Jobs page.

Uses fake AI responses. Makes no network calls and writes only to temporary folders.

Run from the project root with:
    python -m unittest discover -s tests -v
"""

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
import profile_store  # noqa: E402
import resume_pdf  # noqa: E402
import run_daily  # noqa: E402
import settings  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

ROLE = {"company": "Acme", "title": "Principal Product Manager", "started_on": "Jan 2020",
        "finished_on": None, "description": "Owned the API roadmap."}

GOOD_TAILOR = {
    "headline": "Principal Product Manager",
    "summary": "Product leader for enterprise platforms.",
    "skills": ["APIs", "Roadmapping"],
    "experience": [{"company": "Acme", "title": "Principal Product Manager",
                    "bullets": ["Led the event-driven platform roadmap"]}],
    "cover_letter": "Dear Hiring Manager, I am writing about the role.",
    "unsupported_requirements": ["Payments domain experience"],
    "items_to_confirm": [],
}


class FakeResponses:
    def __init__(self, output_text):
        self.output_text = output_text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output_text)


class FakeClient:
    def __init__(self, output_text):
        self.responses = FakeResponses(output_text)


def job():
    return {"title": "Director of Product", "company": "Acme", "location": "Bengaluru",
            "description": "Own the API platform roadmap."}


class TailorRuleTests(unittest.TestCase):
    def test_roles_must_come_from_the_experience_list(self):
        invented = {**GOOD_TAILOR, "experience": [{"company": "Globex", "title": "CEO", "bullets": ["x"]}]}
        profile = {**profile_store.SEED_PROFILE, "experience": [ROLE]}
        with self.assertRaises(ValueError):
            ai.tailor_application(FakeClient(json.dumps(invented)), profile, job(), "résumé")

    def test_known_role_passes_and_stays_unflagged(self):
        profile = {**profile_store.SEED_PROFILE, "experience": [ROLE]}
        result = ai.tailor_application(FakeClient(json.dumps(GOOD_TAILOR)), profile, job(), "résumé")
        self.assertEqual(result["items_to_confirm"], [])

    def test_without_an_experience_list_the_draft_asks_you_to_verify_roles(self):
        result = ai.tailor_application(
            FakeClient(json.dumps(GOOD_TAILOR)), profile_store.SEED_PROFILE, job(), "résumé"
        )
        self.assertTrue(any("not checked" in item for item in result["items_to_confirm"]))

    def test_role_without_bullets_is_rejected(self):
        bad = {**GOOD_TAILOR, "experience": [{"company": "Acme", "title": "Principal Product Manager", "bullets": []}]}
        with self.assertRaises(ValueError):
            ai.validate_tailor(bad)

    def test_empty_experience_is_rejected(self):
        with self.assertRaises(ValueError):
            ai.validate_tailor({**GOOD_TAILOR, "experience": []})

    def test_prompt_forbids_inventing_roles(self):
        self.assertIn("Use the company and title exactly", ai.TAILOR_INSTRUCTIONS)


class ResumePdfTests(unittest.TestCase):
    def test_pdf_contains_name_summary_skills_and_roles(self):
        story = resume_pdf.build_resume_story(GOOD_TAILOR, {**profile_store.SEED_PROFILE, "full_name": "Test Person"})
        text = " ".join(getattr(flowable, "text", "") for flowable in story)
        self.assertIn("Test Person", text)
        self.assertIn("Product leader for enterprise platforms.", text)
        self.assertIn("APIs, Roadmapping", text)
        self.assertIn("Principal Product Manager, Acme", text)

    def test_missing_name_and_education_are_left_out(self):
        story = resume_pdf.build_resume_story(GOOD_TAILOR, {**profile_store.SEED_PROFILE, "full_name": "",
                                                            "education": ""})
        text = " ".join(getattr(flowable, "text", "") for flowable in story)
        self.assertNotIn("Education", text)

    def test_special_characters_do_not_break_the_pdf(self):
        tricky = {**GOOD_TAILOR, "summary": "R&D <b>bold</b> & more"}
        with tempfile.TemporaryDirectory() as tmp:
            path = resume_pdf.write_resume_pdf(tricky, profile_store.SEED_PROFILE, Path(tmp) / "r.pdf")
            self.assertTrue(path.read_bytes().startswith(b"%PDF-"))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "s.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_defaults_are_used_until_saved(self):
        self.assertEqual(settings.load(self.conn)["min_score"], 70)
        self.assertIn("director of product", settings.load(self.conn)["keywords"])

    def test_saved_values_come_back_with_their_types(self):
        settings.save(self.conn, {"min_score": 80, "max_analyses": 5, "keywords": "head of product"})
        loaded = settings.load(self.conn)
        self.assertEqual((loaded["min_score"], loaded["max_analyses"], loaded["keywords"]),
                         (80, 5, "head of product"))

    def test_unknown_setting_is_rejected(self):
        with self.assertRaises(ValueError):
            settings.save(self.conn, {"auto_submit": "yes"})


class ScheduledRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.dict(os.environ, {db.DB_PATH_ENV_VAR: str(Path(self._tmp.name) / "r.db")})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_without_a_key_the_scheduled_run_stops_with_an_error(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(run_daily.main([]), 1)

    def test_with_no_boards_the_scheduled_run_stops_with_an_error(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            self.assertEqual(run_daily.main([]), 1)


class PagesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.dict(
            os.environ,
            {
                db.DB_PATH_ENV_VAR: str(Path(self._tmp.name) / "ui.db"),
                profile_store.DOCS_DIR_ENV_VAR: str(Path(self._tmp.name) / "documents"),
                "OPENAI_API_KEY": "",
            },
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def run_app(self):
        return AppTest.from_file(str(APP_DIR / "main.py"), default_timeout=30).run()

    def test_only_three_pages_remain(self):
        at = self.run_app()
        self.assertEqual(len(at.exception), 0)
        self.assertEqual([t.label for t in at.tabs], ["Jobs", "Review", "Setup"])

    def test_removed_manual_features_are_gone(self):
        at = self.run_app()
        labels = [b.label for b in at.button]
        for removed in ("Save job", "Generate tailored draft", "Research company", "Analyze fit",
                        "Prepare interview questions", "Add event", "Not interested"):
            self.assertNotIn(removed, labels)

    def test_jobs_page_offers_a_check_and_says_nothing_is_submitted(self):
        at = self.run_app()
        self.assertIn("Run check now", [b.label for b in at.button])
        self.assertTrue(any("never submits" in c.value or "schedule" in c.value for c in at.caption))

    def test_setup_page_has_search_settings(self):
        at = self.run_app()
        labels = [t.label for t in at.text_area] + [t.label for t in at.text_input]
        self.assertIn("Title keywords (comma-separated)", labels)
        self.assertIn("Save settings", [b.label for b in at.button])

    def test_saving_search_settings_through_the_page_persists_them(self):
        at = self.run_app()
        next(t for t in at.text_area if t.label == "Title keywords (comma-separated)").input("head of product")
        next(b for b in at.button if b.label == "Save settings").click().run()
        self.assertEqual(len(at.exception), 0)
        with closing(db.connect()) as conn:
            self.assertEqual(settings.load(conn)["keywords"], "head of product")


if __name__ == "__main__":
    unittest.main()
