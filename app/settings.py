"""Saved search settings for the automated pipeline.

These are set once on the Setup page. The scheduled run reads them, so it needs no arguments.
"""

import db

DEFAULTS = {
    "keywords": "director of product, principal, head of product, vp product",
    "locations": "india, bengaluru, mumbai, hyderabad, pune, chennai, uae, dubai, abu dhabi, remote",
    "exclude": "intern, associate, sales, account executive, coordinator",
    "min_score": 70,
    "max_analyses": 10,
}


def load(conn):
    """Return all settings with their types. Missing settings use the defaults."""
    values = {}
    for key, default in DEFAULTS.items():
        raw = db.get_setting(conn, key, None)
        if raw is None:
            values[key] = default
        elif isinstance(default, int):
            values[key] = int(raw)
        else:
            values[key] = raw
    return values


def save(conn, values):
    """Save settings. Unknown keys raise ValueError."""
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown settings: {sorted(unknown)}")
    for key, value in values.items():
        db.set_setting(conn, key, value)
