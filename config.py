"""
config.py — Centralised API key and static data loader

All keys are read from .env in the project root.
Credit ratings are read from ratings.csv in the project root.

Usage:
    from config import FRED_API_KEY, get_rating
"""

import os
import csv
import pathlib
from dotenv import load_dotenv

load_dotenv()

FRED_API_KEY      = os.environ.get("FRED_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

if not FRED_API_KEY:
    import logging
    logging.getLogger(__name__).warning(
        "FRED_API_KEY not set — credit spread data will be unavailable. "
        "Add FRED_API_KEY to your .env file."
    )

# ── Credit ratings static table ───────────────────────────────────────────────
# Loaded from ratings.csv: ticker,sp_rating,moodys_rating,fitch_rating,outlook,as_of_date
# Add new tickers to ratings.csv before running the pipeline.

_RATINGS: dict[str, dict] = {}

def _load_ratings() -> dict[str, dict]:
    path = pathlib.Path(__file__).parent / "ratings.csv"
    result = {}
    if not path.exists():
        import logging
        logging.getLogger(__name__).warning(
            "ratings.csv not found at %s — credit ratings will show N/A. "
            "Create ratings.csv with columns: "
            "ticker,sp_rating,moodys_rating,fitch_rating,outlook,as_of_date",
            path
        )
        return result
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ticker = row.get("ticker", "").strip().upper()
                if ticker:
                    result[ticker] = {
                        "sp_rating":      row.get("sp_rating",      "").strip() or None,
                        "moodys_rating":  row.get("moodys_rating",  "").strip() or None,
                        "fitch_rating":   row.get("fitch_rating",   "").strip() or None,
                        "outlook":        row.get("outlook",        "").strip() or "N/A",
                        "as_of_date":     row.get("as_of_date",     "").strip() or "N/A",
                    }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("ratings.csv load error: %s", e)
    return result

_RATINGS = _load_ratings()


def get_rating(ticker: str) -> dict:
    """
    Returns credit rating dict for a ticker.
    Keys: sp_rating, moodys_rating, fitch_rating, outlook, as_of_date
    All values default to None/"N/A" when not found.
    Logs a warning when ticker is missing from ratings.csv.
    """
    ticker = ticker.upper()
    if ticker in _RATINGS:
        return _RATINGS[ticker]

    import logging
    logging.getLogger(__name__).warning(
        "No credit rating for %s in ratings.csv — add it to populate credit quality section.",
        ticker
    )
    return {
        "sp_rating":     None,
        "moodys_rating": None,
        "fitch_rating":  None,
        "outlook":       "N/A",
        "as_of_date":    "N/A",
    }
