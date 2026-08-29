"""
validate_universe.py -- Consolidated universe sweep. Loads each ticker
ONCE and runs all four existing validation checks against that one load,
instead of four separate scripts each re-fetching the same SEC/yfinance
data independently (measured at ~90s/ticker even for the lightest of the
four -- the cost is per-ticker network I/O, so four separate sweeps cost
~4x one).

Reuses each check's classification logic UNMODIFIED via evaluate_*()
functions extracted from the original scripts for this purpose:
    - test_ticker_availability.test_ticker()        (already load-free --
      run first; also warms data.facts_processor's in-process _facts_cache,
      so the run_data_only() call below resolves the same CIK/facts via
      that cache rather than a second SEC fetch)
    - validate_gross_margin.evaluate_gross_margin()
    - validate_cash_metrics.evaluate_cash_metrics()
    - validate_summary_reconciliation.evaluate_summary()
Each of those three evaluate_*() functions is a mechanical extraction --
same comparison/threshold/classification code, just taking already-fetched
inputs instead of fetching them itself. The original four scripts still
work standalone (each keeps its own test_ticker() that fetches its own
inputs and calls the same evaluate_*() function this script calls).

KNOWN DEVIATION 1 -- sector passed to run_data_only()
------------------------------------------------------
validate_gross_margin.py and validate_cash_metrics.py both pass the
yfinance-reported sector into run_data_only() ("passing sector=None
silently disables sector-aware logic" -- their own comment). The original
validate_summary_reconciliation.py passed no sector (SIC auto-detect).
Loading once means one sector choice has to govern all three checks; this
script uses the yfinance sector, the choice already made by 2 of the 3.
Net effect: the summary-reconciliation check now runs under yfinance-
sector routing instead of SIC-auto-detect routing. These agree for the
overwhelming majority of tickers; a ticker where they diverge is worth
its own investigation, not something to route around here -- flagging
rather than silently resolving, per instructions.

KNOWN DEVIATION 2 -- one row per ticker (cash-metrics was 9 rows/ticker)
---------------------------------------------------------------------
validate_cash_metrics.py's own report has one row per (ticker, metric) --
9 rows per ticker. This script's shared report is one row per ticker, so
those 9 metrics become 9 column groups
(cash_<metric>_pipeline/yfinance/delta/match/source/notes) on that
ticker's single row. The per-metric comparison logic is unchanged; only
the row shape changed, to fit the "one report, one row per ticker" ask.

Usage
-----
    python scripts/validate_universe.py                # sp500 (ratings.csv)
    python scripts/validate_universe.py --ticker NVDA
    python scripts/validate_universe.py --limit 50
    python scripts/validate_universe.py --delay 1
    python scripts/validate_universe.py --force         # ignore existing CSV, start over

Output
------
Progress line per ticker, and scripts/universe_validation_report.csv --
one row per ticker, column groups avail_*/gm_*/cash_<metric>_*/summary_*.
Resumable: skips any ticker already present in that CSV on startup.
"""

import os
import sys
import csv
import time
import argparse

_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _SCRIPT_DIR)

import yfinance as yf

from core.orchestrator import EquityAnalystOrchestrator

import test_ticker_availability as tka
import validate_gross_margin as vgm
import validate_cash_metrics as vcm
import validate_summary_reconciliation as vsr
from runners.main import TICKERS as MAG7_TICKERS

_DEFAULT_RATINGS_CSV = os.path.join(_PROJECT_ROOT, "ratings.csv")
_REPORT_PATH = os.path.join(_SCRIPT_DIR, "universe_validation_report.csv")
EDGAR_IDENTITY = "Your Name your@email.com"

_CASH_METRICS = vcm._METRICS

_FIELDS = (
    ["ticker", "sector", "status"]
    + ["avail_cik", "avail_entity_name", "avail_us_gaap_concepts",
       "avail_annual_periods", "avail_status", "avail_notes"]
    + ["gm_pipeline", "gm_consensus", "gm_consensus_source", "gm_delta_pp",
       "gm_match", "gm_notes"]
    + [f"cash_{m}_{suffix}" for m in _CASH_METRICS
       for suffix in ("pipeline", "yfinance", "delta", "match", "source", "notes")]
    + ["summary_n_checks", "summary_n_mismatches", "summary_mismatch_detail"]
)


def _load_tickers_from_csv(path: str) -> list[str]:
    tickers = []
    seen = set()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = (row.get("ticker") or "").strip().upper()
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)
    return tickers


def _load_done_tickers(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["ticker"] for row in csv.DictReader(f)}


def _resolve_universe(args) -> list[str]:
    """Same convention as the other three scripts: --ticker overrides
    --universe; --universe defaults to sp500 (ratings.csv)."""
    if args.ticker:
        return [args.ticker.upper()]
    if args.universe == "mag7":
        tickers = list(MAG7_TICKERS.keys())
    else:
        tickers = _load_tickers_from_csv(_DEFAULT_RATINGS_CSV)
    if args.limit:
        tickers = tickers[:args.limit]
    return tickers


def _blank_row(ticker: str) -> dict:
    row = {field: "" for field in _FIELDS}
    row["ticker"] = ticker
    return row


def process_ticker(orch: EquityAnalystOrchestrator, ticker: str) -> dict:
    row = _blank_row(ticker)

    # ── 1. Ticker availability -- cheap, and warms facts_processor's
    #        in-process _facts_cache for the run_data_only() call below ──
    avail = tka.test_ticker(ticker)
    row["avail_cik"]              = avail["cik"] or ""
    row["avail_entity_name"]      = avail["entity_name"] or ""
    row["avail_us_gaap_concepts"] = avail["us_gaap_concepts"]
    row["avail_annual_periods"]   = avail["annual_periods"]
    row["avail_status"]           = avail["status"]
    row["avail_notes"]            = avail["notes"]

    if avail["status"] in ("NO_CIK", "BAD_CIK", "ERROR"):
        row["status"] = f"skip: unavailable ({avail['status']})"
        return row

    # ── 2. Shared yfinance fetch: sector + statements for gm/cash consensus,
    #        one Ticker object reused across both checks below ──
    tk = yf.Ticker(ticker)
    try:
        info = tk.info
    except Exception:
        info = {}
    sector = info.get("sector") or ""

    # ── 3. ONE pipeline load, reused by all three data-dependent checks ──
    try:
        result = orch.run_data_only(ticker, sector=sector)
    except Exception as e:
        row["status"] = f"skip: ingestion failed: {str(e)[:100]}"
        return row

    row["sector"] = result.get("sector", sector)
    row["status"] = "ok"

    # ── 4. Gross margin ──
    consensus_gm, consensus_source = vgm._yfinance_consensus_gross_margin(tk)
    gm = vgm.evaluate_gross_margin(ticker, sector, result, consensus_gm, consensus_source)
    row["gm_pipeline"]         = gm["pipeline_gm"] if gm["pipeline_gm"] is not None else ""
    row["gm_consensus"]        = gm["consensus_gm"] if gm["consensus_gm"] is not None else ""
    row["gm_consensus_source"] = gm["consensus_source"]
    row["gm_delta_pp"]         = gm["delta_pp"] if gm["delta_pp"] is not None else ""
    row["gm_match"]            = gm["match"]
    row["gm_notes"]            = gm["notes"]

    # ── 5. Cash metrics (9 metrics -> 9 column groups on this one row) ──
    yf_consensus = vcm._yfinance_consensus_cash_metrics(tk)
    cash_rows = vcm.evaluate_cash_metrics(ticker, sector, result, yf_consensus, xbrl_only=False)
    for r in (cash_rows or []):
        m = r["metric"]
        row[f"cash_{m}_pipeline"] = r["pipeline_val"] if r["pipeline_val"] is not None else ""
        row[f"cash_{m}_yfinance"] = r["yfinance_val"] if r["yfinance_val"] is not None else ""
        row[f"cash_{m}_delta"]    = r["delta_pct"] if r["delta_pct"] is not None else ""
        row[f"cash_{m}_match"]    = r["match"]
        row[f"cash_{m}_source"]   = r["source"]
        row[f"cash_{m}_notes"]    = r["notes"]

    # ── 6. Summary reconciliation ──
    checks, mismatches = vsr.evaluate_summary(ticker, result)
    row["summary_n_checks"]        = len(checks)
    row["summary_n_mismatches"]    = len(mismatches)
    row["summary_mismatch_detail"] = " | ".join(mismatches)

    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", choices=["sp500", "mag7"], default="sp500",
                        help="Ticker universe (default: sp500, from ratings.csv) -- "
                             "same convention as the other three validation scripts")
    parser.add_argument("--ticker", help="single ticker, overrides --universe")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the number of tickers processed (from the top of the universe)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds to sleep between tickers (SEC rate limiting)")
    parser.add_argument("--force", action="store_true",
                        help="ignore the existing report CSV and reprocess every ticker")
    args = parser.parse_args()

    tickers = _resolve_universe(args)

    done = set() if args.force else _load_done_tickers(_REPORT_PATH)
    todo = [t for t in tickers if t not in done]

    write_header = args.force or not os.path.exists(_REPORT_PATH)
    mode = "w" if args.force else "a"
    csv_file = open(_REPORT_PATH, mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=_FIELDS)
    if write_header:
        writer.writeheader()
        csv_file.flush()

    tka.fp.set_identity(EDGAR_IDENTITY)
    orch = EquityAnalystOrchestrator(edgar_identity=EDGAR_IDENTITY)

    n = len(tickers)
    print(f"Universe sweep (4-in-1): {n} tickers total, {len(done)} already done, "
          f"{len(todo)} to go  |  report -> {_REPORT_PATH}\n")

    n_ok = n_skip = 0
    total_gm_bad = total_cash_bad = total_summary_mismatch = 0

    for idx, ticker in enumerate(todo, 1):
        try:
            row = process_ticker(orch, ticker)
        except Exception as e:
            row = _blank_row(ticker)
            row["status"] = f"skip: unexpected error: {str(e)[:100]}"

        writer.writerow(row)
        csv_file.flush()

        if row["status"] == "ok":
            n_ok += 1
            gm_bad = row["gm_match"] == "❌"
            cash_bad = any(row.get(f"cash_{m}_match") == "❌" for m in _CASH_METRICS)
            n_mismatch = row["summary_n_mismatches"] or 0
            total_gm_bad += int(gm_bad)
            total_cash_bad += int(cash_bad)
            total_summary_mismatch += n_mismatch
            flags = []
            if gm_bad:
                flags.append("GM-FAIL")
            if cash_bad:
                flags.append("CASH-FAIL")
            if n_mismatch:
                flags.append(f"{n_mismatch}-SUMMARY-MISMATCH")
            flag_str = f"  <-- {', '.join(flags)}" if flags else ""
            print(f"{idx:>4}/{len(todo)}  {ticker:<8}  OK{flag_str}")
        else:
            n_skip += 1
            print(f"{idx:>4}/{len(todo)}  {ticker:<8}  {row['status']}")

        if idx < len(todo):
            time.sleep(args.delay)

    csv_file.close()

    print("\n" + "=" * 70)
    print(f"  This run: {n_ok} ok, {n_skip} skipped (of {len(todo)} attempted)")
    print(f"  Gross-margin ❌:           {total_gm_bad}")
    print(f"  Cash-metrics (any ❌):     {total_cash_bad}")
    print(f"  Summary-check mismatches: {total_summary_mismatch}")
    print(f"  Full report (cumulative): {_REPORT_PATH}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
