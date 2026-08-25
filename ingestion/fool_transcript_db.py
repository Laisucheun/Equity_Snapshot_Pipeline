"""
fool_transcript_db.py — Motley Fool transcript URL database

Builds and maintains a SQLite database of transcript URLs scraped from
Motley Fool's monthly sitemaps (fool.com/sitemap/YYYY/MM).

The sitemaps are plain text (no JS rendering, no Cloudflare issues) and
contain every transcript URL published that month. One sitemap page = one
GET request = ~200-500 transcript URLs.

Schema:
    transcripts (ticker, date, quarter, fiscal_year, url, scraped_at)
    PRIMARY KEY (ticker, date)

Usage:
    # Build initial database from all available sitemaps (2016-present)
    python fool_transcript_db.py --build

    # Update with latest month only
    python fool_transcript_db.py --update

    # Look up a ticker
    python fool_transcript_db.py --lookup NKE

    # Query the DB directly
    python fool_transcript_db.py
"""

import os
import re
import time
import sqlite3
import datetime
import argparse
import requests
import logging

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fool_transcripts.db"
)
_SITEMAP_BASE = "https://www.fool.com/sitemap"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_TRANSCRIPT_RE = re.compile(
    r'https?://www\.fool\.com/earnings/call-transcripts/'
    r'(\d{4})/(\d{2})/(\d{2})/'
    r'([a-z0-9\-]+)-([a-z0-9]+)-q([1-4])-(\d{4})-earnings(?:-call)?-transcript[^/\s]*',
    re.IGNORECASE
)
_TRANSCRIPT_NOQ_RE = re.compile(
    r'https?://www\.fool\.com/earnings/call-transcripts/'
    r'(\d{4})/(\d{2})/(\d{2})/'
    r'([a-z0-9\-]+-([a-z0-9]{1,6})-(?:earnings|q[1-4]))[^/\s]*',
    re.IGNORECASE
)
_DELAY = 2.0   # seconds between sitemap requests


# ── Database ──────────────────────────────────────────────────────────────────

def get_db(path: str = _DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS transcripts (
            ticker      TEXT NOT NULL,
            date        TEXT NOT NULL,
            quarter     INTEGER,
            fiscal_year INTEGER,
            url         TEXT NOT NULL,
            scraped_at  TEXT NOT NULL,
            PRIMARY KEY (ticker, date)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_ticker ON transcripts(ticker)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_date   ON transcripts(date)")
    con.commit()
    return con


def upsert(con: sqlite3.Connection, rows: list[dict]):
    """Insert or replace transcript records."""
    con.executemany("""
        INSERT OR REPLACE INTO transcripts
            (ticker, date, quarter, fiscal_year, url, scraped_at)
        VALUES
            (:ticker, :date, :quarter, :fiscal_year, :url, :scraped_at)
    """, rows)
    con.commit()


def lookup(con: sqlite3.Connection, ticker: str, latest: bool = True) -> list[sqlite3.Row]:
    """
    Look up transcript records for a ticker.
    If latest=True, return only the most recent entry.
    """
    ticker = ticker.upper()
    if latest:
        return con.execute(
            "SELECT * FROM transcripts WHERE ticker=? ORDER BY date DESC LIMIT 1",
            (ticker,)
        ).fetchall()
    return con.execute(
        "SELECT * FROM transcripts WHERE ticker=? ORDER BY date DESC",
        (ticker,)
    ).fetchall()


# ── Sitemap parsing ───────────────────────────────────────────────────────────

def _parse_transcript_url(url: str) -> dict | None:
    """
    Parse a transcript URL into structured fields.

    URL patterns:
      /earnings/call-transcripts/YYYY/MM/DD/slug-TICKER-qQ-YYYY-earnings-call-transcript/
      /earnings/call-transcripts/YYYY/MM/DD/slug-TICKER-earnings-transcript/  (no quarter)
    """
    url = url.strip().rstrip('/')

    # Pattern 1: has quarter
    m = _TRANSCRIPT_RE.match(url)
    if m:
        year, month, day = m.group(1), m.group(2), m.group(3)
        ticker  = m.group(5).upper()
        quarter = int(m.group(6))
        fy      = int(m.group(7))
        return {
            "ticker":      ticker,
            "date":        f"{year}-{month}-{day}",
            "quarter":     quarter,
            "fiscal_year": fy,
            "url":         url,
            "scraped_at":  datetime.datetime.utcnow().isoformat(),
        }

    # Pattern 2: extract ticker from slug — look for uppercase-able short segment
    # e.g. nike-nke-q3-2026 → NKE, or palantir-pltr-q3-2024 → PLTR
    date_m = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
    if not date_m:
        return None

    year, month, day = date_m.group(1), date_m.group(2), date_m.group(3)
    slug = url.split('/')[-1]

    # Try to find TICKER-qQ-YYYY pattern in slug
    tq = re.search(r'-([a-z]{1,6})-q([1-4])-(\d{4})', slug, re.IGNORECASE)
    if tq:
        return {
            "ticker":      tq.group(1).upper(),
            "date":        f"{year}-{month}-{day}",
            "quarter":     int(tq.group(2)),
            "fiscal_year": int(tq.group(3)),
            "url":         url,
            "scraped_at":  datetime.datetime.utcnow().isoformat(),
        }

    # Last resort: grab 2nd-to-last segment before -earnings or -transcript
    t2 = re.search(r'-([a-z]{1,6})-(?:earnings|transcript)', slug, re.IGNORECASE)
    if t2:
        ticker = t2.group(1).upper()
        if 2 <= len(ticker) <= 6:
            return {
                "ticker":      ticker,
                "date":        f"{year}-{month}-{day}",
                "quarter":     None,
                "fiscal_year": None,
                "url":         url,
                "scraped_at":  datetime.datetime.utcnow().isoformat(),
            }

    return None


def scrape_sitemap(year: int, month: int, session: requests.Session) -> list[dict]:
    """
    Fetch fool.com/sitemap/YYYY/MM and return parsed transcript records.
    Returns empty list on failure.
    """
    url = f"{_SITEMAP_BASE}/{year}/{month:02d}"
    try:
        resp = session.get(url, headers=_HEADERS, timeout=15)
        if resp.status_code == 403:
            logger.warning("fool_transcript_db: 403 on sitemap %d/%02d", year, month)
            return []
        if resp.status_code != 200:
            logger.debug("fool_transcript_db: %d on sitemap %d/%02d", resp.status_code, year, month)
            return []

        # Extract all transcript URLs from the plain-text sitemap
        urls = re.findall(
            r'https?://www\.fool\.com/earnings/call-transcripts/[^\s<>"]+',
            resp.text, re.IGNORECASE
        )

        rows = []
        for u in urls:
            parsed = _parse_transcript_url(u)
            if parsed:
                rows.append(parsed)

        logger.info(
            "fool_transcript_db: sitemap %d/%02d — %d URLs, %d parsed",
            year, month, len(urls), len(rows)
        )
        return rows

    except Exception as e:
        logger.warning("fool_transcript_db: error on sitemap %d/%02d — %s", year, month, e)
        return []


# ── Build / Update ────────────────────────────────────────────────────────────

def get_scraped_months(con: sqlite3.Connection) -> set[tuple]:
    """Return set of (year, month) tuples already in the DB."""
    rows = con.execute(
        "SELECT DISTINCT substr(date,1,4) AS y, substr(date,6,2) AS m FROM transcripts"
    ).fetchall()
    return {(int(r["y"]), int(r["m"])) for r in rows}


def build(con: sqlite3.Connection, start_year: int = 2016, verbose: bool = True):
    """
    Scrape all sitemaps from start_year to current month.
    Skips months already in the DB unless forced.
    """
    today         = datetime.date.today()
    scraped       = get_scraped_months(con)
    session       = requests.Session()
    total_new     = 0
    blocked       = False

    months = []
    for year in range(start_year, today.year + 1):
        for month in range(1, 13):
            if year == today.year and month > today.month:
                break
            if (year, month) not in scraped:
                months.append((year, month))

    if not months:
        if verbose:
            print("All months already scraped. Use --update for latest month.")
        return

    if verbose:
        print(f"Scraping {len(months)} months ({months[0][0]}/{months[0][1]:02d} "
              f"to {months[-1][0]}/{months[-1][1]:02d})...")
        print(f"Estimated time: {len(months) * _DELAY / 60:.1f} minutes\n")

    for i, (year, month) in enumerate(months, 1):
        rows = scrape_sitemap(year, month, session)
        if rows is None:  # 403 block
            blocked = True
            break

        if rows:
            upsert(con, rows)
            total_new += len(rows)

        if verbose:
            count = con.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
            print(f"  [{i}/{len(months)}] {year}/{month:02d}: "
                  f"+{len(rows)} records (total: {count})", end="\r")

        if i < len(months):
            time.sleep(_DELAY)

    if verbose:
        print()
        count = con.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
        print(f"\nDone. {total_new} new records. DB total: {count}")
        if blocked:
            print("Stopped early due to 403 block. Re-run to continue.")


def update(con: sqlite3.Connection, months_back: int = 2, verbose: bool = True):
    """
    Update with the latest N months of sitemaps.
    Designed for monthly cron — fast, minimal requests.
    """
    today   = datetime.date.today()
    session = requests.Session()
    total   = 0

    for i in range(months_back):
        # Go back i months from today
        d     = today.replace(day=1) - datetime.timedelta(days=i * 28)
        year  = d.year
        month = d.month
        rows  = scrape_sitemap(year, month, session)
        if rows:
            upsert(con, rows)
            total += len(rows)
            if verbose:
                print(f"  {year}/{month:02d}: +{len(rows)} records")
        if i < months_back - 1:
            time.sleep(_DELAY)

    if verbose:
        count = con.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
        print(f"\nUpdate complete. {total} new/updated records. DB total: {count}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Motley Fool transcript URL database")
    parser.add_argument("--build",       action="store_true", help="Build full DB from 2016")
    parser.add_argument("--update",      action="store_true", help="Update with latest 2 months")
    parser.add_argument("--lookup",      metavar="TICKER",    help="Look up a ticker")
    parser.add_argument("--start-year",  type=int, default=2016)
    parser.add_argument("--db",          default=_DB_PATH,    help="Database path")
    parser.add_argument("--verbose",     action="store_true", default=True)
    args = parser.parse_args()

    con = get_db(args.db)

    if args.build:
        build(con, start_year=args.start_year, verbose=args.verbose)

    elif args.update:
        update(con, months_back=2, verbose=args.verbose)

    elif args.lookup:
        rows = lookup(con, args.lookup, latest=False)
        if not rows:
            print(f"{args.lookup.upper()}: not found in DB")
        else:
            print(f"\n{args.lookup.upper()} — {len(rows)} transcript(s):")
            print(f"{'Date':<14} {'Q':>3} {'FY':>6}  URL")
            print("─" * 80)
            for r in rows:
                q  = str(r['quarter'])     if r['quarter']     else "?"
                fy = str(r['fiscal_year']) if r['fiscal_year'] else "?"
                print(f"{r['date']:<14} {q:>3} {fy:>6}  {r['url'][:55]}")

    else:
        # Summary
        count = con.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
        tickers = con.execute("SELECT COUNT(DISTINCT ticker) FROM transcripts").fetchone()[0]
        latest = con.execute("SELECT MAX(date) FROM transcripts").fetchone()[0]
        oldest = con.execute("SELECT MIN(date) FROM transcripts").fetchone()[0]
        print(f"\nDB: {args.db}")
        print(f"Records:  {count:,}")
        print(f"Tickers:  {tickers:,}")
        print(f"Date range: {oldest} to {latest}")
        print(f"\nUsage:")
        print(f"  python fool_transcript_db.py --build       # scrape all months 2016-now")
        print(f"  python fool_transcript_db.py --update      # update latest 2 months")
        print(f"  python fool_transcript_db.py --lookup NKE  # show NKE entries")


def needs_update(con: sqlite3.Connection) -> bool:
    """
    Returns True if current or previous month is missing from DB.
    """
    today   = datetime.date.today()
    scraped = get_scraped_months(con)
    for months_back in range(2):
        d     = today.replace(day=1) - datetime.timedelta(days=months_back * 28)
        if (d.year, d.month) not in scraped:
            return True
    return False


def maybe_update(db_path: str = _DB_PATH, verbose: bool = True) -> bool:
    """
    Auto-check and update the DB if current/previous month is missing.
    Call from the pipeline before TranscriptLoader is used.

    Returns True if update ran, False if already current.
    """
    if not os.path.exists(db_path):
        if verbose:
            print("[TranscriptDB] DB not found — run: python fool_transcript_db.py --build")
        return False

    con = get_db(db_path)
    if not needs_update(con):
        logger.debug("TranscriptDB: already current")
        con.close()
        return False

    latest = con.execute("SELECT MAX(date) FROM transcripts").fetchone()[0]
    today  = datetime.date.today()
    if verbose:
        print(f"[TranscriptDB] Out of date (latest: {latest}, today: {today}) — updating...")

    update(con, months_back=2, verbose=verbose)
    con.close()
    return True


if __name__ == "__main__":
    main()
