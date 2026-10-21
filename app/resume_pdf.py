"""Render a tailored résumé as a PDF (one per job).

Content comes only from the tailored result, which the AI built from your saved profile and
experience. Sections with no content are left out instead of being filled with placeholders.
"""

from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

MARGIN = 18 * mm


def _styles():
    base = getSampleStyleSheet()
    return {
        "name": ParagraphStyle("name", parent=base["Title"], alignment=TA_LEFT, fontSize=18, leading=22, spaceAfter=2),
        "headline": ParagraphStyle("headline", parent=base["Normal"], fontSize=11, leading=14, textColor=colors.HexColor("#333333")),
        "section": ParagraphStyle("section", parent=base["Heading2"], fontSize=11, leading=14, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1f3b5a")),
        "body": ParagraphStyle("body", parent=base["Normal"], fontSize=9.5, leading=12.5),
        "role": ParagraphStyle("role", parent=base["Normal"], fontSize=10, leading=13, spaceBefore=4, fontName="Helvetica-Bold"),
        "bullet": ParagraphStyle("bullet", parent=base["Normal"], fontSize=9.5, leading=12.5, leftIndent=12, bulletIndent=2),
    }


def _paragraph(text, style):
    return Paragraph(escape(text), style)


def build_resume_story(tailored, profile):
    """Return the flowables for the résumé. Pure function, so it can be tested without a file."""
    styles = _styles()
    story = []
    name = (profile.get("full_name") or "").strip()
    if name:
        story.append(_paragraph(name, styles["name"]))
    story.append(_paragraph(tailored["headline"], styles["headline"]))
    story.append(Spacer(1, 4))

    story.append(_paragraph("Summary", styles["section"]))
    story.append(_paragraph(tailored["summary"], styles["body"]))

    if tailored["skills"]:
        story.append(_paragraph("Skills", styles["section"]))
        story.append(_paragraph(", ".join(tailored["skills"]), styles["body"]))

    story.append(_paragraph("Experience", styles["section"]))
    for role in tailored["experience"]:
        story.append(_paragraph(f"{role['title']}, {role['company']}", styles["role"]))
        for bullet in role["bullets"]:
            story.append(Paragraph(escape(bullet), styles["bullet"], bulletText="•"))

    education = (profile.get("education") or "").strip()
    if education:
        story.append(_paragraph("Education", styles["section"]))
        story.append(_paragraph(education, styles["body"]))
    return story


def write_resume_pdf(tailored, profile, path):
    """Write the tailored résumé to a PDF file at path."""
    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=f"Résumé: {tailored['headline']}",
    )
    document.build(build_resume_story(tailored, profile))
    return path
