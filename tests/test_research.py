"""Tests for company research and contacts (Stage 7).

These tests use fake responses, so they make no network calls or API credits.

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

GOOD_RESEARCH = {
    "summary": "A global technology distributor.",
    "key_facts": ["Operates in many countries"],
    "recent_news": ["Patent award announced (2026-10-12)"],
    "unknowns": ["Current hiring plans"],
}


def citation(url, title="Source"):
    return SimpleNamespace(type="url_citation", url=url, title=title)


def fake_response(text, annotations=(), search=True):
    """Build an object shaped like a Responses API result that used web search."""
    message = SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="output_text", text=text, annotations=list(annotations))],
    )
    items = [SimpleNamespace(type="web_search_call")] if search else []
    return SimpleNamespace(output_text=text, output=[*items, message])


class FakeResponses:
    """Returns the given responses in order, one per create() call, and records the calls."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.queue.pop(0)


class FakeClient:
    def __init__(self, *responses):
        self.responses = FakeResponses(*responses)


class SourceExtractionTests(unittest.TestCase):
    def test_collects_url_citations_from_message(self):
        response = fake_response(
            "{}", [citation("https://example.com/a", "A"), citation("https://example.com/b", "B")]
        )
        sources = ai.extract_sources(response)
        self.assertEqual(
            sources,
            [
                {"title": "A", "url": "https://example.com/a"},
                {"title": "B", "url": "https://example.com/b"},
            ],
        )

    def test_removes_duplicates_and_tracking_parameters(self):
        response = fake_response(
            "{}",
            [
                citation("https://example.com/news?id=1&utm_source=openai", "News"),
                citation("https://example.com/news?id=1", "News again"),
            ],
        )
        self.assertEqual(
            ai.extract_sources(response),
            [{"title": "News", "url": "https://example.com/news?id=1"}],
        )

    def test_ignores_non_web_links_and_other_annotations(self):
        response = fake_response(
            "{}",
            [
                citation("javascript:alert(1)", "Bad"),
                SimpleNamespace(type="file_citation", url="https://example.com/x", title="X"),
            ],
        )
        self.assertEqual(ai.extract_sources(response), [])

    def test_no_output_gives_no_sources(self):
        self.assertEqual(ai.extract_sources(SimpleNamespace(output=[])), [])


class ResearchTests(unittest.TestCase):
    def test_search_step_uses_tool_and_structuring_step_uses_no_tools(self):
        client = FakeClient(
            fake_response("Briefing text", [citation("https://ir.example.com/press", "Press")]),
            fake_response(json.dumps(GOOD_RESEARCH), search=False),
        )
        result = ai.research_company(client, "Acme", "Director of Product", model="test-model")
        search_call, structure_call = client.responses.calls
        self.assertEqual(search_call["tools"], [{"type": "web_search"}])
        self.assertEqual(search_call["tool_choice"], "required")
        self.assertEqual(search_call["model"], "test-model")
        self.assertIn("Company: Acme", search_call["input"])
        self.assertNotIn("tools", structure_call)
        self.assertEqual(structure_call["input"], "Briefing text")
        self.assertTrue(structure_call["text"]["format"]["strict"])
        self.assertEqual(result["summary"], GOOD_RESEARCH["summary"])
        self.assertEqual(result["sources"], [{"title": "Press", "url": "https://ir.example.com/press"}])

    def test_strips_inline_citation_markup_from_text(self):
        text = {
            **GOOD_RESEARCH,
            "recent_news": ["Deal announced ([itpro.com](https://www.itpro.com/x?utm_source=openai))"],
        }
        client = FakeClient(
            fake_response("Briefing", [citation("https://www.itpro.com/x", "ITPro")]),
            fake_response(json.dumps(text), search=False),
        )
        result = ai.research_company(client, "Acme", "Role")
        self.assertEqual(result["recent_news"], ["Deal announced"])
        self.assertEqual(result["sources"][0]["url"], "https://www.itpro.com/x")

    def test_no_citations_gives_empty_sources_not_an_error(self):
        client = FakeClient(
            fake_response("Briefing", []),
            fake_response(json.dumps(GOOD_RESEARCH), search=False),
        )
        self.assertEqual(ai.research_company(client, "Acme", "Role")["sources"], [])

    def test_rejects_empty_summary(self):
        bad = {**GOOD_RESEARCH, "summary": "  "}
        with self.assertRaises(ValueError):
            ai.validate_research(bad)

    def test_rejects_non_list_fields(self):
        bad = {**GOOD_RESEARCH, "key_facts": "one string"}
        with self.assertRaises(ValueError):
            ai.validate_research(bad)


class StorageTests(unittest.TestCase):
    def test_research_round_trips_per_job(self):
        with tempfile.TemporaryDirectory() as tmp, closing(db.connect(Path(tmp) / "r.db")) as conn:
            job_id = db.add_job(conn, "Role", "Acme")
            other_id = db.add_job(conn, "Other", "Beta")
            db.save_research(conn, job_id, GOOD_RESEARCH)
            history = db.list_research(conn, job_id)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["research"], GOOD_RESEARCH)
            self.assertEqual(db.list_research(conn, other_id), [])

    def test_contacts_can_be_linked_to_a_job_or_stand_alone(self):
        with tempfile.TemporaryDirectory() as tmp, closing(db.connect(Path(tmp) / "c.db")) as conn:
            job_id = db.add_job(conn, "Role", "Acme")
            db.add_contact(conn, "Linked Person", job_id=job_id)
            db.add_contact(conn, "Standalone Person")
            self.assertEqual([c["name"] for c in db.list_contacts(conn, job_id)], ["Linked Person"])
            self.assertEqual(len(db.list_contacts(conn)), 2)

    def test_deleting_a_job_keeps_its_contacts_unlinked(self):
        with tempfile.TemporaryDirectory() as tmp, closing(db.connect(Path(tmp) / "c.db")) as conn:
            job_id = db.add_job(conn, "Role", "Acme")
            db.add_contact(conn, "Keep Me", job_id=job_id)
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
            contact = db.list_contacts(conn)[0]
            self.assertEqual(contact["name"], "Keep Me")
            self.assertIsNone(contact["job_id"])


class ResearchPageTests(unittest.TestCase):
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

    def test_research_without_api_key_shows_setup_message(self):
        with closing(db.connect()) as conn:
            job_id = db.add_job(conn, "Role", "Acme")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            at = self.run_app()
            next(b for b in at.button if b.label == "Research company").click().run()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(any("OPENAI_API_KEY is not set" in e.value for e in at.error))
        with closing(db.connect()) as conn:
            self.assertEqual(db.list_research(conn, job_id), [])

    def test_contact_form_saves_contact_without_lookup(self):
        at = self.run_app()
        next(t for t in at.text_input if t.label == "Name *").input("Hiring Manager Name")
        next(b for b in at.button if b.label == "Save contact").click().run()
        self.assertEqual(len(at.exception), 0)
        with closing(db.connect()) as conn:
            self.assertEqual(db.list_contacts(conn)[0]["name"], "Hiring Manager Name")

    def test_contact_name_is_required(self):
        at = self.run_app()
        next(b for b in at.button if b.label == "Save contact").click().run()
        self.assertTrue(any("Name is required" in e.value for e in at.error))


if __name__ == "__main__":
    unittest.main()
