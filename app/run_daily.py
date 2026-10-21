"""Run the automated job pipeline from the command line. Designed for a scheduler.

It reads your watchlist and saved search settings, finds new postings, scores them, prepares a
tailored PDF résumé and cover letter for strong matches, and adds them to the review queue.
It never submits, sends, or messages anything.

    python app/run_daily.py                 # uses the saved settings
    python app/run_daily.py --min-score 80  # one-off override
"""

import argparse
import sys
from contextlib import closing

import ai
import db
import discovery
import profile_store
import review_queue
import settings


def build_parser():
    parser = argparse.ArgumentParser(description="Search, match, tailor, and queue jobs for review.")
    parser.add_argument("--min-score", type=int, default=None, help="Override the saved minimum score.")
    parser.add_argument("--max-analyses", type=int, default=None, help="Override the saved per-run limit.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        client = ai.get_client()
    except ai.MissingApiKeyError as exc:
        print(exc, file=sys.stderr)
        return 1

    with closing(db.connect()) as conn:
        boards = db.list_watched_boards(conn)
        if not boards:
            print("The watchlist is empty. Add company boards on the Setup page first.", file=sys.stderr)
            return 1
        saved = settings.load(conn)
        resume_text = profile_store.latest_resume_text(conn)
        if not resume_text:
            print("Warning: no résumé uploaded. Tailored résumés will be weaker.", file=sys.stderr)
        summary = review_queue.run_daily(
            conn,
            boards,
            discovery.parse_keywords(saved["keywords"]),
            client,
            profile_store.get_profile(conn),
            resume_text,
            locations=discovery.parse_keywords(saved["locations"]),
            exclude=discovery.parse_keywords(saved["exclude"]),
            min_score=args.min_score if args.min_score is not None else saved["min_score"],
            max_analyses=args.max_analyses if args.max_analyses is not None else saved["max_analyses"],
        )

    print(f"Postings found: {summary['found']}")
    print(f"New to tracker: {summary['new']}")
    print(f"Already tracked from another board: {summary['duplicates']}")
    print(f"Analyzed: {summary['analyzed']}")
    print(f"Added to review queue: {summary['queued']}")
    for error in summary["errors"]:
        print(f"Problem: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
