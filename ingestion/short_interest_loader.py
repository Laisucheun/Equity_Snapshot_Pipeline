"""
short_interest_loader.py — Short Interest Fetcher (yfinance + SQLite cache)

Architecture
------------
Source : yfinance Ticker.info — exposes FINRA-sourced short interest fields
         that Yahoo Finance republishes (sharesShort, shortRatio, etc.).
         FINRA itself requires member-firm registration / OAuth for direct
         API access (see DEBT_PIPELINE_README.md notes on this), so yfinance
         is the practical free, no-registration source — same library this
         pipeline already depends on for institutional ownership data.
Cache  : SQLite (short_interest_history.db) — one normalised table:

    snapshots — one row per (ticker, run_date): summary-level metrics

Reporting cadence
------------------
FINRA requires member firms to report short interest twice a month
(mid-month and end-of-month settlement dates), with ~2-week publication
lag. yfinance's `dateShortInterest` field reflects the settlement date of
the most recent published cycle, not "today" — this can be 1-3 weeks
stale at any given moment. Always surfaced in the returned dict via
`_as_of` so the report shows the real data date, not the fetch date.

Returned dict — public interface
---------------------------------
{
    "shares_short":              94_308_265,
    "shares_short_prior_month":  108_782_648,
    "pct_change_mom":            -0.133,        # decimal, -13.3%
    "days_to_cover":             1.66,           # short_ratio
    "short_pct_of_float":        0.0062,         # decimal, 0.62%
    "short_pct_of_shares_out":   0.0062,
    "_source":   "yfinance (FINRA-sourced)",
    "_as_of":    "2026-06-13",      # settlement date of the data, not fetch date
}
# Empty dict {} returned when no usable data found — never raises.

Interpretation notes (for the calling agent / renderer, not enforced here)
----------------------------------------------------------------------------
    days_to_cover (short_ratio) is the standard "squeeze potential" gauge:
    higher means it would take longer for all short sellers to cover their
    positions at average trading volume. There's no universal "high"
    threshold — it varies hugely by sector and float size — so this module
    deliberately does not classify a level as a flag; that's a judgment
    call for the renderer/agent layer, ideally informed by sector context.

    short_pct_of_float rising sharply month-over-month alongside negative
    or deteriorating fundamentals (declining guidance, downgraded credit,
    insider selling) is a more meaningful combined signal than short
    interest alone — pairing with the existing fundamentals/guidance/
    insider sections in the report is more informative than this module
    in isolation.
"""

import logging
import os
import sqlite3
import datetime

logger = logging.getLogger(__name__)


class ShortInterestLoader:
    """
    Fetches current short interest data via yfinance and maintains a
    SQLite history for month-over-month comparison.

    Parameters
    ----------
    db_path : Path to short_interest_history.db.
              Defaults to short_interest_history.db next to this file.
    """

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "short_interest_history.db"
            )
        self._db_path = db_path
        self._init_db()

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, ticker: str) -> dict:
        """
        Fetch current short interest snapshot for a ticker.
        Never raises. Returns {} when no usable data is found.
        """
        ticker = ticker.upper()
        current = self._fetch_yfinance(ticker)
        if not current:
            logger.warning("ShortInterestLoader: no data for %s", ticker)
            return {}

        run_date = datetime.date.today().isoformat()
        self._store_snapshot(ticker, run_date, current)
        return current

    # ── Fetch via yfinance ────────────────────────────────────────────────────

    def _fetch_yfinance(self, ticker: str) -> dict:
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("ShortInterestLoader: yfinance not installed")
            return {}

        try:
            info = yf.Ticker(ticker).info
        except Exception as e:
            logger.warning("ShortInterestLoader: fetch failed for %s — %s",
                          ticker, e)
            return {}

        if not info:
            return {}

        shares_short       = info.get("sharesShort")
        shares_short_prior = info.get("sharesShortPriorMonth")
        days_to_cover       = info.get("shortRatio")
        short_pct_float     = info.get("shortPercentOfFloat")
        short_pct_out       = info.get("sharesPercentSharesOut")
        date_short_epoch     = info.get("dateShortInterest")

        if shares_short is None and days_to_cover is None:
            logger.debug("ShortInterestLoader: %s has no short interest fields", ticker)
            return {}

        pct_change_mom = None
        if shares_short is not None and shares_short_prior:
            try:
                pct_change_mom = (shares_short - shares_short_prior) / shares_short_prior
            except (TypeError, ZeroDivisionError):
                pct_change_mom = None

        as_of = "unknown settlement date"
        if date_short_epoch:
            try:
                as_of = datetime.date.fromtimestamp(date_short_epoch).isoformat()
            except (TypeError, ValueError, OSError):
                pass

        return {
            "shares_short":             shares_short,
            "shares_short_prior_month": shares_short_prior,
            "pct_change_mom":           pct_change_mom,
            "days_to_cover":            days_to_cover,
            "short_pct_of_float":       short_pct_float,
            "short_pct_of_shares_out":  short_pct_out,
            "_source":  "yfinance (FINRA-sourced)",
            "_as_of":   as_of,
        }

    # ── SQLite cache ──────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self._db_path) as con:
                con.execute("""
                    CREATE TABLE IF NOT EXISTS snapshots (
                        ticker                  TEXT NOT NULL,
                        run_date                TEXT NOT NULL,
                        shares_short            INTEGER,
                        shares_short_prior      INTEGER,
                        pct_change_mom          REAL,
                        days_to_cover           REAL,
                        short_pct_of_float      REAL,
                        short_pct_of_shares_out REAL,
                        as_of_settlement        TEXT,
                        fetched_at              TEXT,
                        PRIMARY KEY (ticker, run_date)
                    )
                """)
                con.commit()
        except Exception as e:
            logger.warning("ShortInterestLoader: DB init failed — %s", e)

    def _store_snapshot(self, ticker: str, run_date: str, data: dict) -> None:
        try:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            with sqlite3.connect(self._db_path) as con:
                con.execute("""
                    INSERT OR REPLACE INTO snapshots
                        (ticker, run_date, shares_short, shares_short_prior,
                         pct_change_mom, days_to_cover, short_pct_of_float,
                         short_pct_of_shares_out, as_of_settlement, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ticker, run_date,
                    data.get("shares_short"),
                    data.get("shares_short_prior_month"),
                    data.get("pct_change_mom"),
                    data.get("days_to_cover"),
                    data.get("short_pct_of_float"),
                    data.get("short_pct_of_shares_out"),
                    data.get("_as_of"),
                    now,
                ))
                con.commit()
        except Exception as e:
            logger.warning("ShortInterestLoader: DB store failed for %s — %s",
                          ticker, e)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"

    loader = ShortInterestLoader()
    result = loader.fetch(ticker)
    if result:
        print(f"\n{ticker} — short interest (as of {result['_as_of']})")
        ss = result.get("shares_short")
        print(f"Shares short:        {ss:,}" if ss is not None else "Shares short:        N/A")
        ssp = result.get("shares_short_prior_month")
        print(f"Prior month:         {ssp:,}" if ssp is not None else "Prior month:         N/A")
        pct = result.get("pct_change_mom")
        print(f"MoM change:          {pct*100:+.1f}%" if pct is not None else "MoM change:          N/A")
        dtc = result.get("days_to_cover")
        print(f"Days to cover:       {dtc:.2f}" if dtc is not None else "Days to cover:       N/A")
        spf = result.get("short_pct_of_float")
        print(f"% of float:          {spf*100:.2f}%" if spf is not None else "% of float:          N/A")
    else:
        print(f"No short interest data found for {ticker}.")
