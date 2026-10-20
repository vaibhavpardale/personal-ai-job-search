"""Run the daily search and review queue from the command line.

Designed for a scheduler (cron or launchd). It searches the watchlist, scores new postings,
tailors drafts for strong matches, and adds them to the review queue. It never submits anything.

    python app/run_daily.py --keywords "director of product, principal" --min-score 70
"""

import argparse
import sys
from contextlib import closing

import ai
import db
import discovery
import profile_store
import review_queue


def build_parser():
    parser = argparse.ArgumentParser(description="Search, match, and queue jobs for review.")
    parser.add_argument(
        "--keywords",
        default="director of product, principal, head of product, vp product",
        help="Comma-separated title words to keep.",
    )
    parser.add_argument("--min-score", type=int, default=review_queue.DEFAULT_MIN_SCORE)
    parser.add_argument("--max-analyses", type=int, default=review_queue.DEFAULT_MAX_ANALYSES)
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
            print("The watchlist is empty. Add company boards in the Discover tab first.", file=sys.stderr)
            return 1
        resume_text = profile_store.latest_resume_text(conn)
        if not resume_text:
            print("Warning: no résumé uploaded. Tailored drafts will be weaker.", file=sys.stderr)
        summary = review_queue.run_daily(
            conn,
            boards,
            discovery.parse_keywords(args.keywords),
            client,
            profile_store.get_profile(conn),
            resume_text,
            min_score=args.min_score,
            max_analyses=args.max_analyses,
        )

    print(f"Postings found: {summary['found']}")
    print(f"New to tracker: {summary['new']}")
    print(f"Analyzed: {summary['analyzed']}")
    print(f"Added to review queue: {summary['queued']}")
    for error in summary["errors"]:
        print(f"Problem: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
