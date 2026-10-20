"""Tests for profile and document storage (Stage 4).

Run from the project root with:
    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import db  # noqa: E402
import profile_store  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.conn = db.connect(self.root / "test.db")
        self._old_docs = os.environ.get(profile_store.DOCS_DIR_ENV_VAR)
        os.environ[profile_store.DOCS_DIR_ENV_VAR] = str(self.root / "documents")

    def tearDown(self):
        self.conn.close()
        if self._old_docs is None:
            os.environ.pop(profile_store.DOCS_DIR_ENV_VAR, None)
        else:
            os.environ[profile_store.DOCS_DIR_ENV_VAR] = self._old_docs
        self._tmp.cleanup()

    def test_new_profile_returns_seed_values_from_background_only(self):
        profile = profile_store.get_profile(self.conn)
        self.assertEqual(profile["headline"], "Principal Technical Product Manager at Ingram Micro")
        self.assertEqual(profile["education"], "IIM PGDM")
        self.assertEqual(profile["full_name"], "")
        self.assertEqual(profile["achievements"], "")

    def test_save_and_reload_profile(self):
        profile_store.save_profile(self.conn, full_name="  Test Person  ", skills="Python, SQL")
        profile = profile_store.get_profile(self.conn)
        self.assertEqual(profile["full_name"], "Test Person")
        self.assertEqual(profile["skills"], "Python, SQL")

    def test_saving_profile_again_replaces_values(self):
        profile_store.save_profile(self.conn, headline="First")
        profile_store.save_profile(self.conn, headline="Second")
        self.assertEqual(profile_store.get_profile(self.conn)["headline"], "Second")
        count = self.conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0]
        self.assertEqual(count, 1)

    def test_save_profile_rejects_unknown_fields(self):
        with self.assertRaises(ValueError):
            profile_store.save_profile(self.conn, salary="secret")

    def test_safe_filename_strips_paths_and_unsafe_characters(self):
        self.assertEqual(profile_store.safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(profile_store.safe_filename("My Résumé (final).pdf"), "My_R_sum_final_.pdf")
        self.assertEqual(profile_store.safe_filename("..."), "file")

    def test_save_document_writes_file_and_records_metadata(self):
        doc_id = profile_store.save_document(self.conn, "Resume", "cv.pdf", b"%PDF-test")
        docs = profile_store.list_documents(self.conn)
        self.assertEqual(docs[0]["id"], doc_id)
        self.assertEqual(docs[0]["original_name"], "cv.pdf")
        stored = Path(os.environ[profile_store.DOCS_DIR_ENV_VAR]) / docs[0]["stored_name"]
        self.assertEqual(stored.read_bytes(), b"%PDF-test")

    def test_save_document_keeps_uploads_inside_documents_folder(self):
        profile_store.save_document(self.conn, "Other", "../../escape.txt", b"x")
        docs_dir = Path(os.environ[profile_store.DOCS_DIR_ENV_VAR])
        written = list(docs_dir.iterdir())
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].parent, docs_dir)
        self.assertFalse((self.root / "escape.txt").exists())

    def test_save_document_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            profile_store.save_document(self.conn, "Certificate", "a.pdf", b"x")

    def test_same_filename_twice_is_stored_as_two_files(self):
        profile_store.save_document(self.conn, "Resume", "cv.pdf", b"one")
        profile_store.save_document(self.conn, "Resume", "cv.pdf", b"two")
        self.assertEqual(len(profile_store.list_documents(self.conn)), 2)


class ProfilePageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = os.environ.get(db.DB_PATH_ENV_VAR)
        self._old_docs = os.environ.get(profile_store.DOCS_DIR_ENV_VAR)
        os.environ[db.DB_PATH_ENV_VAR] = str(Path(self._tmp.name) / "ui.db")
        os.environ[profile_store.DOCS_DIR_ENV_VAR] = str(Path(self._tmp.name) / "documents")

    def tearDown(self):
        for var, old in (
            (db.DB_PATH_ENV_VAR, self._old_db),
            (profile_store.DOCS_DIR_ENV_VAR, self._old_docs),
        ):
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old
        self._tmp.cleanup()

    def run_app(self):
        return AppTest.from_file(str(APP_DIR / "main.py"), default_timeout=30).run()

    @staticmethod
    def field(at, label):
        return next(t for t in at.text_input if t.label == label)

    def test_profile_form_shows_seed_headline_and_renders_without_errors(self):
        at = self.run_app()
        self.assertEqual(len(at.exception), 0)
        self.assertEqual(
            self.field(at, "Headline").value,
            "Principal Technical Product Manager at Ingram Micro",
        )

    def test_saving_profile_through_the_page_persists_it(self):
        at = self.run_app()
        self.field(at, "Full name").set_value("Test Person")
        next(b for b in at.button if b.label == "Save profile").click().run()
        self.assertEqual(len(at.exception), 0)
        with closing(db.connect()) as conn:
            self.assertEqual(profile_store.get_profile(conn)["full_name"], "Test Person")


if __name__ == "__main__":
    unittest.main()
