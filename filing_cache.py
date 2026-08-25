"""
filing_cache.py — Local SQLite + file cache for EDGAR and price data

Eliminates redundant network calls on repeat runs of the same ticker.
The cache is keyed on (ticker, filing_date) so a new 10-K filing
automatically invalidates the old cached data.

What is cached
--------------
Table: filings
  ticker, filing_date, form_type
  → Three statement DataFrames serialised as parquet files
    <cache_dir>/xbrl/<ticker>_<filing_date>_<statement>.parquet

Table: narratives
  ticker, filing_date, source ("8-K" | "10-K")
  earnings_text   TEXT   (up to 25k chars)
  filing_date_8k  TEXT   (ISO date of the 8-K)

Table: prices
  ticker, fetch_date (ISO, today)
  fy_prices_json  TEXT  JSON list of floats/nulls
  periods_json    TEXT  JSON list of period strings (to verify alignment)

What is NOT cached
------------------
  current_price  — must always be live
  market_cap     — must always be live

Cache invalidation
------------------
  filings:    new 10-K filing_date > cached filing_date → re-fetch
  narratives: re-fetched with the filing; same key, INSERT OR REPLACE
  prices:     stale after PRICE_TTL_DAYS (default 1 day for FY prices,
              since they're historical closes and don't change)

Usage (from orchestrator)
-------------------------
    cache = FilingCache(cache_dir=_root)

    # XBRL DataFrames
    hit, financials, filing_meta = cache.get_financials(ticker)
    if not hit:
        # ... load from EDGAR ...
        cache.store_financials(ticker, filing_date, form_type, financials)

    # 8-K narrative
    hit, earnings_text, filing_date_8k = cache.get_narrative(ticker, current_filing_date)
    if not hit:
        # ... fetch from EDGAR ...
        cache.store_narrative(ticker, current_filing_date, "8-K", earnings_text, filing_date_8k)

    # FY prices
    hit, prices = cache.get_prices(ticker, periods)
    if not hit:
        # ... fetch from yfinance ...
        cache.store_prices(ticker, periods, prices)
"""

import json
import logging
import os
import sqlite3
import datetime

logger = logging.getLogger(__name__)

PRICE_TTL_DAYS = 1      # FY-end prices are historical; re-fetch once per day max
NARRATIVE_TTL_DAYS = 7  # Re-check for new 8-K weekly


class FilingCache:
    """
    Local cache for EDGAR filings and price data.

    Parameters
    ----------
    cache_dir : Directory where the SQLite DB and parquet files are stored.
                Defaults to the directory containing this file.
                Will be created if it doesn't exist.
    """

    def __init__(self, cache_dir: str | None = None):
        if cache_dir is None:
            cache_dir = os.path.dirname(os.path.abspath(__file__))
        self._cache_dir  = cache_dir
        self._xbrl_dir   = os.path.join(cache_dir, "xbrl_cache")
        self._db_path    = os.path.join(cache_dir, "filing_cache.db")
        os.makedirs(self._xbrl_dir, exist_ok=True)
        self._init_db()

    # ── Public: XBRL financials ───────────────────────────────────────────────

    def get_financials(self, ticker: str) -> tuple[bool, dict | None, dict | None]:
        """
        Returns (hit, financials_dict, meta_dict).
        financials_dict = {"income_statement": df, "balance_sheet": df, "cash_flow": df}
        meta_dict       = {"filing_date": "2026-02-19", "form_type": "10-K"}
        Returns (False, None, None) on cache miss or any error.
        """
        try:
            with sqlite3.connect(self._db_path) as con:
                con.row_factory = sqlite3.Row
                row = con.execute("""
                    SELECT filing_date, form_type FROM filings
                    WHERE ticker = ?
                    ORDER BY filing_date DESC LIMIT 1
                """, (ticker.upper(),)).fetchone()
            if row is None:
                return False, None, None

            financials = self._load_parquet(ticker, row["filing_date"])
            if financials is None:
                return False, None, None

            logger.info("FilingCache: HIT financials %s (%s %s)",
                        ticker, row["form_type"], row["filing_date"])
            return True, financials, {
                "filing_date": row["filing_date"],
                "form_type":   row["form_type"],
            }
        except Exception as e:
            logger.debug("FilingCache: get_financials error for %s: %s", ticker, e)
            return False, None, None

    def store_financials(self, ticker: str, filing_date: str,
                         form_type: str, financials: dict):
        """
        Store the three statement DataFrames for a ticker.
        financials = {"income_statement": df, "balance_sheet": df, "cash_flow": df}
        """
        try:
            self._save_parquet(ticker, filing_date, financials)
        except Exception as e:
            print(f"[FilingCache] WARNING: parquet save failed for {ticker}: {e}")
            import traceback; traceback.print_exc()
            return
        try:
            with sqlite3.connect(self._db_path) as con:
                con.execute("""
                    INSERT OR REPLACE INTO filings (ticker, filing_date, form_type)
                    VALUES (?, ?, ?)
                """, (ticker.upper(), filing_date, form_type))
            print(f"[FilingCache] Stored: {ticker} {form_type} {filing_date} → xbrl_cache/")
        except Exception as e:
            print(f"[FilingCache] WARNING: DB write failed for {ticker}: {e}")

    def is_filing_current(self, ticker: str, live_filing_date: str) -> bool:
        """
        True if the cached filing_date matches the live filing_date.
        Used to decide whether to skip EDGAR re-fetch.
        """
        try:
            with sqlite3.connect(self._db_path) as con:
                row = con.execute("""
                    SELECT filing_date FROM filings WHERE ticker = ?
                    ORDER BY filing_date DESC LIMIT 1
                """, (ticker.upper(),)).fetchone()
            return row is not None and row[0] == live_filing_date
        except Exception:
            return False

    # ── Public: 8-K narrative ─────────────────────────────────────────────────

    def get_narrative(self, ticker: str, current_filing_date: str
                      ) -> tuple[bool, str | None, str | None]:
        """
        Returns (hit, earnings_text, filing_date_8k).
        Cache miss if: no row exists, or the row is older than NARRATIVE_TTL_DAYS.
        """
        try:
            with sqlite3.connect(self._db_path) as con:
                con.row_factory = sqlite3.Row
                row = con.execute("""
                    SELECT earnings_text, filing_date_8k, fetched_date
                    FROM narratives
                    WHERE ticker = ? AND filing_date = ?
                """, (ticker.upper(), current_filing_date)).fetchone()

            if row is None:
                return False, None, None

            # Expire after TTL to pick up newly filed 8-Ks
            fetched = datetime.date.fromisoformat(row["fetched_date"])
            age     = (datetime.date.today() - fetched).days
            if age > NARRATIVE_TTL_DAYS:
                logger.debug("FilingCache: narrative for %s expired (%d days old)", ticker, age)
                return False, None, None

            logger.info("FilingCache: HIT narrative %s", ticker)
            return True, row["earnings_text"], row["filing_date_8k"]
        except Exception as e:
            logger.debug("FilingCache: get_narrative error for %s: %s", ticker, e)
            return False, None, None

    def store_narrative(self, ticker: str, filing_date: str, source: str,
                        earnings_text: str | None, filing_date_8k: str | None):
        """Store the 8-K/10-K narrative text for a ticker."""
        try:
            today = datetime.date.today().isoformat()
            with sqlite3.connect(self._db_path) as con:
                con.execute("""
                    INSERT OR REPLACE INTO narratives
                        (ticker, filing_date, source, earnings_text,
                         filing_date_8k, fetched_date)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (ticker.upper(), filing_date, source,
                      earnings_text, filing_date_8k, today))
            logger.info("FilingCache: stored narrative %s (source=%s)", ticker, source)
        except Exception as e:
            logger.warning("FilingCache: could not store narrative for %s: %s", ticker, e)

    # ── Public: FY prices ─────────────────────────────────────────────────────

    def get_prices(self, ticker: str, periods: list
                   ) -> tuple[bool, list | None]:
        """
        Returns (hit, prices_list).
        Cache miss if: no row, periods changed, or data older than PRICE_TTL_DAYS.
        """
        try:
            with sqlite3.connect(self._db_path) as con:
                con.row_factory = sqlite3.Row
                row = con.execute("""
                    SELECT fy_prices_json, periods_json, fetch_date
                    FROM prices WHERE ticker = ?
                    ORDER BY fetch_date DESC LIMIT 1
                """, (ticker.upper(),)).fetchone()

            if row is None:
                return False, None

            # Expire after TTL
            fetched = datetime.date.fromisoformat(row["fetch_date"])
            age     = (datetime.date.today() - fetched).days
            if age > PRICE_TTL_DAYS:
                return False, None

            # Verify periods alignment — if FY structure changed, re-fetch
            cached_periods = json.loads(row["periods_json"] or "[]")
            if cached_periods != list(periods):
                logger.debug("FilingCache: periods changed for %s — cache miss", ticker)
                return False, None

            prices = json.loads(row["fy_prices_json"])
            logger.info("FilingCache: HIT prices %s", ticker)
            return True, prices
        except Exception as e:
            logger.debug("FilingCache: get_prices error for %s: %s", ticker, e)
            return False, None

    def store_prices(self, ticker: str, periods: list, prices: list):
        """Store FY-end historical prices for a ticker."""
        try:
            today = datetime.date.today().isoformat()
            with sqlite3.connect(self._db_path) as con:
                con.execute("""
                    INSERT OR REPLACE INTO prices
                        (ticker, fetch_date, fy_prices_json, periods_json)
                    VALUES (?, ?, ?, ?)
                """, (ticker.upper(), today,
                      json.dumps(prices), json.dumps(list(periods))))
            logger.info("FilingCache: stored prices %s", ticker)
        except Exception as e:
            logger.warning("FilingCache: could not store prices for %s: %s", ticker, e)

    # ── Cache management ──────────────────────────────────────────────────────

    def status(self) -> list[dict]:
        """
        Returns a list of dicts summarising cache contents — one row per ticker.
        Useful for query_cache.py.
        """
        try:
            with sqlite3.connect(self._db_path) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute("""
                    SELECT
                        f.ticker,
                        f.filing_date,
                        f.form_type,
                        n.fetched_date   AS narrative_date,
                        n.source         AS narrative_source,
                        p.fetch_date     AS price_date
                    FROM filings f
                    LEFT JOIN narratives n ON n.ticker = f.ticker
                                          AND n.filing_date = f.filing_date
                    LEFT JOIN prices    p ON p.ticker = f.ticker
                    ORDER BY f.ticker
                """).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def clear(self, ticker: str):
        """Remove all cached data for a ticker (forces full re-fetch on next run)."""
        try:
            with sqlite3.connect(self._db_path) as con:
                con.execute("DELETE FROM filings    WHERE ticker = ?", (ticker.upper(),))
                con.execute("DELETE FROM narratives WHERE ticker = ?", (ticker.upper(),))
                con.execute("DELETE FROM prices     WHERE ticker = ?", (ticker.upper(),))
            # Remove parquet files
            for stmt in ("income_statement", "balance_sheet", "cash_flow"):
                for f in os.listdir(self._xbrl_dir):
                    if f.startswith(f"{ticker.upper()}_") and f.endswith(f"_{stmt}.parquet"):
                        os.remove(os.path.join(self._xbrl_dir, f))
            logger.info("FilingCache: cleared %s", ticker)
        except Exception as e:
            logger.warning("FilingCache: could not clear %s: %s", ticker, e)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _init_db(self):
        try:
            with sqlite3.connect(self._db_path) as con:
                con.execute("""
                    CREATE TABLE IF NOT EXISTS filings (
                        ticker       TEXT NOT NULL,
                        filing_date  TEXT NOT NULL,
                        form_type    TEXT,
                        PRIMARY KEY (ticker, filing_date)
                    )
                """)
                con.execute("""
                    CREATE TABLE IF NOT EXISTS narratives (
                        ticker          TEXT NOT NULL,
                        filing_date     TEXT NOT NULL,
                        source          TEXT,
                        earnings_text   TEXT,
                        filing_date_8k  TEXT,
                        fetched_date    TEXT,
                        PRIMARY KEY (ticker, filing_date)
                    )
                """)
                con.execute("""
                    CREATE TABLE IF NOT EXISTS prices (
                        ticker         TEXT NOT NULL,
                        fetch_date     TEXT NOT NULL,
                        fy_prices_json TEXT,
                        periods_json   TEXT,
                        PRIMARY KEY (ticker, fetch_date)
                    )
                """)
        except Exception as e:
            logger.warning("FilingCache: could not init DB: %s", e)

    def _parquet_path(self, ticker: str, filing_date: str, statement: str) -> str:
        fname = f"{ticker.upper()}_{filing_date}_{statement}.parquet"
        return os.path.join(self._xbrl_dir, fname)

    def _save_parquet(self, ticker: str, filing_date: str, financials: dict):
        for stmt, df in financials.items():
            if df is not None:
                path = self._parquet_path(ticker, filing_date, stmt)
                df.to_parquet(path, index=True)
                kb = os.path.getsize(path) // 1024
                print(f"[FilingCache]   wrote {os.path.basename(path)} ({kb} KB)")
            else:
                print(f"[FilingCache]   skipped {stmt} (DataFrame is None)")

    def _load_parquet(self, ticker: str, filing_date: str) -> dict | None:
        import pandas as pd
        result = {}
        for stmt in ("income_statement", "balance_sheet", "cash_flow"):
            path = self._parquet_path(ticker, filing_date, stmt)
            if not os.path.exists(path):
                logger.debug("FilingCache: missing parquet %s", path)
                return None
            try:
                result[stmt] = pd.read_parquet(path)
            except Exception as e:
                logger.debug("FilingCache: parquet read error %s: %s", path, e)
                return None
        return result