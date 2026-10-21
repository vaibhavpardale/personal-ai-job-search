"""Tests for profile completeness and LinkedIn export import (phase 3).

Uses a synthetic export ZIP built in the test, in LinkedIn's CSV layout. Makes no network calls.

Run from the project root with:
    python -m unittest discover -s tests -v
"""

import io
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import ai  # noqa: E402
import db  # noqa: E402
import linkedin_import  # noqa: E402
import profile_store  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

PROFILE_CSV = (
    "First Name,Last Name,Headline,Summary,Industry,Geo Location\n"
    "Test,Person,Product leader for platforms,Builds platform products.,Computer Software,Bengaluru\n"
)
POSITIONS_CSV = (
    "Company Name,Title,Description,Location,Started On,Finished On\n"
    "Acme,Principal Product Manager,Owned the API roadmap.,Bengaluru,Jan 2020,\n"
    "Beta,Senior Product Manager,Ran the payments roadmap.,Remote,Mar 2015,Dec 2019\n"
    ",,Empty row is skipped,,,\n"
)
EDUCATION_CSV = "School Name,Degree Name,Start Date,End Date\nIIM Example,PGDM,2012,2014\n"
SKILLS_CSV = "Name\nAPIs\nproduct strategy\n"


def build_export_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text.encode("utf-8-sig"))
    return buffer.getvalue()


def full_export(**overrides):
    files = {
        "Basic_LinkedInDataExport/Profile.csv": PROFILE_CSV,
        "Basic_LinkedInDataExport/Positions.csv": POSITIONS_CSV,
        "Basic_LinkedInDataExport/Education.csv": EDUCATION_CSV,
        "Basic_LinkedInDataExport/Skills.csv": SKILLS_CSV,
        "Basic_LinkedInDataExport/Connections.csv": "First Name,Last Name\nSomeone,Else\n",
    }
    files.update(overrides)
    return build_export_zip(files)


class ReadAndParseTests(unittest.TestCase):
    def test_reads_only_the_four_expected_files_from_nested_folders(self):
        files = linkedin_import.read_export(full_export())
        self.assertEqual(sorted(files), ["education.csv", "positions.csv", "profile.csv", "skills.csv"])

    def test_non_zip_upload_is_rejected_with_a_clear_message(self):
        with self.assertRaises(ValueError) as ctx:
            linkedin_import.read_export(b"not a zip")
        self.assertIn("ZIP", str(ctx.exception))

    def test_parse_extracts_profile_roles_education_and_skills(self):
        parsed = linkedin_import.parse_export(linkedin_import.read_export(full_export()))
        self.assertEqual(parsed["profile"]["full_name"], "Test Person")
        self.assertEqual(parsed["profile"]["headline"], "Product leader for platforms")
        self.assertEqual(parsed["profile"]["industries"], "Computer Software")
        self.assertEqual(len(parsed["experience"]), 2)
        self.assertEqual(parsed["experience"][0]["title"], "Principal Product Manager")
        self.assertIsNone(parsed["experience"][0]["finished_on"])
        self.assertEqual(parsed["education"], ["PGDM at IIM Example"])
        self.assertEqual(parsed["skills"], ["APIs", "product strategy"])

    def test_missing_files_are_treated_as_empty_not_an_error(self):
        export = build_export_zip({"Profile.csv": PROFILE_CSV})
        parsed = linkedin_import.parse_export(linkedin_import.read_export(export))
        self.assertEqual(parsed["experience"], [])
        self.assertEqual(parsed["skills"], [])
        self.assertEqual(parsed["files_found"], ["profile.csv"])

    def test_column_changes_do_not_crash_the_import(self):
        odd = "Company,Role\nAcme,Director\n"
        parsed = linkedin_import.parse_export(
            linkedin_import.read_export(build_export_zip({"Positions.csv": odd}))
        )
        self.assertEqual(parsed["experience"], [])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "p.db")
        self.parsed = linkedin_import.parse_export(linkedin_import.read_export(full_export()))

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_empty_profile_fields_are_filled(self):
        linkedin_import.apply_export(self.conn, self.parsed)
        profile = profile_store.get_profile(self.conn)
        self.assertEqual(profile["full_name"], "Test Person")
        self.assertEqual(profile["summary"], "Builds platform products.")
        self.assertEqual(profile["industries"], "Computer Software")

    def test_existing_text_is_not_overwritten(self):
        profile_store.save_profile(self.conn, headline="My own headline")
        linkedin_import.apply_export(self.conn, self.parsed)
        self.assertEqual(profile_store.get_profile(self.conn)["headline"], "My own headline")

    def test_skills_are_merged_without_case_duplicates(self):
        profile_store.save_profile(self.conn, skills="apis, Python")
        summary = linkedin_import.apply_export(self.conn, self.parsed)
        skills = profile_store.get_profile(self.conn)["skills"]
        self.assertEqual(skills, "apis, Python, product strategy")
        self.assertEqual(summary["skills_added"], 1)

    def test_reimporting_the_same_export_does_not_duplicate_roles(self):
        linkedin_import.apply_export(self.conn, self.parsed)
        linkedin_import.apply_export(self.conn, self.parsed)
        self.assertEqual(len(db.list_experience(self.conn)), 2)

    def test_manual_experience_is_kept_when_importing(self):
        self.conn.execute(
            "INSERT INTO experience (company, title, source) VALUES ('Manual Co', 'Advisor', 'Manual')"
        )
        self.conn.commit()
        linkedin_import.apply_export(self.conn, self.parsed)
        titles = {e["title"] for e in db.list_experience(self.conn)}
        self.assertIn("Advisor", titles)
        self.assertIn("Senior Product Manager", titles)

    def test_profile_includes_experience_and_new_fields(self):
        linkedin_import.apply_export(self.conn, self.parsed)
        profile = profile_store.get_profile(self.conn)
        self.assertEqual(len(profile["experience"]), 2)
        self.assertIn("target_roles", profile)
        self.assertIn("preferred_locations", profile)


class SavingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "s.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_saving_one_field_keeps_the_others(self):
        profile_store.save_profile(self.conn, skills="APIs", target_roles="Director of Product")
        profile_store.save_profile(self.conn, summary="New summary")
        profile = profile_store.get_profile(self.conn)
        self.assertEqual(profile["skills"], "APIs")
        self.assertEqual(profile["target_roles"], "Director of Product")
        self.assertEqual(profile["summary"], "New summary")

    def test_new_profile_fields_round_trip(self):
        profile_store.save_profile(
            self.conn, target_roles="Head of Product", industries="SaaS", preferred_locations="Dubai"
        )
        profile = profile_store.get_profile(self.conn)
        self.assertEqual(
            (profile["target_roles"], profile["industries"], profile["preferred_locations"]),
            ("Head of Product", "SaaS", "Dubai"),
        )


class PromptTests(unittest.TestCase):
    def test_prompt_includes_experience_and_target_roles(self):
        profile = {
            **profile_store.SEED_PROFILE,
            "target_roles": "Director of Product",
            "experience": [{
                "title": "Principal Product Manager", "company": "Acme",
                "started_on": "Jan 2020", "finished_on": None,
                "description": "Owned the API roadmap.",
            }],
        }
        job = {"title": "Director", "company": "X", "location": None, "description": "JD"}
        text = ai.build_fit_input(profile, job)
        self.assertIn("Target roles: Director of Product", text)
        self.assertIn("Principal Product Manager at Acme (Jan 2020 to present): Owned the API roadmap.", text)

    def test_prompt_says_when_no_experience_is_saved(self):
        job = {"title": "Director", "company": "X", "location": None, "description": "JD"}
        text = ai.build_fit_input({**profile_store.SEED_PROFILE, "experience": []}, job)
        self.assertIn("(no experience saved)", text)


class UpgradeTests(unittest.TestCase):
    def test_existing_profile_table_gets_the_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            with closing(sqlite3.connect(path)) as raw:
                raw.executescript(
                    "CREATE TABLE profile (id INTEGER PRIMARY KEY CHECK (id = 1), full_name TEXT NOT NULL DEFAULT '', "
                    "headline TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', "
                    "years_experience TEXT NOT NULL DEFAULT '', education TEXT NOT NULL DEFAULT '', "
                    "skills TEXT NOT NULL DEFAULT '', achievements TEXT NOT NULL DEFAULT '', "
                    "updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
                    "INSERT INTO profile (id, headline) VALUES (1, 'Kept headline');"
                )
                raw.commit()
            with closing(db.connect(path)) as conn:
                profile = profile_store.get_profile(conn)
        self.assertEqual(profile["headline"], "Kept headline")
        self.assertEqual(profile["target_roles"], "")


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

    def test_setup_page_shows_new_fields_and_import_section(self):
        at = AppTest.from_file(str(APP_DIR / "main.py"), default_timeout=30).run()
        self.assertEqual(len(at.exception), 0)
        labels = [t.label for t in at.text_area] + [t.label for t in at.text_input]
        self.assertIn("Target roles (one per line)", labels)
        self.assertIn("Preferred locations (comma-separated)", labels)
        self.assertIn("LinkedIn data", [s.value for s in at.subheader])


if __name__ == "__main__":
    unittest.main()
