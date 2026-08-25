"""
ownership_loader.py — Institutional Ownership Fetcher (13-F / yfinance + SQLite cache)

Architecture
------------
Tier 1 : yfinance — fetch current snapshot (top-10 holders, % institutional, % insider).
Cache  : SQLite (ownership_history.db) — two normalised tables:

    snapshots  — one row per (ticker, run_date): summary-level metrics
    holders    — one row per (ticker, run_date, rank): individual holder positions

This lets you query holder positions directly in SQL without JSON parsing:

    -- All tickers where Vanguard holds > 5%
    SELECT ticker, run_date, pct FROM holders
    WHERE name LIKE '%Vanguard%' AND pct > 0.05 ORDER BY pct DESC;

    -- Vanguard's position history in AAPL
    SELECT run_date, shares, pct FROM holders
    WHERE ticker = 'AAPL' AND name LIKE '%Vanguard%' ORDER BY run_date;

    -- Institutional % trend for MSFT
    SELECT run_date, ROUND(institutional_pct*100,2) FROM snapshots
    WHERE ticker = 'MSFT' ORDER BY run_date;

DB schema
---------
Table: snapshots
    ticker                  TEXT  (PK with run_date)
    run_date                TEXT  ISO date e.g. "2026-06-18"
    institutional_pct       REAL  decimal 0.712 = 71.2%
    insider_pct             REAL
    top10_concentration_pct REAL
    source                  TEXT

Table: holders
    ticker    TEXT  (PK with run_date, rank)
    run_date  TEXT
    rank      INTEGER  1 = largest holder
    name      TEXT
    pct       REAL     decimal fraction
    shares    INTEGER

Returned dict — public interface unchanged
------------------------------------------
{
    "institutional_pct":            0.712,
    "insider_pct":                  0.034,
    "top10_concentration_pct":      0.381,
    "top_holders": [
        {
            "name":             "Vanguard Group Inc",
            "pct":              0.0842,
            "shares":           1_320_000_000,
            "delta_1yr_shares": +12_500_000,   # None until 1yr of history exists
            "delta_1yr_pct":    +0.005,
            "delta_3yr_shares": None,
            "delta_3yr_pct":    None,
        },
        ...
    ],
    "institutional_pct_delta_1yr":  +0.021,    # None until history exists
    "institutional_pct_delta_3yr":  None,
    "net_change_shares":            "See Δ columns in holder table",
    "_source":     "yfinance institutional_holders",
    "_as_of":      "current (13-F filings lag ~45 days)",
    "_tier":       1,
    "_delta_note": "Δ 1yr vs 2025-06-15",      # None until history exists
}
# Empty dict {} returned when all tiers fail — never raises.

Flag rules (applied in FundamentalAgent)
-----------------------------------------
    institutional_pct < 0.20            → low institutional ownership
    top10_concentration_pct > 0.40      → crowding risk
    institutional_pct_delta_1yr < -0.05 → institutions reducing exposure ≥5pp over 1yr
"""

import logging
import math
import os
import sqlite3
import datetime

logger = logging.getLogger(__name__)

_QTR_TARGET_DAYS = 91
_6MO_TARGET_DAYS = 182
_1YR_TARGET_DAYS = 365
_3YR_TARGET_DAYS = 1095
_WINDOW_DAYS     = 90    # accept snapshot within ±90 days of target


class OwnershipLoader:
    """
    Fetches institutional ownership data and maintains a normalised SQLite history.

    Parameters
    ----------
    db_path : Path to ownership_history.db.
              Defaults to ownership_history.db next to this file.
    """

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "ownership_history.db"
            )
        self._db_path = db_path
        self._init_db()

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, ticker: str, shares_outstanding: int | None = None) -> dict:
        """
        Fetch current ownership snapshot, store it, attach historical deltas.
        Never raises. Returns {} when yfinance returns no usable data.

        Parameters
        ----------
        ticker             : e.g. "AAPL"
        shares_outstanding : Diluted share count from most recent filing.
                             Used to compute pct = shares / shares_outstanding
                             when yfinance does not supply the % Out column.
        """
        ticker = ticker.upper()
        today  = datetime.date.today().isoformat()

        current = self._fetch_yfinance(ticker, shares_outstanding)
        if not current:
            logger.warning("OwnershipLoader: all tiers failed for %s", ticker)
            return {}

        self._store_snapshot(ticker, today, current)
        self._attach_deltas(ticker, today, current)
        return current

    # ── Tier 1: yfinance ─────────────────────────────────────────────────────

    def _fetch_yfinance(self, ticker: str, shares_outstanding: int | None) -> dict:
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)

            major             = t.major_holders
            institutional_pct = _parse_major(major, 1)
            insider_pct       = _parse_major(major, 0)

            inst        = t.institutional_holders
            top10_pct   = _top10_concentration(inst, shares_outstanding)
            top_holders = _top10_holders(inst, shares_outstanding)

            if institutional_pct is None and top10_pct is None:
                logger.debug("OwnershipLoader: yfinance returned no usable data for %s", ticker)
                return {}

            # Sanity check: if institutional_pct < 5% but top-10 holders
            # already account for >20% of shares, the major_holders parse
            # returned a wrong/stale value. Recompute from holder shares sum.
            # This fixes MU (0.8% shown vs ~75% actual) and similar tickers.
            if (institutional_pct is not None and institutional_pct < 0.05
                    and top10_pct is not None and top10_pct > 0.20):
                logger.info(
                    "OwnershipLoader: %s institutional_pct %.1f%% implausible "
                    "(top10=%.1f%%) — using top10_pct as floor",
                    ticker, institutional_pct * 100, top10_pct * 100
                )
                # Best estimate: institutional_pct ≥ top10_concentration
                # (actual is higher since we only have top 10)
                institutional_pct = top10_pct

            return {
                "institutional_pct":           institutional_pct,
                "insider_pct":                 insider_pct,
                "top10_concentration_pct":     top10_pct,
                "top_holders":                 top_holders,
                "institutional_pct_delta_1yr": None,
                "institutional_pct_delta_3yr": None,
                "net_change_shares":           "N/A (builds after first re-run)",
                "_source":     "yfinance institutional_holders",
                "_as_of":      "current (13-F filings lag ~45 days)",
                "_tier":       1,
                "_delta_note": None,
            }

        except Exception as e:
            logger.debug("OwnershipLoader: yfinance failed for %s: %s", ticker, e)
            return {}

    # ── SQLite cache ──────────────────────────────────────────────────────────

    def _init_db(self):
        """Create normalised snapshots + holders tables if they don't exist."""
        try:
            with sqlite3.connect(self._db_path) as con:
                con.execute("""
                    CREATE TABLE IF NOT EXISTS snapshots (
                        ticker                  TEXT NOT NULL,
                        run_date                TEXT NOT NULL,
                        institutional_pct       REAL,
                        insider_pct             REAL,
                        top10_concentration_pct REAL,
                        source                  TEXT,
                        PRIMARY KEY (ticker, run_date)
                    )
                """)
                con.execute("""
                    CREATE TABLE IF NOT EXISTS holders (
                        ticker    TEXT    NOT NULL,
                        run_date  TEXT    NOT NULL,
                        rank      INTEGER NOT NULL,
                        name      TEXT,
                        pct       REAL,
                        shares    INTEGER,
                        PRIMARY KEY (ticker, run_date, rank)
                    )
                """)
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_snap_ticker_date "
                    "ON snapshots(ticker, run_date)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_holders_ticker_date "
                    "ON holders(ticker, run_date)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_holders_name "
                    "ON holders(name)"
                )
                # Migrate old single-table DB: add source col if absent
                try:
                    con.execute("ALTER TABLE snapshots ADD COLUMN source TEXT")
                except Exception:
                    pass  # column already exists
        except Exception as e:
            logger.warning("OwnershipLoader: could not init DB at %s: %s", self._db_path, e)

    def _store_snapshot(self, ticker: str, run_date: str, data: dict):
        """
        Upsert summary row into snapshots, then replace all holder rows for
        (ticker, run_date). Re-running the same ticker on the same day refreshes.
        """
        try:
            with sqlite3.connect(self._db_path) as con:
                # Upsert summary
                con.execute("""
                    INSERT OR REPLACE INTO snapshots
                        (ticker, run_date, institutional_pct, insider_pct,
                         top10_concentration_pct, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    ticker, run_date,
                    data.get("institutional_pct"),
                    data.get("insider_pct"),
                    data.get("top10_concentration_pct"),
                    data.get("_source"),
                ))

                # Replace holder rows for this (ticker, run_date)
                con.execute(
                    "DELETE FROM holders WHERE ticker = ? AND run_date = ?",
                    (ticker, run_date)
                )
                for rank, h in enumerate(data.get("top_holders", []), start=1):
                    con.execute("""
                        INSERT INTO holders (ticker, run_date, rank, name, pct, shares)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        ticker, run_date, rank,
                        h.get("name"),
                        h.get("pct"),
                        h.get("shares"),
                    ))

            logger.debug("OwnershipLoader: stored snapshot for %s on %s", ticker, run_date)
        except Exception as e:
            logger.warning("OwnershipLoader: could not store snapshot for %s: %s", ticker, e)

    def _attach_deltas(self, ticker: str, today_str: str, data: dict):
        """
        Query cache for recent (qtr/6mo/1yr fallback), 1yr, and 3yr snapshots.
        Attach delta fields to data in-place.
        """
        try:
            today    = datetime.date.fromisoformat(today_str)

            # Recent delta: try qtr → 6mo → 1yr, use first found
            _RECENT_CANDIDATES = [
                (_QTR_TARGET_DAYS, "Qtr"),
                (_6MO_TARGET_DAYS, "6mo"),
                (_1YR_TARGET_DAYS, "1yr"),
            ]
            snap_recent       = None
            recent_label      = None
            recent_used_1yr   = False
            for days, label in _RECENT_CANDIDATES:
                snap = self._nearest_snapshot(ticker, today, days)
                if snap:
                    snap_recent     = snap
                    recent_label    = label
                    recent_used_1yr = (label == "1yr")
                    break

            snap_1yr = None if recent_used_1yr else self._nearest_snapshot(ticker, today, _1YR_TARGET_DAYS)
            snap_3yr = self._nearest_snapshot(ticker, today, _3YR_TARGET_DAYS)

            note_parts = []

            if snap_recent:
                data["institutional_pct_delta_recent"] = _delta_pct(
                    data.get("institutional_pct"), snap_recent["institutional_pct"])
                data["_recent_label"] = recent_label
                _apply_holder_deltas(data["top_holders"], snap_recent["holders"], suffix="recent")
                note_parts.append(f"Δ {recent_label} vs {snap_recent['run_date']}")

            if snap_1yr:
                d1 = _delta_pct(data.get("institutional_pct"), snap_1yr["institutional_pct"])
                data["institutional_pct_delta_1yr"] = d1
                _apply_holder_deltas(data["top_holders"], snap_1yr["holders"], suffix="1yr")
                note_parts.append(f"Δ 1yr vs {snap_1yr['run_date']}")

            if snap_3yr:
                d3 = _delta_pct(data.get("institutional_pct"), snap_3yr["institutional_pct"])
                data["institutional_pct_delta_3yr"] = d3
                _apply_holder_deltas(data["top_holders"], snap_3yr["holders"], suffix="3yr")
                note_parts.append(f"Δ 3yr vs {snap_3yr['run_date']}")

            if note_parts:
                data["_delta_note"]      = "  |  ".join(note_parts)
                data["net_change_shares"] = "See Δ columns in holder table"

        except Exception as e:
            logger.debug("OwnershipLoader: delta attach failed for %s: %s", ticker, e)

    def _nearest_snapshot(self, ticker: str, today: datetime.date,
                           target_days: int) -> dict | None:
        """
        Find the snapshot row closest to (today - target_days), within ±_WINDOW_DAYS.
        Returns dict with run_date, institutional_pct, and holders list — or None.
        Holders are read from the normalised holders table (not JSON).
        """
        try:
            target   = today - datetime.timedelta(days=target_days)
            lo       = (target - datetime.timedelta(days=_WINDOW_DAYS)).isoformat()
            hi       = (target + datetime.timedelta(days=_WINDOW_DAYS)).isoformat()
            # Enforce minimum age: snapshot must be at least half the target
            # period old — prevents a 5-day-old snapshot being used as a
            # "quarterly" comparison when _WINDOW_DAYS is wide enough to match it.
            min_date = (today - datetime.timedelta(days=target_days // 2)).isoformat()

            with sqlite3.connect(self._db_path) as con:
                con.row_factory = sqlite3.Row

                # Find closest snapshot date in window, at least half target age
                snap = con.execute("""
                    SELECT run_date, institutional_pct
                    FROM snapshots
                    WHERE ticker   = ?
                      AND run_date >= ?
                      AND run_date <= ?
                      AND run_date <  ?
                      AND run_date <= ?
                    ORDER BY ABS(julianday(run_date) - julianday(?)) ASC
                    LIMIT 1
                """, (ticker, lo, hi, today.isoformat(),
                      min_date, target.isoformat())).fetchone()

                if snap is None:
                    return None

                # Read individual holders for that date from normalised table
                holder_rows = con.execute("""
                    SELECT name, pct, shares
                    FROM holders
                    WHERE ticker = ? AND run_date = ?
                    ORDER BY rank ASC
                """, (ticker, snap["run_date"])).fetchall()

            holders = [
                {"name": r["name"], "pct": r["pct"], "shares": r["shares"]}
                for r in holder_rows
            ]

            return {
                "run_date":          snap["run_date"],
                "institutional_pct": snap["institutional_pct"],
                "holders":           holders,
            }
        except Exception:
            return None


# ── Delta helpers ─────────────────────────────────────────────────────────────

def _delta_pct(current: float | None, historical: float | None) -> float | None:
    """Return current - historical in decimal form, or None if either missing."""
    if current is None or historical is None:
        return None
    return round(current - historical, 4)


def _apply_holder_deltas(
    current_holders: list,
    historical_holders: list,
    suffix: str,    # "1yr" or "3yr"
):
    """
    Match current holders to historical by name (case-insensitive).
    Attach delta_<suffix>_shares and delta_<suffix>_pct to each current holder.
    None = new position or no historical data for that holder.
    """
    hist_by_name = {
        h["name"].upper(): h
        for h in historical_holders
        if isinstance(h, dict) and h.get("name")
    }
    for h in current_holders:
        hist = hist_by_name.get(h.get("name", "").upper())
        if hist:
            curr_sh  = h.get("shares")
            hist_sh  = hist.get("shares")
            curr_pct = h.get("pct")
            hist_pct = hist.get("pct")
            h[f"delta_{suffix}_shares"] = (
                curr_sh - hist_sh
                if curr_sh is not None and hist_sh is not None else None
            )
            h[f"delta_{suffix}_pct"] = (
                round(curr_pct - hist_pct, 4)
                if curr_pct is not None and hist_pct is not None else None
            )
        else:
            h[f"delta_{suffix}_shares"] = None  # new position
            h[f"delta_{suffix}_pct"]    = None


# ── yfinance parse helpers ────────────────────────────────────────────────────

def _parse_major(major, row_idx: int) -> float | None:
    """
    Parse a row from yfinance major_holders into a decimal fraction.

    yfinance major_holders has changed structure across versions:
      Old (< 0.2.40): DataFrame with positional rows 0–3, value in column 0
      New (>= 0.2.40): DataFrame or Series with named index labels
        'insidersPercentHeld', 'institutionsPercentHeld',
        'institutionsFloatPercentHeld', 'institutionsCount'

    We try named index lookup first, then fall back to positional.
    Row index mapping (positional): 0=insiders, 1=institutions(shares), 2=institutions(float)
    """
    try:
        if major is None or major.empty:
            return None

        # Named index lookup (new yfinance format)
        named_keys = [
            ['insidersPercentHeld', 'percentInsiders'],             # row_idx=0
            ['institutionsPercentHeld', 'percentInstitutions'],     # row_idx=1
            ['institutionsFloatPercentHeld'],                       # row_idx=2
        ]
        if row_idx < len(named_keys):
            for key in named_keys[row_idx]:
                try:
                    # Try as column name
                    if key in major.columns:
                        val = major[key].iloc[0]
                        return _normalise_pct(val)
                    # Try as index label
                    if key in major.index:
                        val = major.loc[key]
                        val = val.iloc[0] if hasattr(val, 'iloc') else val
                        return _normalise_pct(val)
                except Exception:
                    continue

        # Positional fallback (old yfinance format)
        row = major.iloc[row_idx]
        raw = row.iloc[0] if hasattr(row, 'iloc') else row
        return _normalise_pct(raw)

    except Exception:
        return None


def _normalise_pct(raw) -> float | None:
    """Convert a raw yfinance percentage value to a decimal fraction."""
    if raw is None:
        return None
    if isinstance(raw, float) and math.isnan(raw):
        return None
    if isinstance(raw, str):
        return _pct_str_to_decimal(raw)
    if isinstance(raw, (int, float)):
        v = float(raw)
        # yfinance now returns decimals (0.712) not percentages (71.2)
        # but old versions returned percentages — detect which form
        return v / 100.0 if v > 1.0 else v
    return None


def _pct_str_to_decimal(s: str) -> float | None:
    """'71.23%' → 0.7123.  Returns None on any parse failure."""
    try:
        return float(s.strip().replace('%', '').replace(',', '')) / 100.0
    except Exception:
        return None


def _parse_pct_cell(x) -> float | None:
    """
    Normalise a yfinance % Out cell to decimal fraction.
    Handles: float 0.0842, float 8.42 (pct form), string "8.42%", NaN → None.
    """
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    if isinstance(x, str):
        return _pct_str_to_decimal(x)
    if isinstance(x, (int, float)):
        v = float(x)
        return v / 100.0 if v > 1 else v
    return None


def _top10_concentration(inst, shares_outstanding: int | None = None) -> float | None:
    """
    Sum % Out for top-10 holders. Falls back to shares / shares_outstanding per row.
    """
    try:
        if inst is None or inst.empty:
            return None
        col    = _find_col(inst, ['% Out', '% out', 'pctOut', 'percentOut', 'pctHeld', 'Pct Held'])
        sh_col = _find_col(inst, ['Shares', 'shares', 'sharesHeld'])
        total, counted = 0.0, 0
        for _, row in inst.head(10).iterrows():
            pct = _parse_pct_cell(row[col]) if col else None
            if pct is None and shares_outstanding and sh_col and _is_numeric(row[sh_col]):
                pct = float(row[sh_col]) / shares_outstanding
            if pct is not None:
                total   += pct
                counted += 1
        return round(total, 4) if counted > 0 else None
    except Exception:
        return None


def _top10_holders(inst, shares_outstanding: int | None = None) -> list:
    """
    Returns up to 10 dicts: {name, pct, shares}.
    pct derived from % Out when available, else shares / shares_outstanding.
    Delta fields are added later by _apply_holder_deltas.
    """
    try:
        if inst is None or inst.empty:
            return []
        name_col   = _find_col(inst, ['Holder', 'holder', 'Name', 'name'])
        shares_col = _find_col(inst, ['Shares', 'shares', 'sharesHeld'])
        pct_col    = _find_col(inst, ['% Out', '% out', 'pctOut', 'percentOut', 'pctHeld', 'Pct Held'])
        if name_col is None:
            return []
        result = []
        for _, row in inst.head(10).iterrows():
            name   = str(row[name_col]).strip()
            shares = int(row[shares_col]) if shares_col and _is_numeric(row[shares_col]) else None
            pct    = _parse_pct_cell(row[pct_col]) if pct_col else None
            if pct is None and shares and shares_outstanding and shares_outstanding > 0:
                pct = shares / shares_outstanding
            result.append({"name": name, "pct": pct, "shares": shares})
        return result
    except Exception:
        return []


def _find_col(df, candidates: list) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _is_numeric(val) -> bool:
    try:
        float(val)
        return True
    except (TypeError, ValueError):
        return False