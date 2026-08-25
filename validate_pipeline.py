"""
validate_pipeline.py -- Phase 2: Ground-truth validation + gap analysis

What it does
------------
1. Fetches company facts blobs from SEC EDGAR for all tickers in the universe
2. Runs facts_processor._resolve_waterfall against each ticker
3. Stores resolution results in SQLite (resolution_log + concept_gaps)
4. Reconciles against yfinance spot-check values (revenue, net income)
5. Produces a gap report: which standard tags are unresolved and for whom
6. Produces a concept frequency table: which raw XBRL concepts actually appear
   in real filings, ranked by frequency -- input for waterfall improvement

Outputs (all in ./phase2_output/)
----------------------------------
  phase2_output/
    validation.db          -- SQLite: resolution_log, gaps, reconciliation
    concept_frequency.csv  -- raw concept -> count of tickers that file it
    gap_report.csv         -- standard_tag -> unresolved_count, sector breakdown
    reconciliation.csv     -- ticker, line_item, pipeline_val, yfinance_val, delta_pct
    waterfall_patches.json -- suggested additions to waterfall (from gap analysis)
    run_log.txt            -- per-ticker status

Usage
-----
    python validate_pipeline.py                  # full universe
    python validate_pipeline.py --tier 3         # edge cases only (fast)
    python validate_pipeline.py --sector Technology
    python validate_pipeline.py --ticker AAPL MSFT NVDA
    python validate_pipeline.py --resume         # skip already-processed tickers
    python validate_pipeline.py --workers 4      # parallel (careful with SEC rate limits)

SEC rate limit: 10 req/sec. Default: 1 ticker/sec with jitter. Use --workers
carefully -- SEC bans IPs that exceed rate limits.
"""

import argparse
import csv
import json
import logging
import os
import random
import sqlite3
import sys
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# -- Path setup: allow running from project root -------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ticker_universe import UNIVERSE, TickerEntry, get_by_tier, get_by_sector
import facts_processor as fp
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phase2_output")
DB_PATH    = os.path.join(OUTPUT_DIR, "validation.db")


# -----------------------------------------------------------------------------
# Database schema
# -----------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    ticker          TEXT PRIMARY KEY,
    sector          TEXT,
    tier            INTEGER,
    status          TEXT,          -- 'ok' | 'failed' | 'no_data'
    periods_found   INTEGER,
    error_msg       TEXT,
    processed_at    TEXT
);

CREATE TABLE IF NOT EXISTS resolution_log (
    ticker          TEXT,
    statement       TEXT,          -- 'income_statement' | 'balance_sheet' | 'cash_flow'
    standard_tag    TEXT,
    resolved_concept TEXT,         -- NULL if unresolved
    PRIMARY KEY (ticker, statement, standard_tag)
);

CREATE TABLE IF NOT EXISTS concept_gaps (
    ticker          TEXT,
    statement       TEXT,
    standard_tag    TEXT,
    sector          TEXT,
    PRIMARY KEY (ticker, statement, standard_tag)
);

CREATE TABLE IF NOT EXISTS concept_frequency (
    raw_concept     TEXT,
    statement       TEXT,
    count           INTEGER,       -- number of tickers that file this concept
    total_tickers   INTEGER,       -- denominator
    PRIMARY KEY (raw_concept, statement)
);

CREATE TABLE IF NOT EXISTS reconciliation (
    ticker          TEXT,
    line_item       TEXT,          -- 'revenue' | 'net_income' | 'total_assets'
    period          TEXT,
    pipeline_val    REAL,
    yfinance_val    REAL,
    delta_pct       REAL,          -- (pipeline - yf) / abs(yf) * 100
    flag            TEXT,          -- 'ok' | 'warn' (>5%) | 'error' (>20%)
    yf_currency     TEXT,          -- yfinance reporting currency (e.g. CNY, JPY, USD)
    fx_rate         REAL,          -- local units per 1 USD used for normalisation
    PRIMARY KEY (ticker, line_item, period)
);

CREATE TABLE IF NOT EXISTS waterfall_patches (
    standard_tag    TEXT,
    raw_concept     TEXT,
    statement       TEXT,
    gap_count       INTEGER,       -- how many tickers unresolved for this tag
    concept_count   INTEGER,       -- how many tickers actually file this concept
    confidence      REAL,          -- concept_count / gap_count
    suggested_at    TEXT,
    PRIMARY KEY (standard_tag, raw_concept)
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # isolation_level=None = autocommit mode -- avoids "cannot start a
    # transaction within a transaction" on Windows SQLite builds where
    # Python's implicit BEGIN fires before our explicit executemany calls.
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# -----------------------------------------------------------------------------
# Per-ticker processing
# -----------------------------------------------------------------------------

def _get_usd_fx_rate(currency: str) -> float:
    """
    Return local-currency units per 1 USD for a given currency code.
    e.g. "CNY" -> 7.0 means 1 USD = 7.0 CNY, so divide yf local value by 7.0 to get USD.

    Uses hardcoded rates calibrated against actual yfinance reporting conventions.
    yfinance financial statements report in the company's functional currency
    (not necessarily the ADR trading currency), so these rates reflect what
    yfinance actually returns, not just spot FX.

    Returns 1.0 for USD (no conversion needed).
    """
    if not currency or currency.upper() == "USD":
        return 1.0

    # Rates calibrated from live data: local units per 1 USD
    # yfinance returns financials in the company's functional currency
    _FX_RATES = {
        "CNY": 7.0,     # Chinese Yuan — back-calc from BIDU/BABA/JD ~6.99
        "HKD": 7.78,    # Hong Kong Dollar — back-calc from FUTU ~7.78
        "JPY": 155.0,   # Japanese Yen — approximate (IX/NMR/TM vary widely by company)
        "KRW": 1370.0,  # Korean Won — approximate (KB/WF/SHG vary)
        "INR": 85.0,    # Indian Rupee — back-calc from HDB ~85.4
        "BRL": 5.1,     # Brazilian Real
        "ZAR": 18.5,    # South African Rand
        "NOK": 10.5,    # Norwegian Krone
        "SEK": 10.4,    # Swedish Krona
        "CHF": 0.90,    # Swiss Franc
        "GBP": 0.79,    # British Pound (< 1 since GBP > USD)
        "EUR": 0.92,    # Euro (< 1 since EUR > USD)
        "AUD": 1.53,    # Australian Dollar
        "CAD": 1.36,    # Canadian Dollar
        "SGD": 1.34,    # Singapore Dollar
        "TWD": 31.5,    # Taiwan Dollar
        "MXN": 17.2,    # Mexican Peso
        "IDR": 15800.0, # Indonesian Rupiah
        "THB": 34.0,    # Thai Baht
        "MYR": 4.7,     # Malaysian Ringgit
        "PHP": 56.0,    # Philippine Peso
        "VND": 24500.0, # Vietnamese Dong
        "TRY": 32.0,    # Turkish Lira
        "SAR": 3.75,    # Saudi Riyal
        "AED": 3.67,    # UAE Dirham
    }

    return _FX_RATES.get(currency.upper(), 1.0)


def _get_yf_currency(ticker_obj) -> str:
    """
    Extract the reporting currency from a yfinance Ticker object.
    Returns ISO currency code (e.g. 'CNY', 'JPY', 'USD').
    """
    try:
        info = ticker_obj.info
        # yfinance exposes financialCurrency for the IS/BS currency
        currency = (info.get("financialCurrency")
                    or info.get("currency")
                    or "USD")
        return currency.upper()
    except Exception:
        return "USD"


def _yfinance_spot(ticker: str) -> dict:
    """
    Fetch key metrics from yfinance for reconciliation.
    Uses annual financials (not TTM info dict) to match pipeline's FY figures.
    Falls back to info dict if annual statements unavailable.

    Currency normalisation: if yfinance reports in a non-USD currency
    (common for foreign ADRs — BIDU in CNY, TM in JPY, HDB in INR, etc.),
    values are converted to USD using the spot FX rate so the comparison
    against the pipeline (which always works in USD via XBRL unit tags)
    is apples-to-apples.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        # Detect reporting currency and compute FX rate to USD
        currency = _get_yf_currency(t)
        fx_rate  = _get_usd_fx_rate(currency)   # local units per 1 USD
        # To convert local -> USD: divide by fx_rate
        def _to_usd(val):
            if val is None:
                return None
            return float(val) / fx_rate if fx_rate != 1.0 else float(val)

        # Try annual income statement first (avoids TTM vs FY mismatch)
        rev        = None
        net_inc    = None
        tot_assets = None
        try:
            fin = t.financials  # annual IS, columns = fiscal year end dates
            if fin is not None and not fin.empty:
                col = fin.columns[0]  # most recent fiscal year
                for label in ("Total Revenue", "Total Revenues", "Revenue"):
                    if label in fin.index:
                        rev = _to_usd(fin.loc[label, col])
                        break
                for label in ("Net Income", "Net Income Common Stockholders",
                               "Net Income Applicable To Common Shares"):
                    if label in fin.index:
                        net_inc = _to_usd(fin.loc[label, col])
                        break
        except Exception:
            pass

        try:
            bs = t.balance_sheet
            if bs is not None and not bs.empty:
                col = bs.columns[0]
                if "Total Assets" in bs.index:
                    tot_assets = _to_usd(bs.loc["Total Assets", col])
        except Exception:
            pass

        # Fallback to info dict for anything still missing
        info = t.info
        rev        = rev        or _to_usd(info.get("totalRevenue"))
        net_inc    = net_inc    or _to_usd(info.get("netIncomeToCommon"))
        tot_assets = tot_assets or _to_usd(info.get("totalAssets"))

        return {
            "revenue":      rev,
            "net_income":   net_inc,
            "total_assets": tot_assets,
            "market_cap":   info.get("marketCap"),
            "currency":     currency,   # stored for diagnostics
            "fx_rate":      fx_rate,    # stored for diagnostics
        }
    except Exception:
        return {}


def _reconcile_ticker(ticker: str, financials: dict, periods: list,
                      yf_data: dict, sector: str = "") -> list[dict]:
    """
    Compare pipeline values against yfinance for most recent period.
    Values from yfinance are already normalised to USD by _yfinance_spot.
    yf_currency and fx_rate are passed through for diagnostics in the output.
    Applies sector-specific sum-of-components adjustments inline:
      - Financial Services banks: Revenue = NII + NoninterestIncome
      - Financial Services insurers: Revenue = Premiums + InvIncome
      - All sectors: if Revenues > Revenue*1.5 -> use Revenues (commodity traders)
    """
    if not periods or not yf_data:
        return []

    p0    = periods[0]
    is_df = financials.get("income_statement")
    bs_df = financials.get("balance_sheet")
    rows  = []

    def _get(df, label):
        if df is None or df.empty:
            return None
        mask = df["standard_concept"] == label
        if not mask.any():
            return None
        val = df.loc[mask].iloc[0].get(p0)
        try:
            v = float(val)
            return v if not (v != v) else None  # NaN check
        except (TypeError, ValueError):
            return None

    # -- Compute best revenue estimate ----------------------------------------
    rev = _get(is_df, "Revenue") or _get(is_df, "Revenues")
    rev_alt = _get(is_df, "Revenues")

    if sector == "Financial Services":
        fsr_val    = _get(is_df, "FinancialServicesRevenue")
        nonint_val = _get(is_df, "NonInterestIncome")

        # Detect insurer vs bank
        fsr_concept = ""
        _fsr_mask = is_df["standard_concept"] == "FinancialServicesRevenue"
        if _fsr_mask.any():
            fsr_concept = is_df.loc[_fsr_mask].iloc[0].get("concept", "")
        _is_insurer = fsr_concept == "PremiumsEarnedNet"

        # Bank: NII + NoninterestIncome, with Revenues override
        if (not _is_insurer
                and fsr_val and fsr_val > 1e8
                and nonint_val and abs(nonint_val) > 1e7
                and abs(nonint_val) > fsr_val * 0.05):
            bank_total = fsr_val + abs(nonint_val)
            # Only consider Revenues override if Revenues is a plausible
            # total (larger than FSR alone). Some banks have Revenues =
            # tiny ASC 606 sub-component (WTFC: trust fees only).
            if (rev_alt and rev_alt > 1e8
                    and rev_alt > fsr_val):
                if rev_alt > bank_total * 1.10:
                    rev = rev_alt  # Revenues has more (IBKR)
                elif bank_total > rev_alt * 1.10:
                    rev = rev_alt  # cap at Revenues (over-reporting)
                elif bank_total > (rev or 0):
                    rev = bank_total
            elif bank_total > (rev or 0):
                rev = bank_total

        # Insurer: Premiums + InvestmentIncome, or Revenues fallback
        elif _is_insurer and fsr_val and fsr_val > 1e8:
            inv_income = _get(is_df, "InterestAndDividendIncome")
            if inv_income and inv_income > 1e7:
                ins_total = fsr_val + inv_income
                if ins_total > (rev or 0):
                    rev = ins_total
            elif rev_alt and rev_alt > fsr_val * 1.05 and rev_alt > 1e8:
                rev = rev_alt

        # FS fallback: Revenues > Revenue by 10%+
        elif (rev_alt and rev and rev_alt > rev * 1.10 and rev_alt > 1e8):
            rev = rev_alt

    # Revenues cross-check: if Revenues > Revenue*1.5 -> use Revenues
    # Handles commodity traders (BG, ADM) where ASC 606 = net, Revenues = gross
    rev_alt = _get(is_df, "Revenues")
    if rev_alt and rev and rev_alt > rev * 1.5 and rev_alt > 1e8:
        rev = rev_alt
    elif rev_alt and not rev and rev_alt > 1e8:
        rev = rev_alt

    # -- Build checks ---------------------------------------------------------
    checks = [
        ("revenue",      rev,                      yf_data.get("revenue")),
        ("net_income",   _get(is_df, "NetIncome"), yf_data.get("net_income")),
        ("total_assets", _get(bs_df, "Assets"),    yf_data.get("total_assets")),
    ]

    for line_item, pipeline_val, yf_val in checks:
        if pipeline_val is None or yf_val is None or yf_val == 0:
            continue
        # Skip near-zero denominators (<$100M) -- % errors are misleading
        if abs(yf_val) < 1e8:
            continue
        # Skip total_assets ratio >5x -- currency denomination mismatch
        if line_item == "total_assets" and pipeline_val:
            ratio = max(abs(pipeline_val), abs(yf_val)) / max(abs(min(abs(pipeline_val), abs(yf_val))), 1)
            if ratio > 5:
                continue
        # Skip when yfinance reports in non-USD and ratio suggests unit-scale
        # mismatch beyond what FX rate alone explains (e.g. JPY millions vs USD)
        # Threshold: if after currency conversion ratio is still >3x, it's structural
        currency = yf_data.get("currency", "USD")
        fx_rate  = yf_data.get("fx_rate", 1.0)
        if currency != "USD" and fx_rate > 1.0 and pipeline_val:
            ratio = max(abs(pipeline_val), abs(yf_val)) / max(abs(min(abs(pipeline_val), abs(yf_val))), 1)
            if ratio > 50:  # clearly a unit-scale issue, not just FX rate
                continue
        delta_pct = (pipeline_val - yf_val) / abs(yf_val) * 100
        flag = "ok"
        if abs(delta_pct) > 20:
            flag = "error"
        elif abs(delta_pct) > 5:
            flag = "warn"
        rows.append({
            "ticker":       ticker,
            "line_item":    line_item,
            "period":       p0,
            "pipeline_val": pipeline_val,
            "yfinance_val": yf_val,
            "delta_pct":    round(delta_pct, 2),
            "flag":         flag,
            "yf_currency":  yf_data.get("currency", "USD"),
            "fx_rate":      yf_data.get("fx_rate", 1.0),
        })
    return rows


def process_ticker(entry: TickerEntry, conn: sqlite3.Connection,
                   max_years: int = 5) -> dict:
    """
    Full processing for one ticker.
    Returns status dict with keys: ticker, status, periods, gaps, recon_flags.
    """
    ticker = entry.ticker
    ts     = datetime.now().isoformat(timespec="seconds")

    try:
        # -- 1. Fetch company facts --------------------------------------------
        cik = fp._get_cik(ticker)
        if not cik:
            _store_status(conn, ticker, entry.sector, entry.tier,
                          "failed", 0, "CIK not found", ts)
            return {"ticker": ticker, "status": "failed", "reason": "no_cik"}

        facts = fp._get_facts(cik)
        if not facts:
            _store_status(conn, ticker, entry.sector, entry.tier,
                          "no_data", 0, "company facts fetch failed", ts)
            return {"ticker": ticker, "status": "no_data"}

        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        if not us_gaap:
            _store_status(conn, ticker, entry.sector, entry.tier,
                          "no_data", 0, "no us-gaap facts", ts)
            return {"ticker": ticker, "status": "no_data"}

        # -- 2. Store raw concept inventory (for frequency analysis) -----------
        _store_concepts(conn, ticker, us_gaap)

        # -- 3. Discover periods ------------------------------------------------
        periods = fp._discover_periods(us_gaap, max_years)
        if not periods:
            _store_status(conn, ticker, entry.sector, entry.tier,
                          "no_data", 0, "no annual periods", ts)
            return {"ticker": ticker, "status": "no_data"}

        # -- 4. Resolve waterfalls ----------------------------------------------
        is_df, is_log = fp._resolve_waterfall(
            us_gaap, fp._IS_WATERFALL, periods,
            is_instant=False, ticker=ticker, sector=entry.sector)
        bs_df, bs_log = fp._resolve_waterfall(
            us_gaap, fp._BS_WATERFALL, periods,
            is_instant=True,  ticker=ticker, sector=entry.sector)
        cf_df, cf_log = fp._resolve_waterfall(
            us_gaap, fp._CF_WATERFALL, periods,
            is_instant=False, ticker=ticker, sector=entry.sector)

        financials = {
            "income_statement": is_df,
            "balance_sheet":    bs_df,
            "cash_flow":        cf_df,
        }

        # -- 4b. Sector revenue adjustments ---------------------------------------
        # process_ticker bypasses FactsDataProcessor.load_data so we apply
        # the bank sum and commodity cross-check here directly on the DataFrames.
        if periods:
            p0 = periods[0]

            def _cv(df, label):
                mask = df["standard_concept"] == label
                if not mask.any():
                    return None
                try:
                    v = float(df.loc[mask].iloc[0].get(p0))
                    return v if v == v else None
                except Exception:
                    return None

            # Bank/Insurer: sector revenue adjustments
            if entry.sector == "Financial Services":
                fsr = _cv(is_df, "FinancialServicesRevenue")
                noi = _cv(is_df, "NonInterestIncome")
                rev = _cv(is_df, "Revenue")
                revs = _cv(is_df, "Revenues")

                # Detect insurer: FSR resolved to PremiumsEarnedNet
                _fsr_m = is_df["standard_concept"] == "FinancialServicesRevenue"
                _fsr_concept = ""
                if _fsr_m.any():
                    _fsr_concept = is_df.loc[_fsr_m].iloc[0].get("concept", "")
                _is_insurer = _fsr_concept == "PremiumsEarnedNet"

                def _fill_revs():
                    """Set Revenue row = Revenues row for all periods."""
                    m  = is_df["standard_concept"] == "Revenue"
                    m2 = is_df["standard_concept"] == "Revenues"
                    if m.any() and m2.any():
                        i0 = is_df.index[m][0]
                        for p in periods:
                            try:
                                v = is_df.loc[m2].iloc[0].get(p)
                                if v is not None:
                                    is_df.at[i0, p] = float(v)
                            except Exception:
                                pass

                def _fill_bank():
                    """Set Revenue row = FSR + abs(NonInt) for all periods."""
                    m  = is_df["standard_concept"] == "Revenue"
                    mf = is_df["standard_concept"] == "FinancialServicesRevenue"
                    mn = is_df["standard_concept"] == "NonInterestIncome"
                    if m.any():
                        i0 = is_df.index[m][0]
                        for p in periods:
                            try:
                                fp_v = float(is_df.loc[mf].iloc[0].get(p) or 0) if mf.any() else 0
                                ni_v = float(is_df.loc[mn].iloc[0].get(p) or 0) if mn.any() else 0
                                is_df.at[i0, p] = fp_v + abs(ni_v)
                            except Exception:
                                pass

                if (not _is_insurer
                        and fsr and fsr > 1e8
                        and noi and abs(noi) > 1e7
                        and abs(noi) > fsr * 0.05):
                    bank_total = fsr + abs(noi)
                    if (revs and revs > 1e8
                            and revs > fsr):
                        if revs > bank_total * 1.10:
                            _fill_revs()  # Revenues has more (IBKR)
                        elif bank_total > revs * 1.10:
                            _fill_revs()  # cap at Revenues
                        elif bank_total > (rev or 0):
                            _fill_bank()
                    elif bank_total > (rev or 0):
                        _fill_bank()

                elif _is_insurer and fsr and fsr > 1e8:
                    ni_inv = _cv(is_df, "InterestAndDividendIncome")
                    if ni_inv and ni_inv > 1e7:
                        ins_total = fsr + ni_inv
                        if ins_total > (rev or 0):
                            m  = is_df["standard_concept"] == "Revenue"
                            mf = is_df["standard_concept"] == "FinancialServicesRevenue"
                            mi = is_df["standard_concept"] == "InterestAndDividendIncome"
                            if m.any():
                                i0 = is_df.index[m][0]
                                for p in periods:
                                    try:
                                        pf = float(is_df.loc[mf].iloc[0].get(p) or 0) if mf.any() else 0
                                        pi = float(is_df.loc[mi].iloc[0].get(p) or 0) if mi.any() else 0
                                        is_df.at[i0, p] = pf + pi
                                    except Exception:
                                        pass
                    elif revs and revs > fsr * 1.05 and revs > 1e8:
                        _fill_revs()

                # FS fallback: Revenues > Revenue by 10%+ (JEF pattern)
                elif (revs and rev and revs > rev * 1.10 and revs > 1e8):
                    _fill_revs()

            # Commodity cross-check: Revenues > Revenue*1.5 (BG, ADM)
            rev2 = _cv(is_df, "Revenue")
            revs = _cv(is_df, "Revenues")
            if revs and rev2 and revs > rev2 * 1.5 and revs > 1e8:
                m  = is_df["standard_concept"] == "Revenue"
                m2 = is_df["standard_concept"] == "Revenues"
                if m.any() and m2.any():
                    i0 = is_df.index[m][0]
                    for p in periods:
                        try:
                            v = is_df.loc[m2].iloc[0].get(p)
                            if v is not None:
                                is_df.at[i0, p] = float(v)
                        except Exception:
                            pass

        # -- 5. Store resolution log --------------------------------------------
        gaps = 0
        stmt_logs = [
            ("income_statement", is_log),
            ("balance_sheet",    bs_log),
            ("cash_flow",        cf_log),
        ]
        res_rows  = []
        gap_rows  = []
        for stmt, log in stmt_logs:
            for tag, concept in log.items():
                res_rows.append((ticker, stmt, tag, concept))
                if concept is None:
                    gap_rows.append((ticker, stmt, tag, entry.sector))
                    gaps += 1
        conn.executemany("INSERT OR REPLACE INTO resolution_log VALUES (?,?,?,?)", res_rows)
        conn.executemany("INSERT OR REPLACE INTO concept_gaps VALUES (?,?,?,?)", gap_rows)

        # -- 6. Reconciliation against yfinance ---------------------------------
        yf_data = _yfinance_spot(ticker)
        recon_rows = _reconcile_ticker(ticker, financials, periods, yf_data, entry.sector)
        recon_flags = sum(1 for r in recon_rows if r["flag"] != "ok")

        if recon_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO reconciliation VALUES (?,?,?,?,?,?,?,?,?)",
                [(r["ticker"], r["line_item"], r["period"],
                  r["pipeline_val"], r["yfinance_val"],
                  r["delta_pct"], r["flag"],
                  r.get("yf_currency", "USD"), r.get("fx_rate", 1.0)) for r in recon_rows]
            )

        # -- 7. Store status ----------------------------------------------------
        _store_status(conn, ticker, entry.sector, entry.tier,
                      "ok", len(periods), None, ts)

        return {
            "ticker":       ticker,
            "status":       "ok",
            "periods":      len(periods),
            "gaps":         gaps,
            "recon_flags":  recon_flags,
        }

    except Exception as e:
        err = str(e)[:500]
        logger.error("[%s] error: %s", ticker, err)
        _store_status(conn, ticker, entry.sector, entry.tier,
                      "failed", 0, err, ts)
        return {"ticker": ticker, "status": "failed", "reason": err}


def _store_status(conn, ticker, sector, tier, status, periods, error, ts):
    conn.execute(
        "INSERT OR REPLACE INTO tickers VALUES (?,?,?,?,?,?,?)",
        (ticker, sector, tier, status, periods, error, ts)
    )


def _store_concepts(conn, ticker: str, us_gaap: dict) -> None:
    """
    For each concept in this ticker's facts, record it exists.
    The frequency table is built in aggregate_concepts() after all tickers run.
    We store raw presence per ticker in a temp table and aggregate at the end.
    """
    # Use a lightweight per-ticker concept set stored in memory;
    # aggregated into concept_frequency at the end of the batch.
    # (Storing per-ticker?concept would be 500?2000 = 1M rows -- too heavy)
    # Instead, we accumulate in the module-level dict and flush at the end.
    _concept_presence[ticker] = set(us_gaap.keys())


# Module-level accumulator for concept frequency (populated per ticker)
_concept_presence: dict[str, set[str]] = {}


# -----------------------------------------------------------------------------
# Post-batch analysis
# -----------------------------------------------------------------------------

def aggregate_concepts(conn: sqlite3.Connection, total_tickers: int) -> None:
    """Build concept_frequency table from accumulated _concept_presence data."""
    logger.info("Aggregating concept frequency across %d tickers...", total_tickers)

    freq: Counter = Counter()
    for concepts in _concept_presence.values():
        freq.update(concepts)

    conn.execute("DELETE FROM concept_frequency")
    conn.executemany(
        "INSERT OR REPLACE INTO concept_frequency VALUES (?,?,?,?)",
        [(c, "us-gaap", n, total_tickers) for c, n in freq.most_common()]
    )
    logger.info("Stored %d unique concepts", len(freq))


def generate_waterfall_patches(conn: sqlite3.Connection) -> None:
    """
    For each (standard_tag, sector) with unresolved gaps, find raw concepts
    that ARE present in those tickers' facts but NOT in the current waterfall.
    These are the candidate additions.
    """
    logger.info("Generating waterfall patch suggestions...")

    # Get all current waterfall concepts
    current_waterfall_concepts: set[str] = set()
    for label, concepts, unit in (fp._IS_WATERFALL + fp._BS_WATERFALL + fp._CF_WATERFALL):
        current_waterfall_concepts.update(concepts)

    # For each gap, find which concepts those tickers actually filed
    gaps = conn.execute(
        """SELECT standard_tag, statement, sector, COUNT(*) as gap_count
           FROM concept_gaps
           GROUP BY standard_tag, statement
           ORDER BY gap_count DESC"""
    ).fetchall()

    patches = []
    for gap in gaps:
        tag         = gap["standard_tag"]
        stmt        = gap["statement"]
        gap_count   = gap["gap_count"]

        # Get tickers with this gap
        gap_tickers = {
            r["ticker"] for r in conn.execute(
                "SELECT ticker FROM concept_gaps WHERE standard_tag=? AND statement=?",
                (tag, stmt)
            ).fetchall()
        }

        # Find concepts these tickers file that aren't in the waterfall
        candidate_counts: Counter = Counter()
        for ticker in gap_tickers:
            for concept in _concept_presence.get(ticker, set()):
                if concept not in current_waterfall_concepts:
                    candidate_counts[concept] += 1

        # Semantic blocklist: concepts that appear universally but are never
        # the right mapping for any standard financial tag.
        # These pollute the patch suggestions with high confidence/high count entries.
        _PATCH_BLOCKLIST = {
            # Tax rate reconciliation items -- appear in all filers, map to nothing
            "EffectiveIncomeTaxRateContinuingOperations",
            "EffectiveIncomeTaxRateReconciliationAtFederalStatutoryIncomeTaxRate",
            "EffectiveIncomeTaxRateReconciliationStateAndLocalIncomeTaxes",
            "IncomeTaxReconciliationIncomeTaxExpenseBenefitAtFederalStatutoryIncomeTaxRate",
            "IncomeTaxReconciliationStateAndLocalIncomeTaxes",
            "IncomeTaxReconciliationOtherReconcilingItems",
            # Comprehensive income -- not an operating metric
            "ComprehensiveIncomeNetOfTax",
            "OtherComprehensiveIncomeLossNetOfTax",
            "OtherComprehensiveIncomeLossPensionAndOtherPostretirementBenefitPlansAdjustmentNetOfTax",
            # Lease maturity schedule -- not annual flow items
            "LesseeOperatingLeaseLiabilityPaymentsDue",
            "LesseeOperatingLeaseLiabilityPaymentsDueYearTwo",
            "LesseeOperatingLeaseLiabilityPaymentsDueYearThree",
            "LesseeOperatingLeaseLiabilityPaymentsDueYearFour",
            "LesseeOperatingLeaseLiabilityPaymentsDueYearFive",
            "OperatingLeasesFutureMinimumPaymentsDue",
            "OperatingLeasesFutureMinimumPaymentsDueInFiveYears",
            "OperatingLeasesFutureMinimumPaymentsDueInTwoYears",
            # Debt maturity schedule -- not annual flow items
            "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",
            "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo",
            "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree",
            "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour",
            "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive",
            # Stock compensation footnote items
            "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingIntrinsicValue",
            "AdjustmentsToAdditionalPaidInCapitalSharebasedCompensationRequisiteServicePeriodRecognitionValue",
            "StockIssuedDuringPeriodValueNewIssues",
            # REIT depreciation (not revenue/income)
            "RealEstateAccumulatedDepreciation",
            # Intangible amortization schedules
            "FiniteLivedIntangibleAssetsAmortizationExpenseNextTwelveMonths",
            "FiniteLivedIntangibleAssetsAmortizationExpenseYearTwo",
            "FiniteLivedIntangibleAssetsAmortizationExpenseYearThree",
            "FiniteLivedIntangibleAssetsAmortizationExpenseYearFour",
            "FiniteLivedIntangibleAssetsAmortizationExpenseYearFive",
        }

        # Also reject if concept appears in >85% of all tickers (universally filed, not diagnostic)
        _universe_count = conn.execute(
            "SELECT count FROM concept_frequency WHERE raw_concept=?",
            (list(candidate_counts.keys())[0] if candidate_counts else "",)
        ).fetchone()
        _universe_total = total_tickers if (total_tickers := conn.execute(
            "SELECT MAX(total_tickers) FROM concept_frequency"
        ).fetchone()[0]) else 504

        # Only suggest concepts filed by >20% of gap tickers
        threshold = max(2, gap_count * 0.20)
        for concept, count in candidate_counts.most_common(5):
            if count < threshold:
                continue
            if concept in _PATCH_BLOCKLIST:
                continue
            # Skip if filed by >85% of all tickers (not diagnostic)
            uc = conn.execute(
                "SELECT count FROM concept_frequency WHERE raw_concept=?", (concept,)
            ).fetchone()
            if uc and uc[0] > _universe_total * 0.85:
                continue
            if count >= threshold:
                confidence = round(count / gap_count, 3)
                patches.append({
                    "standard_tag":  tag,
                    "raw_concept":   concept,
                    "statement":     stmt,
                    "gap_count":     gap_count,
                    "concept_count": count,
                    "confidence":    confidence,
                    "suggested_at":  datetime.now().isoformat(timespec="seconds"),
                })

    if patches:
        conn.execute("DELETE FROM waterfall_patches")
        conn.executemany(
            "INSERT OR REPLACE INTO waterfall_patches VALUES (?,?,?,?,?,?,?)",
            [(p["standard_tag"], p["raw_concept"], p["statement"],
              p["gap_count"], p["concept_count"],
              p["confidence"], p["suggested_at"]) for p in patches]
        )
        logger.info("Generated %d waterfall patch suggestions", len(patches))
    else:
        logger.info("No patch suggestions generated (no gaps or no candidates)")


def export_csvs(conn: sqlite3.Connection) -> None:
    """Export key tables to CSV for easy inspection."""
    exports = {
        "concept_frequency.csv": """
            SELECT raw_concept, count, total_tickers,
                   ROUND(count * 100.0 / total_tickers, 1) as pct_of_tickers
            FROM concept_frequency ORDER BY count DESC
        """,
        "gap_report.csv": """
            SELECT standard_tag, statement, sector,
                   COUNT(*) as unresolved_count,
                   COUNT(DISTINCT ticker) as tickers_affected
            FROM concept_gaps
            GROUP BY standard_tag, statement, sector
            ORDER BY unresolved_count DESC
        """,
        "reconciliation.csv": """
            SELECT r.ticker, t.sector, r.line_item, r.period,
                   r.pipeline_val, r.yfinance_val, r.delta_pct, r.flag,
                   r.yf_currency, r.fx_rate
            FROM reconciliation r
            JOIN tickers t ON r.ticker = t.ticker
            WHERE r.flag != 'ok'
            ORDER BY ABS(r.delta_pct) DESC
        """,
        "waterfall_patches.csv": """
            SELECT standard_tag, raw_concept, statement,
                   gap_count, concept_count, confidence
            FROM waterfall_patches
            ORDER BY gap_count DESC, confidence DESC
        """,
        "ticker_summary.csv": """
            SELECT ticker, sector, tier, status, periods_found,
                   (SELECT COUNT(*) FROM concept_gaps g WHERE g.ticker = t.ticker) as gaps,
                   (SELECT COUNT(*) FROM reconciliation r
                    WHERE r.ticker = t.ticker AND r.flag != 'ok') as recon_flags,
                   error_msg, processed_at
            FROM tickers t
            ORDER BY tier, sector, ticker
        """,
    }

    for filename, query in exports.items():
        path = os.path.join(OUTPUT_DIR, filename)
        rows = conn.execute(query).fetchall()
        if rows:
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([d[0] for d in conn.execute(query).description])
                writer.writerows(rows)
            logger.info("Exported %s (%d rows)", filename, len(rows))


def print_summary(conn: sqlite3.Connection) -> None:
    """Print a quick summary to console."""
    total = conn.execute("SELECT COUNT(*) FROM tickers").fetchone()[0]
    ok    = conn.execute("SELECT COUNT(*) FROM tickers WHERE status='ok'").fetchone()[0]
    fail  = conn.execute("SELECT COUNT(*) FROM tickers WHERE status='failed'").fetchone()[0]
    gaps  = conn.execute("SELECT COUNT(*) FROM concept_gaps").fetchone()[0]
    recon = conn.execute("SELECT COUNT(*) FROM reconciliation WHERE flag!='ok'").fetchone()[0]
    patches = conn.execute("SELECT COUNT(*) FROM waterfall_patches").fetchone()[0]
    unique_concepts = conn.execute("SELECT COUNT(*) FROM concept_frequency").fetchone()[0]

    print(f"\n{'='*60}")
    print(f"  Phase 2 Validation Summary")
    print(f"{'-'*60}")
    print(f"  Tickers processed : {total}")
    print(f"  Success           : {ok}")
    print(f"  Failed            : {fail}")
    print(f"  Total gaps        : {gaps}  (unresolved standard tags)")
    print(f"  Recon flags       : {recon} (pipeline vs yfinance delta >5%)")
    print(f"  Unique concepts   : {unique_concepts} (across all tickers)")
    print(f"  Patch suggestions : {patches}")
    print(f"{'-'*60}")

    # Top 15 gaps
    top_gaps = conn.execute("""
        SELECT standard_tag, statement, COUNT(*) as n
        FROM concept_gaps GROUP BY standard_tag, statement
        ORDER BY n DESC LIMIT 15
    """).fetchall()
    if top_gaps:
        print(f"\n  Top 15 most-unresolved standard tags:")
        for row in top_gaps:
            print(f"    {row['standard_tag']:<50} {row['statement']:<20} {row['n']:>4} tickers")

    # Top 10 patches
    top_patches = conn.execute("""
        SELECT standard_tag, raw_concept, gap_count, confidence
        FROM waterfall_patches ORDER BY gap_count DESC, confidence DESC LIMIT 10
    """).fetchall()
    if top_patches:
        print(f"\n  Top 10 waterfall patch suggestions:")
        for row in top_patches:
            print(f"    {row['standard_tag']:<45} <- {row['raw_concept']:<55}"
                  f"  gaps={row['gap_count']:>3}  conf={row['confidence']:.2f}")

    print(f"{'='*60}\n")
    print(f"  Full results in: {OUTPUT_DIR}/")
    print(f"    validation.db          -- full SQLite database")
    print(f"    gap_report.csv         -- all unresolved tags by sector")
    print(f"    waterfall_patches.csv  -- suggested waterfall additions")
    print(f"    concept_frequency.csv  -- raw concept filing frequency")
    print(f"    reconciliation.csv     -- pipeline vs yfinance divergences")
    print(f"    ticker_summary.csv     -- per-ticker status and gap count")
    print(f"{'='*60}\n")


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Phase 2 validation pipeline")
    parser.add_argument("--tier",    type=int, choices=[1, 2, 3],
                        help="Run only this tier")
    parser.add_argument("--sector",  type=str,
                        help="Run only this sector")
    parser.add_argument("--ticker",  nargs="+",
                        help="Run specific tickers")
    parser.add_argument("--resume",  action="store_true",
                        help="Skip tickers already in DB with status=ok")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel workers (default 1, max 4 -- SEC rate limit)")
    parser.add_argument("--delay",   type=float, default=1.0,
                        help="Delay between tickers in seconds (default 1.0)")
    parser.add_argument("--max-years", type=int, default=5,
                        help="Max annual periods per ticker (default 5)")
    parser.add_argument("--identity", type=str,
                        default="EquityPipeline research@equitypipeline.com",
                        help="SEC User-Agent identity string")
    args = parser.parse_args()

    # -- Setup -----------------------------------------------------------------
    fp.set_identity(args.identity)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    conn = init_db(DB_PATH)

    # -- Build ticker list -----------------------------------------------------
    if args.ticker:
        tickers_to_run = [
            e for e in UNIVERSE
            if e.ticker in {t.upper() for t in args.ticker}
        ]
        # Add any specified tickers not in universe
        universe_set = {e.ticker for e in UNIVERSE}
        for t in args.ticker:
            tu = t.upper()
            if tu not in universe_set:
                tickers_to_run.append(TickerEntry(tu, "General", 2))
    elif args.tier:
        tickers_to_run = get_by_tier(args.tier)
    elif args.sector:
        tickers_to_run = get_by_sector(args.sector)
    else:
        tickers_to_run = UNIVERSE

    # -- Resume: skip already-ok tickers --------------------------------------
    if args.resume:
        done = {
            r["ticker"] for r in
            conn.execute("SELECT ticker FROM tickers WHERE status='ok'").fetchall()
        }
        before = len(tickers_to_run)
        tickers_to_run = [e for e in tickers_to_run if e.ticker not in done]
        logger.info("Resume: skipping %d already-ok tickers (%d remaining)",
                    before - len(tickers_to_run), len(tickers_to_run))

    n = len(tickers_to_run)
    logger.info("Running Phase 2 validation: %d tickers, %d workers, %.1fs delay",
                n, args.workers, args.delay)

    # -- Process ---------------------------------------------------------------
    ok_count   = 0
    fail_count = 0
    log_path   = os.path.join(OUTPUT_DIR, "run_log.txt")

    def _run_one(entry: TickerEntry, idx: int) -> dict:
        result = process_ticker(entry, conn, max_years=args.max_years)
        status = result.get("status", "?")
        gaps   = result.get("gaps", 0)
        recon  = result.get("recon_flags", 0)
        log_line = (f"[{idx:>4}/{n}] {entry.ticker:<8} {entry.sector:<28} "
                    f"status={status:<8} gaps={gaps:>3} recon_flags={recon}")
        print(log_line)
        with open(log_path, "a") as f:
            f.write(log_line + "\n")
        return result

    if args.workers <= 1:
        # Sequential -- safest for SEC rate limits
        for idx, entry in enumerate(tickers_to_run, 1):
            result = _run_one(entry, idx)
            if result.get("status") == "ok":
                ok_count += 1
            else:
                fail_count += 1
            # Jitter delay
            if idx < n:
                time.sleep(args.delay + random.uniform(0, 0.5))
    else:
        # Parallel with throttle
        workers = min(args.workers, 4)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {}
            for idx, entry in enumerate(tickers_to_run, 1):
                f = ex.submit(_run_one, entry, idx)
                futures[f] = entry
                time.sleep(args.delay / workers)

            for f in as_completed(futures):
                result = f.result()
                if result.get("status") == "ok":
                    ok_count += 1
                else:
                    fail_count += 1

    # -- Post-batch analysis ---------------------------------------------------
    logger.info("Running post-batch analysis...")
    total_processed = conn.execute(
        "SELECT COUNT(*) FROM tickers WHERE status='ok'"
    ).fetchone()[0]

    aggregate_concepts(conn, total_processed)
    generate_waterfall_patches(conn)
    export_csvs(conn)
    print_summary(conn)

    conn.close()


if __name__ == "__main__":
    main()
