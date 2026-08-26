"""
test_ticker_availability.py -- Diagnose ticker -> CIK -> SEC facts availability

Reuses the existing CIK resolution and period-discovery logic from
data/facts_processor.py (_get_cik, _get_facts, _discover_periods) rather
than duplicating it. For each ticker in the chosen universe, resolves the
CIK, fetches company facts, counts us-gaap concept keys, and counts annual
periods discovered -- then classifies the result so wrong/shell CIK
mappings (like the XOM -> ExxonMobil Holdings Corp case) can be spotted
and queued for a manual override.

Usage
-----
    python scripts/test_ticker_availability.py                    # MAG7 (default)
    python scripts/test_ticker_availability.py --universe mag7
    python scripts/test_ticker_availability.py --universe sp500   # reads ratings.csv
    python scripts/test_ticker_availability.py --csv my_list.csv  # any CSV with a "ticker" column

Output
------
Live progress table on stdout, an edge-case summary, run statistics, and
a full CSV report at scripts/ticker_availability_report.csv.
"""

import os
import sys
import csv
import time
import argparse

_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _PROJECT_ROOT)

import data.facts_processor as fp
from runners.main import TICKERS as MAG7_TICKERS, EDGAR_IDENTITY

_DEFAULT_RATINGS_CSV = os.path.join(_PROJECT_ROOT, "ratings.csv")
_REPORT_PATH         = os.path.join(_SCRIPT_DIR, "ticker_availability_report.csv")

_MIN_US_GAAP_CONCEPTS = 50     # same threshold as data/facts_processor.py's _get_cik
_DELAY_SECS           = 1      # courtesy delay between tickers -- SEC rate limits


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
    if args.csv_path:
        return _load_tickers_from_csv(args.csv_path), f"custom CSV ({args.csv_path})"
    if args.universe == "sp500":
        return _load_tickers_from_csv(_DEFAULT_RATINGS_CSV), "sp500 (ratings.csv)"
    return list(MAG7_TICKERS.keys()), "mag7 (runners/main.py TICKERS)"


# ── Per-ticker test ────────────────────────────────────────────────────────────

def test_ticker(ticker: str) -> dict:
    """
    Resolve CIK, fetch facts, count us-gaap concepts + annual periods,
    and classify the result. Never raises -- any exception is captured
    and reported as status ERROR.
    """
    result = {
        "ticker":           ticker,
        "cik":              None,
        "entity_name":      None,
        "us_gaap_concepts": 0,
        "annual_periods":   0,
        "status":           None,
        "notes":            "",
    }
    try:
        cik = fp._get_cik(ticker)
        if not cik:
            result["status"] = "NO_CIK"
            result["notes"]  = "Ticker not found in SEC company_tickers.json mapping"
            return result
        result["cik"] = cik

        facts = fp._get_facts(cik)
        if not facts:
            result["status"] = "BAD_CIK"
            result["notes"]  = "CIK resolved but company facts fetch returned no data"
            return result
        result["entity_name"] = facts.get("entityName")

        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        concept_count = len(us_gaap)
        result["us_gaap_concepts"] = concept_count

        if concept_count < _MIN_US_GAAP_CONCEPTS:
            result["status"] = "BAD_CIK"
            result["notes"]  = (
                f"us-gaap concept count ({concept_count}) below threshold "
                f"({_MIN_US_GAAP_CONCEPTS}) -- likely wrong entity/shell filer"
            )
            return result

        periods = fp._discover_periods(us_gaap, 0)   # 0 = no cap, count all
        period_count = len(periods)
        result["annual_periods"] = period_count

        if period_count >= 1:
            result["status"] = "OK"
        else:
            result["status"] = "WARN"
            result["notes"]  = "us-gaap concepts sufficient but no annual periods discovered"

    except Exception as e:
        result["status"] = "ERROR"
        result["notes"]  = f"{type(e).__name__}: {e}"

    return result


# ── Reporting ──────────────────────────────────────────────────────────────────

def _print_progress_row(r: dict) -> None:
    cik_str = r["cik"] or "—"
    print(f"{r['ticker']:<8} {cik_str:<12} {r['us_gaap_concepts']:>9} {r['annual_periods']:>8}  {r['status']}")


def _unresolvable_note(note: str) -> bool:
    """BCR-type entries: description flags the ticker as too old / truly gone."""
    note_lower = note.lower()
    return "too old" in note_lower or "skip" in note_lower


def _print_edge_case_row(r: dict) -> None:
    if r["status"] == "BAD_CIK":
        print(f"  {r['ticker']:<8} BAD_CIK   CIK={r['cik'] or '—'}  "
              f"Entity=\"{r['entity_name'] or 'unknown'}\"  "
              f"us-gaap={r['us_gaap_concepts']}  periods={r['annual_periods']}")
        print(f"           -> {r['notes']}")
    elif r["status"] == "NO_CIK":
        print(f"  {r['ticker']:<8} NO_CIK    {r['notes']}")
    elif r["status"] == "WARN":
        print(f"  {r['ticker']:<8} WARN      CIK={r['cik']}  "
              f"Entity=\"{r['entity_name'] or 'unknown'}\"  "
              f"us-gaap={r['us_gaap_concepts']}  periods={r['annual_periods']}")
        print(f"           -> {r['notes']}")
    elif r["status"] == "ERROR":
        print(f"  {r['ticker']:<8} ERROR     {r['notes']}")
    else:
        print(f"  {r['ticker']:<8} {r['status']:<9} {r['notes']}")


def _print_edge_cases(results: list) -> None:
    edge_cases = [r for r in results if r["status"] != "OK"]

    print("\n" + "=" * 78)
    print("EDGE CASES REQUIRING INTERVENTION:")
    print("=" * 78)

    if not edge_cases:
        print("  (none -- every ticker resolved OK)")
        return

    group1_needs_override = []   # BAD_CIK, wrong entity, not yet a known delisting/20-F case
    group2_20f_filers     = []
    group3_delisted       = []
    group4_unresolvable   = []
    group_other           = []   # anything not covered by the four groups above

    for r in edge_cases:
        ticker = r["ticker"]
        if ticker in fp.KNOWN_20F_FILERS:
            group2_20f_filers.append(r)
        elif ticker in fp.KNOWN_DELISTINGS:
            if _unresolvable_note(fp.KNOWN_DELISTINGS[ticker]):
                group4_unresolvable.append(r)
            else:
                group3_delisted.append(r)
        elif r["status"] == "BAD_CIK":
            group1_needs_override.append(r)
        else:
            group_other.append(r)

    print("\nGROUP 1 -- NEEDS CIK OVERRIDE (BAD_CIK with wrong entity)")
    print("-" * 78)
    if group1_needs_override:
        for r in group1_needs_override:
            _print_edge_case_row(r)
            print(f"           -> Suggest: Add to _CIK_OVERRIDES with correct CIK")
    else:
        print("  (none)")

    print("\nGROUP 2 -- 20-F FILERS (foreign companies, use 20-F ingestion path)")
    print("-" * 78)
    if group2_20f_filers:
        for r in group2_20f_filers:
            _print_edge_case_row(r)
            print(f"           -> {fp.KNOWN_20F_FILERS[r['ticker']]}")
    else:
        print("  (none)")

    print("\nGROUP 3 -- DELISTED/ACQUIRED (historical data only via last 10-K)")
    print("-" * 78)
    if group3_delisted:
        for r in group3_delisted:
            _print_edge_case_row(r)
            print(f"           -> {fp.KNOWN_DELISTINGS[r['ticker']]}")
    else:
        print("  (none)")

    print("\nGROUP 4 -- UNRESOLVABLE (BCR-type, too old or truly gone)")
    print("-" * 78)
    if group4_unresolvable:
        for r in group4_unresolvable:
            _print_edge_case_row(r)
            print(f"           -> {fp.KNOWN_DELISTINGS[r['ticker']]}")
    else:
        print("  (none)")

    if group_other:
        print("\nUNCLASSIFIED (not in _CIK_OVERRIDES-related lists -- investigate manually)")
        print("-" * 78)
        for r in group_other:
            _print_edge_case_row(r)
            if r["status"] == "NO_CIK":
                print(f"           -> Suggest: Add to _CIK_OVERRIDES with correct CIK")


def _print_statistics(results: list) -> None:
    total = len(results)
    counts = {"OK": 0, "WARN": 0, "BAD_CIK": 0, "NO_CIK": 0, "ERROR": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    print("\n" + "=" * 78)
    print("STATISTICS:")
    print("=" * 78)
    print(f"  Total tested: {total}")
    print(f"  OK:           {counts['OK']}")
    print(f"  Edge cases:   {total - counts['OK']}")
    print(f"    WARN:       {counts['WARN']}")
    print(f"    BAD_CIK:    {counts['BAD_CIK']}")
    print(f"    NO_CIK:     {counts['NO_CIK']}")
    print(f"    ERROR:      {counts['ERROR']}")


def _write_csv_report(results: list) -> None:
    with open(_REPORT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ticker", "cik", "entity_name", "us_gaap_concepts",
            "annual_periods", "status", "notes",
        ])
        for r in results:
            writer.writerow([
                r["ticker"],
                r["cik"] or "",
                r["entity_name"] or "",
                r["us_gaap_concepts"],
                r["annual_periods"],
                r["status"],
                r["notes"],
            ])
    print(f"\nFull results saved to: {_REPORT_PATH}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test ticker -> CIK -> SEC facts availability")
    parser.add_argument("--universe", choices=["mag7", "sp500"], default="mag7",
                        help="Ticker universe to test (default: mag7)")
    parser.add_argument("--csv", dest="csv_path", default=None,
                        help="Path to a CSV file with a 'ticker' column -- overrides --universe")
    args = parser.parse_args()

    fp.set_identity(EDGAR_IDENTITY)

    tickers, universe_label = _resolve_universe(args)
    n = len(tickers)

    print(f"Universe: {universe_label}  |  {n} ticker(s) to test\n")
    print(f"{'Ticker':<8} {'CIK':<12} {'us-gaap':>9} {'Periods':>8}  Status")
    print("-" * 60)

    results = []
    for idx, ticker in enumerate(tickers, 1):
        r = test_ticker(ticker)
        results.append(r)
        _print_progress_row(r)
        if idx < n:
            time.sleep(_DELAY_SECS)

    _print_edge_cases(results)
    _print_statistics(results)
    _write_csv_report(results)


if __name__ == "__main__":
    main()
