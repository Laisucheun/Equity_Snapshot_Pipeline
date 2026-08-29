"""
validate_gross_margin.py -- Validate pipeline gross margin against yfinance consensus

For each ticker, runs the pipeline in data-only mode (no PDF) to get the
most recent fiscal year's gross margin, compares it against yfinance's
trailing-twelve-month grossMargins figure, and classifies the delta.

Reuses existing pipeline code rather than duplicating it:
    - core.orchestrator.EquityAnalystOrchestrator.run_data_only()
    - core.agents._parse_pct() to parse the "46.91%"-style string the
      pipeline returns back into a decimal comparable to yfinance's figure

Usage
-----
    python scripts/validate_gross_margin.py                      # sp500 (ratings.csv), default
    python scripts/validate_gross_margin.py --universe sp500
    python scripts/validate_gross_margin.py --universe mag7
    python scripts/validate_gross_margin.py --ticker AAPL
    python scripts/validate_gross_margin.py --delay 3
    python scripts/validate_gross_margin.py --skip-financials

Output
------
Live progress table on stdout, a summary (counts by match bucket, mean
absolute error, list of failures), and a full CSV report at
scripts/gross_margin_validation_report.csv.
"""

import os
import sys
import csv
import time
import argparse

_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _PROJECT_ROOT)

import yfinance as yf

from core.orchestrator import EquityAnalystOrchestrator
from core.agents import _parse_pct
from runners.main import TICKERS as MAG7_TICKERS, EDGAR_IDENTITY

_DEFAULT_RATINGS_CSV = os.path.join(_PROJECT_ROOT, "ratings.csv")
_REPORT_PATH         = os.path.join(_SCRIPT_DIR, "gross_margin_validation_report.csv")

_MATCH_THRESHOLD  = 4   # pp -- within this = OK
_WARN_THRESHOLD   = 8   # pp -- within this (and beyond _MATCH_THRESHOLD) = WARN


# ── Universe loading ───────────────────────────────────────────────────────────

def _load_tickers_from_csv(path: str) -> list[str]:
    """Read a 'ticker' column from a CSV file, dedup'd, order preserved."""
    tickers = []
    seen = set()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = (row.get("ticker") or "").strip().upper()
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)
    return tickers


def _resolve_universe(args) -> tuple[list[str], str]:
    if args.ticker:
        return [args.ticker.upper()], f"single ticker ({args.ticker.upper()})"
    if args.universe == "mag7":
        return list(MAG7_TICKERS.keys()), "mag7 (runners/main.py TICKERS)"
    return _load_tickers_from_csv(_DEFAULT_RATINGS_CSV), "sp500 (ratings.csv)"


# ── Per-ticker test ────────────────────────────────────────────────────────────

def _yfinance_consensus_gross_margin(ticker_obj) -> tuple:
    """
    Consensus gross margin from yfinance: prefer fiscal-year-matched annual
    data (Gross Profit / Total Revenue from the same FY) over TTM, so the
    comparison lines up with the pipeline's most-recent-FY figure. Split
    out of test_ticker() -- unmodified -- so scripts/validate_universe.py
    can call it on a ticker_obj it already fetched, without a second
    yfinance round-trip for the same data.
    """
    try:
        financials = ticker_obj.financials  # annual, columns are FY end dates
        if financials is not None and not financials.empty:
            latest_col = financials.columns[0]
            gross_profit = financials.loc["Gross Profit", latest_col] if "Gross Profit" in financials.index else None
            revenue = financials.loc["Total Revenue", latest_col] if "Total Revenue" in financials.index else None
            if gross_profit and revenue and revenue != 0:
                return gross_profit / revenue, f"yfinance annual FY {str(latest_col)[:10]}"
        return ticker_obj.info.get("grossMargins", None), "yfinance TTM (fallback)"
    except Exception:
        return ticker_obj.info.get("grossMargins", None), "yfinance TTM (fallback)"


def evaluate_gross_margin(ticker: str, sector: str, data: dict,
                          consensus_gm, consensus_source: str,
                          initial_notes: str = "") -> dict:
    """
    Pure classification step: compares the pipeline's most-recent-FY gross
    margin (already resolved, in `data` from run_data_only()) against an
    already-fetched yfinance consensus figure. Split out of test_ticker()
    -- unmodified below this point -- so a shared multi-check loader
    (scripts/validate_universe.py) can call it on data it already loaded,
    without a second run_data_only() call.
    """
    result = {
        "ticker":            ticker,
        "sector":            sector,
        "pipeline_gm":       None,
        "consensus_gm":      consensus_gm,
        "consensus_source":  consensus_source,
        "delta_pp":          None,
        "match":             "❌",
        "notes":             initial_notes,
    }

    fundamental = data.get("fundamental") or {}
    periods     = fundamental.get("periods") or []
    gm_by_period = fundamental.get("gross_margin") or {}
    gm_label     = fundamental.get("gross_margin_label") or "Gross Margin"

    pipeline_gm = None
    raw_gm = gm_by_period.get(periods[0]) if periods else None
    if isinstance(raw_gm, str) and "%" in raw_gm:
        pipeline_gm = _parse_pct(raw_gm)
    result["pipeline_gm"] = pipeline_gm

    if not result["sector"]:
        result["sector"] = data.get("sector") or ""

    # A subset of sectors deliberately report a DIFFERENT metric under the
    # gross_margin field, by the pipeline's own design (see gross_margin_label
    # in core.agents.FundamentalAgent) -- Net Interest Margin for financials,
    # Operating Cost Ratio for energy. Neither is comparable to a COGS-based
    # gross margin, so diffing either against yfinance's grossMargins is an
    # apples-to-oranges comparison, not a pipeline failure. Net Revenue
    # Margin (freight brokers) is deliberately excluded from this list --
    # it's engineered as a genuine gross-margin proxy and validates within
    # threshold against consensus.
    _non_comparable_labels = {"Net Interest Margin", "Operating Cost Ratio"}

    # -- Delta + classification ----------------------------------------------
    if isinstance(raw_gm, str) and "(yfinance" in raw_gm:
        # Pipeline's own SEC-XBRL computation returned nothing usable and
        # fell back to yfinance (see core.agents._yfinance_gross_margin_fallback).
        # Consensus in THIS script is also sourced from yfinance, so diffing
        # the two would just be comparing yfinance to itself -- not an
        # independent validation. Still useful for the report (a real
        # number beats a bare N/A), just excluded from the KPI.
        result["notes"] = f"SOURCE_FALLBACK — pipeline used yfinance ({raw_gm}), not an independent check"
        result["match"] = "➖"
    elif isinstance(raw_gm, str) and raw_gm.startswith("DEFINITION_MISMATCH"):
        # Deliberate sector routing (Utilities/REITs), not a pipeline
        # failure -- consensus uses a genuinely different metric definition
        # (NOI margin, fuel-cost-adjusted margin) that isn't comparable to
        # a COGS-based gross margin. Excluded from the delta KPI.
        result["notes"] = f"DEFINITION_MISMATCH — excluded from delta KPI ({raw_gm})"
        result["match"] = "➖"
    elif gm_label in _non_comparable_labels:
        result["notes"] = f"DEFINITION_MISMATCH — {gm_label} is not gross margin, excluded from delta KPI"
        result["match"] = "➖"
    elif pipeline_gm is None:
        result["notes"] = (f"pipeline N/A: {raw_gm}" if raw_gm else "pipeline returned N/A")
        result["match"] = "❌"
    elif consensus_gm is None:
        result["notes"] = "yfinance no grossMargins"
        result["match"] = "❌"
    else:
        delta_pp = (pipeline_gm - consensus_gm) * 100
        result["delta_pp"] = round(delta_pp, 2)
        abs_d = abs(delta_pp)
        result["match"] = "✅" if abs_d <= _MATCH_THRESHOLD else (
            "⚠️" if abs_d <= _WARN_THRESHOLD else "❌"
        )
        if abs_d > _WARN_THRESHOLD:
            result["notes"] = "investigate: possible segment subtotal or GAAP/adjusted gap"

    return result


def test_ticker(orch: EquityAnalystOrchestrator, ticker: str,
                skip_financials: bool) -> dict | None:
    """
    Standalone entry point (used when this script runs on its own): fetches
    yfinance sector/consensus and runs the pipeline itself, then delegates
    the actual comparison to evaluate_gross_margin(). Returns None if the
    ticker was skipped (--skip-financials and the yfinance-reported sector
    is Financials). Never raises -- any exception is captured in "notes".
    """
    ticker_obj = yf.Ticker(ticker)
    notes = ""
    try:
        info = ticker_obj.info
    except Exception as e:
        info = {}
        notes = f"yfinance fetch error: {e}"

    sector = info.get("sector") or ""
    is_financials = "financial" in sector.lower()
    if is_financials and skip_financials:
        return None

    consensus_gm, consensus_source = _yfinance_consensus_gross_margin(ticker_obj)

    try:
        data = orch.run_data_only(ticker, sector=sector)
    except Exception as e:
        return {
            "ticker": ticker, "sector": sector, "pipeline_gm": None,
            "consensus_gm": consensus_gm, "consensus_source": consensus_source,
            "delta_pp": None, "match": "❌", "notes": f"pipeline error: {e}",
        }

    return evaluate_gross_margin(ticker, sector, data, consensus_gm,
                                 consensus_source, initial_notes=notes)


# ── Reporting ──────────────────────────────────────────────────────────────────

def _fmt_pct(v) -> str:
    return f"{v * 100:.2f}%" if v is not None else "N/A"


def _fmt_pp(v) -> str:
    return f"{v:+.2f}pp" if v is not None else "N/A"


def _print_progress_row(r: dict) -> None:
    print(f"{r['ticker']:<8} {_fmt_pct(r['pipeline_gm']):>10} {_fmt_pct(r['consensus_gm']):>10} "
          f"{_fmt_pp(r['delta_pp']):>10}  {r['match']}  {r['notes']}")


def _print_summary(results: list) -> None:
    total = len(results)
    ok    = [r for r in results if r["match"] == "✅"]
    warn  = [r for r in results if r["match"] == "⚠️"]
    bad   = [r for r in results if r["match"] == "❌"]
    mismatch = [r for r in results if r["match"] == "➖"]
    deltas = [abs(r["delta_pp"]) for r in results if r["delta_pp"] is not None]
    mae = sum(deltas) / len(deltas) if deltas else None
    # KPI rate excludes DEFINITION_MISMATCH tickers -- they're a deliberate
    # sector-routing decision (metric genuinely isn't comparable), not part
    # of the pipeline's pass/fail population.
    kpi_total = total - len(mismatch)
    match_rate = (len(ok) / kpi_total * 100) if kpi_total else None

    print("\n" + "=" * 78)
    print("SUMMARY:")
    print("=" * 78)
    print(f"  Total tested:        {total}")
    print(f"  ✅ within ±{_MATCH_THRESHOLD}pp:      {len(ok)}")
    print(f"  ⚠️  within ±5-{_WARN_THRESHOLD}pp:     {len(warn)}")
    print(f"  ❌ beyond ±{_WARN_THRESHOLD}pp / N/A: {len(bad)}")
    print(f"  ➖ DEFINITION_MISMATCH (excluded from KPI): {len(mismatch)}")
    print(f"  Match rate (of {kpi_total} KPI-eligible): "
          f"{match_rate:.1f}%" if match_rate is not None else "  Match rate: N/A")
    print(f"  Mean absolute error: {mae:.2f}pp" if mae is not None else "  Mean absolute error: N/A")

    if bad:
        print(f"\n  ❌ tickers ({len(bad)}):")
        for r in bad:
            delta_str = _fmt_pp(r["delta_pp"]) if r["delta_pp"] is not None else "N/A"
            print(f"    {r['ticker']:<8} delta={delta_str:<10} {r['notes']}")

    if mismatch:
        print(f"\n  ➖ DEFINITION_MISMATCH tickers ({len(mismatch)}):")
        for r in mismatch:
            print(f"    {r['ticker']:<8} {r['notes']}")


def _write_csv_report(results: list) -> None:
    with open(_REPORT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ticker", "sector", "pipeline_gm", "consensus_gm",
            "consensus_source", "delta_pp", "match", "notes",
        ])
        for r in results:
            writer.writerow([
                r["ticker"],
                r["sector"],
                r["pipeline_gm"] if r["pipeline_gm"] is not None else "",
                r["consensus_gm"] if r["consensus_gm"] is not None else "",
                r["consensus_source"],
                r["delta_pp"] if r["delta_pp"] is not None else "",
                r["match"],
                r["notes"],
            ])
    print(f"\nFull results saved to: {_REPORT_PATH}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Validate pipeline gross margin against yfinance consensus")
    parser.add_argument("--universe", choices=["sp500", "mag7"], default="sp500",
                        help="Ticker universe to test (default: sp500, from ratings.csv)")
    parser.add_argument("--ticker", default=None,
                        help="Single ticker to test -- overrides --universe")
    parser.add_argument("--delay", type=float, default=2,
                        help="Seconds to sleep between tickers (default: 2)")
    parser.add_argument("--skip-financials", action="store_true",
                        help="Skip tickers whose yfinance sector is Financials")
    args = parser.parse_args()

    start_time = time.time()

    orch = EquityAnalystOrchestrator(edgar_identity=EDGAR_IDENTITY)

    tickers, universe_label = _resolve_universe(args)
    n = len(tickers)

    print(f"Universe: {universe_label}  |  {n} ticker(s) to test\n")
    print(f"{'Ticker':<8} {'Pipeline GM':>10} {'Consensus':>10} {'Delta':>10}  Match  Notes")
    print("-" * 78)

    results = []
    skipped = 0
    for idx, ticker in enumerate(tickers, 1):
        r = test_ticker(orch, ticker, args.skip_financials)
        if r is None:
            skipped += 1
            print(f"{ticker:<8} skipped (Financials sector)")
        else:
            results.append(r)
            _print_progress_row(r)
        if idx < n:
            time.sleep(args.delay)

    if skipped:
        print(f"\n({skipped} ticker(s) skipped via --skip-financials, excluded from totals)")

    _print_summary(results)
    _write_csv_report(results)

    elapsed = time.time() - start_time
    mins, secs = divmod(int(elapsed), 60)
    print(f"Total runtime: {mins}m {secs}s")


if __name__ == "__main__":
    main()
