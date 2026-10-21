"""Tests for résumé text extraction, tailoring, and draft storage (Stage 6).

These tests use a fake client, so they make no API calls.

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

from docx import Document  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

def build_minimal_pdf(text):
    """Build a one-page PDF containing `text`, with correct byte offsets in the xref table."""
    stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return bytes(out)

GOOD_TAILOR = {
    "headline": "Principal Technical Product Manager",
    "summary": "Product leader for enterprise platforms and APIs.",
    "bullets": ["Led event-driven platform roadmap", "Partnered with engineering on API governance"],
    "cover_letter": "Dear Hiring Manager, I am writing about the role.",
    "unsupported_requirements": ["Fintech domain experience"],
    "items_to_confirm": ["Rephrased platform roadmap bullet"],
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


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_plain_text(self):
        path = self.dir / "cv.txt"
        path.write_text("Plain résumé text", encoding="utf-8")
        self.assertEqual(profile_store.extract_text(path), "Plain résumé text")

    def test_reads_docx_paragraphs(self):
        path = self.dir / "cv.docx"
        document = Document()
        document.add_paragraph("Docx headline")
        document.add_paragraph("Docx bullet")
        document.save(path)
        text = profile_store.extract_text(path)
        self.assertIn("Docx headline", text)
        self.assertIn("Docx bullet", text)

    def test_reads_pdf_text(self):
        path = self.dir / "cv.pdf"
        path.write_bytes(build_minimal_pdf("Principal Product Manager"))
        self.assertIn("Principal Product Manager", profile_store.extract_text(path))

    def test_unsupported_type_raises(self):
        path = self.dir / "cv.png"
        path.write_bytes(b"\x89PNG")
        with self.assertRaises(ValueError):
            profile_store.extract_text(path)

    def test_document_text_uses_stored_file_by_id(self):
        os.environ[profile_store.DOCS_DIR_ENV_VAR] = str(self.dir / "documents")
        try:
            with closing(db.connect(self.dir / "t.db")) as conn:
                doc_id = profile_store.save_document(conn, "Resume", "cv.txt", b"Stored text")
                self.assertEqual(profile_store.document_text(conn, doc_id), "Stored text")
                with self.assertRaises(ValueError):
                    profile_store.document_text(conn, 999)
        finally:
            os.environ.pop(profile_store.DOCS_DIR_ENV_VAR, None)








if __name__ == "__main__":
    unittest.main()
