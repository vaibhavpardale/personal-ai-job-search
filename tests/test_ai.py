"""Tests for OpenAI integration and job-fit analysis (Stage 5).

These tests use a fake client, so they make no network calls and use no API credits.
A live check against the real API needs OPENAI_API_KEY in .env.

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
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import ai  # noqa: E402
import db  # noqa: E402
import profile_store  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

GOOD_RESULT = {
    "score": 82,
    "verdict": "Strong fit",
    "strengths": ["Led enterprise platform products"],
    "gaps": ["No direct fintech experience stated"],
    "summary": "Good seniority match with a platform background.",
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


def sample_job(**overrides):
    job = {
        "title": "Director of Product",
        "company": "Acme",
        "location": "Bengaluru",
        "description": "Own the platform roadmap for APIs and events.",
    }
    job.update(overrides)
    return job


class PromptTests(unittest.TestCase):
    def test_input_includes_profile_and_job_facts(self):
        profile = {**profile_store.SEED_PROFILE, "skills": "APIs, event-driven architecture"}
        text = ai.build_fit_input(profile, sample_job())
        self.assertIn("Principal Technical Product Manager at Ingram Micro", text)
        self.assertIn("APIs, event-driven architecture", text)
        self.assertIn("Director of Product", text)
        self.assertIn("Own the platform roadmap", text)

    def test_empty_profile_fields_are_skipped(self):
        text = ai.build_fit_input(profile_store.SEED_PROFILE, sample_job())
        self.assertNotIn("Summary:", text)
        self.assertNotIn("Skills:", text)

    def test_missing_description_is_marked_explicitly(self):
        text = ai.build_fit_input(profile_store.SEED_PROFILE, sample_job(description=None))
        self.assertIn("(no description saved)", text)


class AnalyzeFitTests(unittest.TestCase):
    def test_returns_parsed_result_and_sends_strict_schema(self):
        client = FakeClient(json.dumps(GOOD_RESULT))
        result = ai.analyze_job_fit(client, profile_store.SEED_PROFILE, sample_job(), model="test-model")
        self.assertEqual(result, GOOD_RESULT)
        call = client.responses.calls[0]
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["text"]["format"]["type"], "json_schema")
        self.assertTrue(call["text"]["format"]["strict"])
        self.assertIn("Use only the facts", call["instructions"])

    def test_rejects_score_out_of_range(self):
        bad = {**GOOD_RESULT, "score": 150}
        with self.assertRaises(ValueError):
            ai.analyze_job_fit(FakeClient(json.dumps(bad)), profile_store.SEED_PROFILE, sample_job())

    def test_rejects_unknown_verdict(self):
        bad = {**GOOD_RESULT, "verdict": "Maybe"}
        with self.assertRaises(ValueError):
            ai.analyze_job_fit(FakeClient(json.dumps(bad)), profile_store.SEED_PROFILE, sample_job())

    def test_rejects_boolean_score(self):
        with self.assertRaises(ValueError):
            ai.validate_fit({**GOOD_RESULT, "score": True})

    def test_malformed_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            ai.analyze_job_fit(FakeClient("not json"), profile_store.SEED_PROFILE, sample_job())


class ClientTests(unittest.TestCase):
    def test_missing_key_raises_clear_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ai.MissingApiKeyError) as ctx:
                ai.get_client()
        self.assertIn("OPENAI_API_KEY is not set", str(ctx.exception))

    def test_blank_key_counts_as_missing(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "   "}):
            with self.assertRaises(ai.MissingApiKeyError):
                ai.get_client()

    def test_model_can_be_overridden_by_environment(self):
        with mock.patch.dict(os.environ, {"OPENAI_MODEL": "some-model"}):
            self.assertEqual(ai.get_model(), "some-model")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ai.get_model(), ai.DEFAULT_MODEL)


class DatabaseUpgradeTests(unittest.TestCase):
    def test_old_jobs_table_gets_new_columns_and_keeps_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            with closing(sqlite3.connect(path)) as raw:
                raw.execute(
                    "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
                    "company TEXT NOT NULL, location TEXT, url TEXT, "
                    "status TEXT NOT NULL DEFAULT 'Saved', notes TEXT, "
                    "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                    "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
                )
                raw.execute("INSERT INTO jobs (title, company) VALUES ('Old role', 'Old co')")
                raw.commit()
            with closing(db.connect(path)) as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
                self.assertIn("description", columns)
                self.assertIn("fit_analysis", columns)
                self.assertEqual(db.list_jobs(conn)[0]["title"], "Old role")

    def test_fit_analysis_round_trips_through_the_database(self):
        with tempfile.TemporaryDirectory() as tmp, closing(db.connect(Path(tmp) / "t.db")) as conn:
            job_id = db.add_job(conn, "Role", "Co", description="JD text")
            db.save_fit_analysis(conn, job_id, GOOD_RESULT)
            job = db.get_job(conn, job_id)
            self.assertEqual(job["description"], "JD text")
            self.assertEqual(json.loads(job["fit_analysis"]), GOOD_RESULT)




if __name__ == "__main__":
    unittest.main()
