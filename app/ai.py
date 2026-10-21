"""OpenAI integration for the automated pipeline: job-fit analysis and per-job résumé tailoring.

The API key is read from the OPENAI_API_KEY environment variable, which is loaded from the
project's .env file (git-ignored). The key is never printed or stored in the database.
"""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from db import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

API_KEY_ENV_VAR = "OPENAI_API_KEY"
MODEL_ENV_VAR = "OPENAI_MODEL"
# Set OPENAI_MODEL in .env to change this without editing code.
DEFAULT_MODEL = "gpt-4.1-mini"

VERDICTS = ("Strong fit", "Possible fit", "Weak fit")

FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["score", "verdict", "strengths", "gaps", "summary"],
    "additionalProperties": False,
}

FIT_INSTRUCTIONS = """You assess how well a candidate fits a specific job.

Rules:
- Use only the facts in the candidate profile, experience, and job description. Do not assume
  experience, skills, or achievements that are not written there.
- If something the job requires is not in the profile, list it under gaps.
- Judge seniority and scope as well as keywords: a senior product leader should not be
  scored as a fit for a junior role just because the titles overlap.
- Score from 0 to 100, where 100 is an excellent fit for this candidate's level.
- Keep each strength and gap to one short sentence.
"""

TAILOR_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "string"}},
        "experience": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "title": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["company", "title", "bullets"],
                "additionalProperties": False,
            },
        },
        "cover_letter": {"type": "string"},
        "unsupported_requirements": {"type": "array", "items": {"type": "string"}},
        "items_to_confirm": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "headline",
        "summary",
        "skills",
        "experience",
        "cover_letter",
        "unsupported_requirements",
        "items_to_confirm",
    ],
    "additionalProperties": False,
}

TAILOR_INSTRUCTIONS = """You tailor a candidate's résumé for one job and write a cover letter.
The output becomes a PDF that the candidate reviews before anything is sent.

Rules:
- Use only facts in the candidate profile, experience list, and résumé text. Never invent
  employers, titles, dates, metrics, skills, degrees, or achievements.
- experience: include the most relevant roles (at most 4). Use the company and title exactly as
  they appear in the EXPERIENCE list. Give each role 3 to 5 bullets that reword and emphasize
  facts from that role for this job. Do not move achievements between roles.
- Bullets describe what the candidate did. Do not copy responsibilities, tools, or outcomes from
  the job description into a role unless that role's own description or résumé text says it.
  If a bullet would only be true because the job asks for it, leave it out and list the
  requirement in unsupported_requirements.
- skills: list only skills that appear in the profile or résumé, most relevant to this job first.
- Any requirement in the job description that the materials do not support goes in
  unsupported_requirements. Do not write bullets or cover letter sentences that claim it.
- If you rephrased or inferred anything beyond the exact wording, add it to items_to_confirm.
- headline: a short professional headline that matches the candidate's real title.
- cover_letter: about 250 words, first person, professional, no generic filler phrases.
"""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["unsupported_claims"],
    "additionalProperties": False,
}

VERIFY_INSTRUCTIONS = """You check a tailored résumé and cover letter against the candidate's own sources.

For every claim in the tailored output that states a fact about the candidate (an employer, title,
date, metric, scope, team size, outcome, responsibility, tool, or skill), decide whether the
SOURCES state it, at least in substance. Claims that only the job description supports are
unsupported. Vague wording such as "improved performance" counts as a claim if the sources do not
say what was improved.

Return each unsupported claim verbatim in unsupported_claims. If every claim is supported, return an
empty list. Do not judge style or tone.
"""


def verify_tailored(client, profile, job, resume_text, tailored, model=None):
    """Return the claims in a tailored draft that the candidate's own sources do not support."""
    sources = build_tailor_input(profile, job, resume_text)
    draft = json.dumps(
        {key: tailored[key] for key in ("headline", "summary", "skills", "experience", "cover_letter")},
        ensure_ascii=False,
        indent=2,
    )
    response = client.responses.create(
        model=model or get_model(),
        instructions=VERIFY_INSTRUCTIONS,
        input=f"SOURCES\n{sources}\n\nTAILORED OUTPUT\n{draft}",
        text={
            "format": {
                "type": "json_schema",
                "name": "claim_check",
                "schema": VERIFY_SCHEMA,
                "strict": True,
            }
        },
    )
    claims = json.loads(response.output_text)["unsupported_claims"]
    return [claim for claim in claims if claim.strip()]


class MissingApiKeyError(RuntimeError):
    """Raised when OPENAI_API_KEY is not configured."""


def get_model():
    return os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL)


def get_client():
    """Create an OpenAI client from the environment. Raises MissingApiKeyError if unset."""
    api_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if not api_key:
        raise MissingApiKeyError(
            "OPENAI_API_KEY is not set. Add it to the .env file in the project root (see README)."
        )
    return OpenAI(api_key=api_key)


def build_fit_input(profile, job):
    """Turn the saved profile, experience, and a job into the text the model reads."""
    profile_lines = [
        f"- {label}: {profile[field].strip()}"
        for field, label in (
            ("headline", "Headline"),
            ("years_experience", "Years of experience"),
            ("education", "Education"),
            ("summary", "Summary"),
            ("skills", "Skills"),
            ("achievements", "Key achievements"),
            ("target_roles", "Target roles"),
            ("industries", "Industries"),
            ("preferred_locations", "Preferred locations"),
        )
        if profile.get(field, "").strip()
    ]
    experience_lines = [
        f"- {entry['title']} at {entry['company']} "
        f"({entry.get('started_on') or 'start not stated'} to {entry.get('finished_on') or 'present'})"
        + (f": {entry['description']}" if entry.get("description") else "")
        for entry in profile.get("experience", [])
    ]
    return "\n".join(
        [
            "CANDIDATE PROFILE",
            *(profile_lines or ["- (no profile details saved)"]),
            "",
            "EXPERIENCE (from LinkedIn export and manual entries)",
            *(experience_lines or ["- (no experience saved)"]),
            "",
            "JOB",
            f"- Title: {job['title']}",
            f"- Company: {job['company']}",
            f"- Location: {job.get('location') or 'not stated'}",
            "",
            "JOB DESCRIPTION",
            (job.get("description") or "").strip() or "(no description saved)",
        ]
    )


def build_tailor_input(profile, job, resume_text):
    """Combine saved profile, résumé text, and job into the model input."""
    resume_part = (resume_text or "").strip() or "(no résumé text provided)"
    return "\n".join([build_fit_input(profile, job), "", "RÉSUMÉ TEXT", resume_part])


def analyze_job_fit(client, profile, job, model=None):
    """Ask the model to assess fit. Returns a validated dict with score, verdict, and lists."""
    response = client.responses.create(
        model=model or get_model(),
        instructions=FIT_INSTRUCTIONS,
        input=build_fit_input(profile, job),
        text={
            "format": {
                "type": "json_schema",
                "name": "job_fit",
                "schema": FIT_SCHEMA,
                "strict": True,
            }
        },
    )
    result = json.loads(response.output_text)
    validate_fit(result)
    return result


def validate_fit(result):
    """Check the model output against the rules the UI depends on. Raises ValueError if wrong."""
    score = result.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        raise ValueError(f"Score must be an integer from 0 to 100, got {score!r}")
    if result.get("verdict") not in VERDICTS:
        raise ValueError(f"Unknown verdict: {result.get('verdict')!r}")
    for key in ("strengths", "gaps"):
        if not isinstance(result.get(key), list):
            raise ValueError(f"{key} must be a list")
    if not isinstance(result.get("summary"), str):
        raise ValueError("summary must be text")
    return result


def tailor_application(client, profile, job, resume_text, model=None):
    """Produce a structured, tailored résumé and a cover letter for one job.

    Experience entries must match roles in the profile's experience list. If that list is empty,
    the roles could not be checked, so the draft says so in items_to_confirm.
    """
    response = client.responses.create(
        model=model or get_model(),
        instructions=TAILOR_INSTRUCTIONS,
        input=build_tailor_input(profile, job, resume_text),
        text={
            "format": {
                "type": "json_schema",
                "name": "tailored_application",
                "schema": TAILOR_SCHEMA,
                "strict": True,
            }
        },
    )
    result = json.loads(response.output_text)
    known_roles = {
        (entry["company"].strip().lower(), entry["title"].strip().lower())
        for entry in profile.get("experience", [])
    }
    validate_tailor(result, known_roles)
    if not known_roles:
        result["items_to_confirm"].append(
            "Roles were not checked against a saved experience list. Verify them against your résumé."
        )
    return result


def validate_tailor(result, known_roles=frozenset()):
    """Check the tailored output. Raises ValueError if the content is unusable or invents a role."""
    for key in ("headline", "summary", "cover_letter"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise ValueError(f"{key} must be non-empty text")
    for key in ("skills", "unsupported_requirements", "items_to_confirm"):
        if not isinstance(result.get(key), list) or not all(isinstance(i, str) for i in result[key]):
            raise ValueError(f"{key} must be a list of text")
    if not result.get("experience"):
        raise ValueError("experience must include at least one role")
    for role in result["experience"]:
        if not role.get("bullets") or not all(isinstance(b, str) and b.strip() for b in role["bullets"]):
            raise ValueError(f"Role {role.get('title')!r} needs at least one bullet")
        if known_roles and (role["company"].strip().lower(), role["title"].strip().lower()) not in known_roles:
            raise ValueError(
                f"Role {role['title']!r} at {role['company']!r} is not in your experience list"
            )
    return result
