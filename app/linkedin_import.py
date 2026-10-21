"""Import your LinkedIn data export into the profile.

Get the export from LinkedIn: Settings > Data privacy > Get a copy of your data. Choose the
option that includes Profile, Positions, Education, and Skills, then upload the ZIP here.

Only four files are read: Profile.csv, Positions.csv, Education.csv, and Skills.csv. The ZIP
itself is not stored. Its contents are copied into your local profile database.

Column names follow LinkedIn's export format. LinkedIn changes its export from time to time, so
missing columns are treated as blank rather than causing an error.
"""

import csv
import io
import zipfile

from db import replace_linkedin_experience
from profile_store import get_profile, save_profile

WANTED_FILES = {"profile.csv", "positions.csv", "education.csv", "skills.csv"}
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DESCRIPTION_CHARS = 1500


def read_export(zip_bytes):
    """Return {lowercase file name: list of row dicts} for the files we need.

    Raises ValueError if the upload is not a ZIP file.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError("That file is not a ZIP archive. Upload the LinkedIn export ZIP.") from exc

    files = {}
    with archive:
        for info in archive.infolist():
            name = info.filename.rsplit("/", 1)[-1].lower()
            if name not in WANTED_FILES or info.file_size > MAX_FILE_BYTES:
                continue
            text = archive.read(info).decode("utf-8-sig", errors="replace")
            files[name] = list(csv.DictReader(io.StringIO(text)))
    return files


def _clean(value):
    return (value or "").strip()


def parse_export(files):
    """Turn the raw CSV rows into profile fields, experience, education, and skills."""
    profile_rows = files.get("profile.csv") or [{}]
    profile_row = profile_rows[0]
    full_name = " ".join(
        part for part in (_clean(profile_row.get("First Name")), _clean(profile_row.get("Last Name"))) if part
    )

    experience = []
    for row in files.get("positions.csv", []):
        company, title = _clean(row.get("Company Name")), _clean(row.get("Title"))
        if not (company or title):
            continue
        experience.append(
            {
                "company": company or "Not stated",
                "title": title or "Not stated",
                "location": _clean(row.get("Location")) or None,
                "started_on": _clean(row.get("Started On")) or None,
                "finished_on": _clean(row.get("Finished On")) or None,
                "description": _clean(row.get("Description"))[:MAX_DESCRIPTION_CHARS] or None,
            }
        )

    education = []
    for row in files.get("education.csv", []):
        school, degree = _clean(row.get("School Name")), _clean(row.get("Degree Name"))
        if school or degree:
            education.append(" at ".join(part for part in (degree, school) if part))

    skills = []
    for row in files.get("skills.csv", []):
        name = _clean(row.get("Name"))
        if name:
            skills.append(name)

    return {
        "profile": {
            "full_name": full_name,
            "headline": _clean(profile_row.get("Headline")),
            "summary": _clean(profile_row.get("Summary")),
            "industries": _clean(profile_row.get("Industry")),
        },
        "experience": experience,
        "education": education,
        "skills": skills,
        "files_found": sorted(files),
    }


def apply_export(conn, parsed):
    """Save the parsed export. Empty profile fields are filled; existing values are kept.

    Skills are merged without duplicates. LinkedIn experience is replaced, so importing the same
    export twice does not duplicate it. Returns a summary for display.
    """
    current = get_profile(conn)
    fills = {}
    for field, value in parsed["profile"].items():
        if value and not current.get(field, "").strip():
            fills[field] = value

    if parsed["education"] and not current.get("education", "").strip():
        fills["education"] = "; ".join(parsed["education"])

    existing_skills = [s.strip() for s in current.get("skills", "").split(",") if s.strip()]
    seen = {s.lower() for s in existing_skills}
    new_skills = []
    for skill in parsed["skills"]:
        if skill.lower() not in seen:
            seen.add(skill.lower())
            new_skills.append(skill)
    if new_skills:
        fills["skills"] = ", ".join(existing_skills + new_skills)

    if fills:
        save_profile(conn, **fills)
    replace_linkedin_experience(conn, parsed["experience"])

    return {
        "fields_filled": sorted(k for k in fills if k != "skills"),
        "skills_added": len(new_skills),
        "experience": len(parsed["experience"]),
        "education": len(parsed["education"]),
    }
