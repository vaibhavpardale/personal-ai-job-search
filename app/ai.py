"""OpenAI integration and job-fit analysis (Stage 5).

The API key is read from the OPENAI_API_KEY environment variable, which is loaded from the
project's .env file (git-ignored). The key is never printed or stored in the database.
"""

import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
- Use only the facts in the candidate profile and the job description. Do not assume
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
        "bullets": {"type": "array", "items": {"type": "string"}},
        "cover_letter": {"type": "string"},
        "unsupported_requirements": {"type": "array", "items": {"type": "string"}},
        "items_to_confirm": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "headline",
        "summary",
        "bullets",
        "cover_letter",
        "unsupported_requirements",
        "items_to_confirm",
    ],
    "additionalProperties": False,
}

TAILOR_INSTRUCTIONS = """You tailor a candidate's résumé and write a cover letter for one job.

Rules:
- Use only facts found in the candidate profile and résumé text. Never invent employers,
  titles, dates, metrics, skills, degrees, or achievements.
- Reword and emphasize relevant facts for this job, but do not change what they say.
- Any requirement in the job description that the candidate materials do not support goes
  in unsupported_requirements. Do not write bullets or letter sentences that claim it.
- If you had to rephrase or infer something beyond the exact wording, add it to
  items_to_confirm so the candidate can check it.
- Bullets: 4 to 6 concise, outcome-focused bullets.
- Cover letter: about 250 words, first person, professional, no generic filler phrases,
  signed with the candidate's full name if one is provided (otherwise leave the name out).
"""


RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_facts": {"type": "array", "items": {"type": "string"}},
        "recent_news": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "key_facts", "recent_news", "unknowns"],
    "additionalProperties": False,
}

RESEARCH_INSTRUCTIONS = """You write a short briefing about a company for a job applicant, using web search.

Rules:
- Use only information from the search results. Do not rely on memory for facts.
- Cover what helps an applicant: business model, scale, products, strategy, recent
  announcements (with dates), and the context around the role.
- Say explicitly what you could not verify or did not find.
- Do not research or name private individuals. Company information only.
- Do not put links, URLs, or citation markers in the text. Sources are reported separately.
"""

# Inline citation markup such as "([site.com](https://...))" that the model sometimes adds.
CITATION_MARKUP = re.compile(r"\s*\(\[[^\]]*\]\([^)]*\)\)")

# Query parameters added by some sites and by the search tool, which are not part of the page.
TRACKING_PARAMS = ("utm_source", "utm_medium", "utm_campaign")


def extract_sources(response):
    """Collect unique web sources from the citation annotations the search tool returned.

    Sources come from the search results themselves, not from text the model wrote.
    """
    sources, seen = [], set()
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            for annotation in getattr(part, "annotations", []) or []:
                if getattr(annotation, "type", None) != "url_citation":
                    continue
                url = _strip_tracking(annotation.url)
                if url in seen or not url.startswith(("http://", "https://")):
                    continue
                seen.add(url)
                sources.append({"title": getattr(annotation, "title", "") or url, "url": url})
    return sources


def _strip_tracking(url):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


STRUCTURE_INSTRUCTIONS = """You turn a company briefing into structured fields.

Rules:
- Use only the facts in the briefing. Do not add facts from memory.
- Put anything the briefing says it could not verify, or did not find, into unknowns.
- Remove any links or URLs from the text.
"""


def research_company(client, company, job_title, model=None):
    """Search the web for company information and return a structured briefing with sources.

    Two steps. The first call searches the web and returns the sources as citations. The second
    call has no tools and only shapes that briefing into fields. Citations are not returned when a
    JSON schema is requested in the same call, so the steps are kept separate.
    """
    search = client.responses.create(
        model=model or get_model(),
        instructions=RESEARCH_INSTRUCTIONS,
        input=f"Company: {company}\nRole the applicant is considering: {job_title}",
        tools=[{"type": "web_search"}],
        tool_choice="required",
    )
    sources = extract_sources(search)

    structured = client.responses.create(
        model=model or get_model(),
        instructions=STRUCTURE_INSTRUCTIONS,
        input=search.output_text,
        text={
            "format": {
                "type": "json_schema",
                "name": "company_research",
                "schema": RESEARCH_SCHEMA,
                "strict": True,
            }
        },
    )
    result = json.loads(structured.output_text)
    validate_research(result)
    result = clean_research_text(result)
    result["sources"] = sources
    return result


def clean_research_text(result):
    """Remove inline citation markup from the text fields. Sources are kept separately."""

    def clean(text):
        return CITATION_MARKUP.sub("", text).strip()

    cleaned = dict(result)
    cleaned["summary"] = clean(result["summary"])
    for key in ("key_facts", "recent_news", "unknowns"):
        cleaned[key] = [clean(item) for item in result[key]]
    return cleaned


def validate_research(result):
    """Check research output has the expected fields. Raises ValueError if not."""
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise ValueError("summary must be non-empty text")
    for key in ("key_facts", "recent_news", "unknowns"):
        if not isinstance(result.get(key), list) or not all(isinstance(i, str) for i in result[key]):
            raise ValueError(f"{key} must be a list of text")
    return result


OUTREACH_KINDS = ("Referral request", "Networking message")

OUTREACH_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "message": {"type": "string"},
        "items_to_confirm": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subject", "message", "items_to_confirm"],
    "additionalProperties": False,
}

OUTREACH_INSTRUCTIONS = """You draft a message from a job candidate to one person.
The candidate will review and send it themselves. Nothing is sent automatically.

Rules:
- Use only the facts in the candidate profile, the job, and the contact details given.
- Do not claim any shared history, mutual acquaintances, or prior conversations with the
  contact unless the contact notes state them.
- Referral request: ask a specific, easy question (for example, whether they would be
  willing to share the job or a referral), make it clear the candidate is not assuming a
  referral, and offer a short way to say no.
- Networking message: a brief, specific reason to connect that relates to the contact's
  role or company, with no request for a job in the first message.
- Keep the message under 150 words. Plain, respectful, no flattery and no buzzwords.
- Put anything you assumed or inferred, such as the reason for contacting them, in
  items_to_confirm.
"""

PREP_SCHEMA = {
    "type": "object",
    "properties": {
        "likely_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "why_asked": {"type": "string"},
                    "answer_from_profile": {"type": "string"},
                },
                "required": ["question", "why_asked", "answer_from_profile"],
                "additionalProperties": False,
            },
        },
        "gaps_to_prepare_for": {"type": "array", "items": {"type": "string"}},
        "questions_to_ask_them": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["likely_questions", "gaps_to_prepare_for", "questions_to_ask_them"],
    "additionalProperties": False,
}

PREP_INSTRUCTIONS = """You prepare a job candidate for an interview for one specific role.

Rules:
- Base questions on the job description and the role's requirements.
- answer_from_profile must restate a fact that is written in the candidate profile or the
  résumé text, using the same specifics. Do not add scope, team sizes, industries, outcomes,
  or responsibilities that are not written there. Generic professional advice is not evidence.
- If the profile and résumé text do not contain the fact, write exactly:
  "No evidence in profile or résumé: prepare a real example". Do not guess.
- Put job requirements that the profile and résumé do not evidence in gaps_to_prepare_for.
- Suggest 4 to 6 thoughtful questions the candidate could ask the interviewer.
- Keep answers short: one to three sentences each.
"""


def draft_outreach(client, profile, job, contact, kind, model=None):
    """Draft a referral request or networking message. The candidate sends it, not the app."""
    if kind not in OUTREACH_KINDS:
        raise ValueError(f"Unknown outreach kind: {kind!r}")
    contact_lines = "\n".join(
        [
            "CONTACT",
            f"- Name: {contact['name']}",
            f"- Role: {contact.get('role') or 'not stated'}",
            f"- Company: {contact.get('company') or 'not stated'}",
            f"- Notes from the candidate: {contact.get('notes') or 'none'}",
        ]
    )
    response = client.responses.create(
        model=model or get_model(),
        instructions=OUTREACH_INSTRUCTIONS,
        input=f"Message kind: {kind}\n\n{build_fit_input(profile, job)}\n\n{contact_lines}",
        text={
            "format": {
                "type": "json_schema",
                "name": "outreach_draft",
                "schema": OUTREACH_SCHEMA,
                "strict": True,
            }
        },
    )
    result = json.loads(response.output_text)
    if not isinstance(result.get("message"), str) or not result["message"].strip():
        raise ValueError("message must be non-empty text")
    return result


def interview_prep(client, profile, job, resume_text="", model=None):
    """Prepare likely questions, evidence-based answers, gaps, and questions to ask."""
    response = client.responses.create(
        model=model or get_model(),
        instructions=PREP_INSTRUCTIONS,
        input=build_tailor_input(profile, job, resume_text),
        text={
            "format": {
                "type": "json_schema",
                "name": "interview_prep",
                "schema": PREP_SCHEMA,
                "strict": True,
            }
        },
    )
    result = json.loads(response.output_text)
    if not result["likely_questions"]:
        raise ValueError("likely_questions must not be empty")
    return result


def render_prep(result):
    """Format interview prep as plain text for saving and copying."""
    lines = ["LIKELY QUESTIONS"]
    for index, item in enumerate(result["likely_questions"], start=1):
        lines += [
            f"{index}. {item['question']}",
            f"   Why: {item['why_asked']}",
            f"   Answer from your profile: {item['answer_from_profile']}",
            "",
        ]
    lines.append("GAPS TO PREPARE FOR")
    if result["gaps_to_prepare_for"]:
        lines += [f"- {gap}" for gap in result["gaps_to_prepare_for"]]
    else:
        lines.append("- None identified")
    lines += ["", "QUESTIONS TO ASK THEM"]
    lines += [f"- {question}" for question in result["questions_to_ask_them"]]
    return "\n".join(lines) + "\n"


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
    """Turn the saved profile and a job into the text the model reads. Empty fields are skipped."""
    profile_lines = [
        f"- {label}: {profile[field].strip()}"
        for field, label in (
            ("headline", "Headline"),
            ("years_experience", "Years of experience"),
            ("education", "Education"),
            ("summary", "Summary"),
            ("skills", "Skills"),
            ("achievements", "Key achievements"),
        )
        if profile.get(field, "").strip()
    ]
    return "\n".join(
        [
            "CANDIDATE PROFILE",
            *(profile_lines or ["- (no profile details saved)"]),
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


def build_tailor_input(profile, job, resume_text):
    """Combine saved profile, résumé text, and job into the model input."""
    resume_part = (resume_text or "").strip() or "(no résumé text provided)"
    return "\n".join(
        [
            build_fit_input(profile, job),
            "",
            "RÉSUMÉ TEXT",
            resume_part,
        ]
    )


def tailor_application(client, profile, job, resume_text, model=None):
    """Generate a tailored résumé section and cover letter. Returns a validated dict."""
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
    validate_tailor(result)
    return result


def validate_tailor(result):
    """Check tailoring output has the expected text and list fields. Raises ValueError if not."""
    for key in ("headline", "summary", "cover_letter"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise ValueError(f"{key} must be non-empty text")
    for key in ("bullets", "unsupported_requirements", "items_to_confirm"):
        if not isinstance(result.get(key), list) or not all(isinstance(i, str) for i in result[key]):
            raise ValueError(f"{key} must be a list of text")
    if not result["bullets"]:
        raise ValueError("bullets must not be empty")
    return result


def render_resume_draft(result):
    """Format the tailored résumé section as plain text for copying into a résumé."""
    bullets = "\n".join(f"- {bullet}" for bullet in result["bullets"])
    return f"{result['headline']}\n\n{result['summary']}\n\n{bullets}\n"


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
