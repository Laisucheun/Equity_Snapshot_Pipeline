"""
validate_summary_reconciliation.py -- Sweeps the sp500 universe (ratings.csv)
and counts "SUMMARY CHECK ... MISMATCH" occurrences from
core.renderer.check_summary_mismatches() -- the same assertion
EquityBriefRenderer.render() runs on every summary figure that has a table
counterpart (see core/renderer.py, _build_exec_summary()).

Runs via EquityAnalystOrchestrator.run_data_only() -- no PDF, no debt-note/
FRED/momentum fetches, none of which the exec summary reads from -- then
calls _build_exec_summary() + check_summary_mismatches() directly on the
returned dicts. This is the same check render() runs, just reached without
paying for a PDF: run_data_only() already returns every dict
_build_exec_summary() takes (fundamental, valuation, commentary, risk,
insider, analyst_targets, peer_comparison, periods, track_analysis,
management_quality).

Resumable: writes one row per ticker to the output CSV as it goes (flushed
immediately), and on startup skips any ticker already present there. Kill
it any time -- rerunning the same command picks up where it left off. Use
--force to ignore the existing CSV and start clean.

Usage
-----
    python scripts/validate_summary_reconciliation.py                # sp500 (ratings.csv)
    python scripts/validate_summary_reconciliation.py --ticker NVDA
    python scripts/validate_summary_reconciliation.py --limit 50
    python scripts/validate_summary_reconciliation.py --delay 1
    python scripts/validate_summary_reconciliation.py --force         # ignore existing CSV, start over

Output
------
Progress line per ticker, and scripts/summary_reconciliation_report.csv
(ticker, sector, status, n_checks, n_mismatches, mismatch_detail).
"""

import os
import sys
import csv
import time
import argparse

_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from core.orchestrator import EquityAnalystOrchestrator
from core.renderer import _build_exec_summary, check_summary_mismatches

_DEFAULT_RATINGS_CSV = os.path.join(_PROJECT_ROOT, "ratings.csv")
_REPORT_PATH = os.path.join(_SCRIPT_DIR, "summary_reconciliation_report.csv")
_FIELDS = ["ticker", "sector", "status", "n_checks", "n_mismatches", "mismatch_detail"]
EDGAR_IDENTITY = "Your Name your@email.com"


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


def evaluate_summary(ticker: str, result: dict) -> tuple:
    """
    Pure classification step: builds the exec-summary bullets from an
    already-loaded run_data_only() result and returns (checks, mismatches).
    Thin wrapper -- same shape as evaluate_gross_margin()/
    evaluate_cash_metrics() -- so scripts/validate_universe.py can call all
    three checks on one load uniformly.
    """
    _, checks = _build_exec_summary(
        ticker=ticker,
        fundamental=result["fundamental"],
        valuation=result["valuation"],
        commentary=result["commentary"],
        management_quality=result.get("management_quality"),
        insider_activity=result.get("insider"),
        analyst_targets=result.get("analyst_targets"),
        peer_comparison=result.get("peer_comparison"),
        periods=result["periods"],
        track_analysis=result.get("track_analysis"),
        risk=result.get("risk"),
    )
    mismatches = check_summary_mismatches(ticker, checks)
    return checks, mismatches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="single ticker, overrides the universe")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the number of tickers processed (from the top of the universe)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds to sleep between tickers (SEC rate limiting)")
    parser.add_argument("--force", action="store_true",
                        help="ignore the existing report CSV and reprocess every ticker")
    args = parser.parse_args()

    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        tickers = _load_tickers_from_csv(_DEFAULT_RATINGS_CSV)
        if args.limit:
            tickers = tickers[:args.limit]

    done = set() if args.force else _load_done_tickers(_REPORT_PATH)
    todo = [t for t in tickers if t not in done]

    write_header = args.force or not os.path.exists(_REPORT_PATH)
    mode = "w" if args.force else "a"
    csv_file = open(_REPORT_PATH, mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=_FIELDS)
    if write_header:
        writer.writeheader()
        csv_file.flush()

    orch = EquityAnalystOrchestrator(edgar_identity=EDGAR_IDENTITY)

    n = len(tickers)
    print(f"Summary-check sweep: {n} tickers total, {len(done)} already done, "
          f"{len(todo)} to go  |  report -> {_REPORT_PATH}\n")

    total_mismatches = 0
    n_ok = 0
    n_failed = 0

    for idx, ticker in enumerate(todo, 1):
        row = {"ticker": ticker, "sector": "", "status": "", "n_checks": 0,
               "n_mismatches": 0, "mismatch_detail": ""}
        try:
            result = orch.run_data_only(ticker)
            row["sector"] = result.get("sector", "")

            checks, mismatches = evaluate_summary(ticker, result)

            row["status"] = "ok"
            row["n_checks"] = len(checks)
            row["n_mismatches"] = len(mismatches)
            row["mismatch_detail"] = " | ".join(mismatches)
            n_ok += 1
            total_mismatches += len(mismatches)

            flag = "" if not mismatches else f"  <-- {len(mismatches)} MISMATCH"
            print(f"{idx:>4}/{len(todo)}  {ticker:<8}  OK{flag}")
            for m in mismatches:
                print(f"         {m}")

        except Exception as e:
            row["status"] = f"skip: {str(e)[:100]}"
            n_failed += 1
            print(f"{idx:>4}/{len(todo)}  {ticker:<8}  SKIP ({str(e)[:70]})")

        writer.writerow(row)
        csv_file.flush()

        if idx < len(todo):
            time.sleep(args.delay)

    csv_file.close()

    print("\n" + "=" * 70)
    print(f"  This run:  {n_ok} ok, {n_failed} skipped (of {len(todo)} attempted)")
    print(f"  This run's SUMMARY CHECK mismatches: {total_mismatches}")
    print(f"  Full report (all runs, cumulative): {_REPORT_PATH}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
