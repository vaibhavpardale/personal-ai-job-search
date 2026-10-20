"""Job discovery from public job-board APIs (Stage 9).

Sources are the public posting APIs that Greenhouse and Lever provide for companies' own job
boards. The app only reads the boards you add to the watchlist. It does not scrape websites,
log in, or touch private data.

Requests are made one at a time with a timeout, and there is a short pause between boards.
"""

import html
import json
import re
import time
import urllib.error
import urllib.request

PROVIDERS = ("Greenhouse", "Lever")

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{token}?mode=json"
USER_AGENT = "personal-job-search/0.1 (personal use; reads public job board APIs)"
REQUEST_TIMEOUT_SECONDS = 20
PAUSE_BETWEEN_BOARDS_SECONDS = 1.0


class DiscoveryError(RuntimeError):
    """A board could not be read. The message is safe to show to the user."""


def source_key(provider, token):
    """Identify a board in the database, for example 'Greenhouse:stripe'."""
    return f"{provider}:{token}"


def fetch_json(url, timeout=REQUEST_TIMEOUT_SECONDS):
    """GET a URL and parse JSON. Raises DiscoveryError with a readable message on failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise DiscoveryError("Board not found. Check the board name.") from exc
        raise DiscoveryError(f"The job board returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DiscoveryError(f"Could not reach the job board: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DiscoveryError("The job board returned data that is not JSON.") from exc


def strip_html(text):
    """Turn HTML job content into readable plain text."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def normalize_greenhouse(token, label, data):
    """Convert a Greenhouse board response into the shared job shape."""
    jobs = []
    for posting in data.get("jobs", []):
        jobs.append(
            {
                "title": posting.get("title", "").strip(),
                "company": label,
                "location": (posting.get("location") or {}).get("name") or None,
                "url": posting.get("absolute_url") or None,
                "description": strip_html(posting.get("content")) or None,
                "source": source_key("Greenhouse", token),
                "external_id": str(posting["id"]),
            }
        )
    return jobs


def normalize_lever(token, label, data):
    """Convert a Lever postings response (a list) into the shared job shape."""
    jobs = []
    for posting in data:
        categories = posting.get("categories") or {}
        description = posting.get("descriptionPlain") or strip_html(posting.get("description"))
        jobs.append(
            {
                "title": (posting.get("text") or "").strip(),
                "company": label,
                "location": categories.get("location") or None,
                "url": posting.get("hostedUrl") or None,
                "description": (description or "").strip() or None,
                "source": source_key("Lever", token),
                "external_id": str(posting["id"]),
            }
        )
    return jobs


def fetch_board(provider, token, label, fetch=fetch_json):
    """Read one board and return normalized jobs. Raises DiscoveryError if it cannot be read."""
    if provider == "Greenhouse":
        return normalize_greenhouse(token, label, fetch(GREENHOUSE_URL.format(token=token)))
    if provider == "Lever":
        return normalize_lever(token, label, fetch(LEVER_URL.format(token=token)))
    raise ValueError(f"Unknown provider: {provider!r}")


def parse_keywords(text):
    """Split a comma-separated keyword string into a clean list."""
    return [keyword.strip().lower() for keyword in (text or "").split(",") if keyword.strip()]


def filter_by_keywords(jobs, keywords):
    """Keep jobs whose title contains any keyword. An empty keyword list keeps everything."""
    if not keywords:
        return list(jobs)
    return [job for job in jobs if any(keyword in job["title"].lower() for keyword in keywords)]


def discover(boards, keywords, fetch=fetch_board, pause=time.sleep):
    """Check each watched board in turn. Returns (jobs, errors).

    One board failing does not stop the others. Errors are returned as messages for the UI.
    """
    jobs, errors = [], []
    for index, board in enumerate(boards):
        if index:
            pause(PAUSE_BETWEEN_BOARDS_SECONDS)
        try:
            jobs.extend(fetch(board["provider"], board["token"], board["label"]))
        except DiscoveryError as exc:
            errors.append(f"{board['label']} ({board['provider']}): {exc}")
    return filter_by_keywords(jobs, keywords), errors
