"""Tests for networking drafts and interview prep (Stage 8).

These tests use a fake client, so they make no API calls. Nothing in the app sends messages;
these tests check drafting and storage only.

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

from streamlit.testing.v1 import AppTest  # noqa: E402

GOOD_OUTREACH = {
    "subject": "Question about the Director of Product role",
    "message": "Hello, I am exploring the role at Acme. Would you be open to a short conversation?",
    "items_to_confirm": ["Reason for contacting them is inferred"],
}

GOOD_PREP = {
    "likely_questions": [
        {
            "question": "Tell me about a platform roadmap you owned.",
            "why_asked": "The role owns an API platform roadmap.",
            "answer_from_profile": "Led an event-driven platform roadmap as a Principal PM.",
        }
    ],
    "gaps_to_prepare_for": ["Fintech payments experience"],
    "questions_to_ask_them": ["How is success measured for the platform in year one?"],
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


def sample_job():
    return {
        "title": "Director of Product",
        "company": "Acme",
        "location": "Bengaluru",
        "description": "Own the API platform roadmap.",
    }


def sample_contact(**overrides):
    contact = {"name": "Jordan Example", "role": "Engineering Manager", "company": "Acme", "notes": ""}
    contact.update(overrides)
    return contact


class OutreachTests(unittest.TestCase):
    def test_sends_contact_details_and_kind_and_returns_message(self):
        client = FakeClient(json.dumps(GOOD_OUTREACH))
        result = ai.draft_outreach(
            client, profile_store.SEED_PROFILE, sample_job(), sample_contact(), "Referral request"
        )
        self.assertEqual(result["message"], GOOD_OUTREACH["message"])
        call = client.responses.calls[0]
        self.assertIn("Message kind: Referral request", call["input"])
        self.assertIn("Jordan Example", call["input"])
        self.assertIn("Do not claim any shared history", call["instructions"])
        self.assertTrue(call["text"]["format"]["strict"])

    def test_unknown_kind_is_rejected_before_calling_the_model(self):
        client = FakeClient(json.dumps(GOOD_OUTREACH))
        with self.assertRaises(ValueError):
            ai.draft_outreach(client, profile_store.SEED_PROFILE, sample_job(), sample_contact(), "Cold pitch")
        self.assertEqual(client.responses.calls, [])

    def test_empty_message_is_rejected(self):
        bad = {**GOOD_OUTREACH, "message": "  "}
        with self.assertRaises(ValueError):
            ai.draft_outreach(
                FakeClient(json.dumps(bad)), profile_store.SEED_PROFILE, sample_job(), sample_contact(),
                "Networking message",
            )


class InterviewPrepTests(unittest.TestCase):
    def test_returns_prep_and_instructs_evidence_based_answers(self):
        client = FakeClient(json.dumps(GOOD_PREP))
        result = ai.interview_prep(client, profile_store.SEED_PROFILE, sample_job())
        self.assertEqual(result, GOOD_PREP)
        self.assertIn("Do not add scope", client.responses.calls[0]["instructions"])
        self.assertIn("No evidence in profile or résumé", client.responses.calls[0]["instructions"])

    def test_resume_text_is_included_in_the_input(self):
        client = FakeClient(json.dumps(GOOD_PREP))
        ai.interview_prep(client, profile_store.SEED_PROFILE, sample_job(), "Led a platform team of 4")
        self.assertIn("Led a platform team of 4", client.responses.calls[0]["input"])

    def test_empty_question_list_is_rejected(self):
        bad = {**GOOD_PREP, "likely_questions": []}
        with self.assertRaises(ValueError):
            ai.interview_prep(FakeClient(json.dumps(bad)), profile_store.SEED_PROFILE, sample_job())

    def test_render_prep_includes_all_sections(self):
        text = ai.render_prep(GOOD_PREP)
        self.assertIn("1. Tell me about a platform roadmap you owned.", text)
        self.assertIn("Answer from your profile: Led an event-driven", text)
        self.assertIn("- Fintech payments experience", text)
        self.assertIn("- How is success measured", text)

    def test_render_prep_says_none_when_no_gaps(self):
        text = ai.render_prep({**GOOD_PREP, "gaps_to_prepare_for": []})
        self.assertIn("GAPS TO PREPARE FOR\n- None identified", text)


class StorageTests(unittest.TestCase):
    def test_prep_drafts_are_saved_per_job_with_optional_contact(self):
        with tempfile.TemporaryDirectory() as tmp, closing(db.connect(Path(tmp) / "p.db")) as conn:
            job_id = db.add_job(conn, "Role", "Acme")
            other_id = db.add_job(conn, "Other", "Beta")
            contact_id = db.add_contact(conn, "Jordan Example", job_id=job_id)
            db.save_prep_draft(conn, job_id, "Referral request", "Subject: Hi", contact_id=contact_id)
            db.save_prep_draft(conn, job_id, "Interview prep", "Prep text")
            drafts = db.list_prep_drafts(conn, job_id)
            self.assertEqual(len(drafts), 2)
            self.assertIn(contact_id, [d["contact_id"] for d in drafts])
            self.assertEqual(db.list_prep_drafts(conn, other_id), [])

    def test_unknown_prep_kind_is_rejected_by_database(self):
        with tempfile.TemporaryDirectory() as tmp, closing(db.connect(Path(tmp) / "p.db")) as conn:
            job_id = db.add_job(conn, "Role", "Acme")
            with self.assertRaises(Exception):
                db.save_prep_draft(conn, job_id, "Cold email", "x")

    def test_deleting_a_job_removes_its_prep_drafts(self):
        with tempfile.TemporaryDirectory() as tmp, closing(db.connect(Path(tmp) / "p.db")) as conn:
            job_id = db.add_job(conn, "Role", "Acme")
            db.save_prep_draft(conn, job_id, "Interview prep", "x")
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM prep_drafts").fetchone()[0], 0)


class OutreachPageTests(unittest.TestCase):
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

    def test_interview_prep_without_api_key_shows_setup_message(self):
        with closing(db.connect()) as conn:
            job_id = db.add_job(conn, "Role", "Acme", description="JD")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            at = self.run_app()
            next(b for b in at.button if b.label == "Prepare interview questions").click().run()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(any("OPENAI_API_KEY is not set" in e.value for e in at.error))
        with closing(db.connect()) as conn:
            self.assertEqual(db.list_prep_drafts(conn, job_id), [])

    def test_page_states_that_nothing_is_sent(self):
        at = self.run_app()
        self.assertTrue(any("Nothing here is sent" in c.value for c in at.caption))


if __name__ == "__main__":
    unittest.main()
