"""
probe_xbrl_debt.py — Probe SEC XBRL company facts API for debt concepts

Checks how many tickers have structured XBRL debt data vs requiring text parsing.

Key XBRL concepts for debt:
  DebtInstrumentInterestRateStatedPercentage  — stated rate per tranche
  DebtInstrumentFaceAmount                   — principal per tranche
  DebtInstrumentMaturityDate                 — maturity per tranche
  LongTermDebt                               — total LT debt
  LongTermDebtCurrent                        — current portion
  ShortTermBorrowings                        — ST debt

Usage:
    python probe_xbrl_debt.py                        # default tickers
    python probe_xbrl_debt.py --ticker AIG BNY AKAM  # specific tickers

Output: probe_xbrl.txt
"""

import sys
import os
import time
import argparse
import requests
import json

OUTPUT     = "probe_xbrl.txt"
DELAY_SECS = 0.15   # SEC rate limit: 10 req/sec max
HEADERS    = {"User-Agent": "Your Name your@email.com"}   # ← replace

# Key debt XBRL concepts to check
DEBT_CONCEPTS = [
    "DebtInstrumentInterestRateStatedPercentage",
    "DebtInstrumentInterestRateEffectivePercentage",
    "DebtInstrumentFaceAmount",
    "DebtInstrumentCarryingAmount",
    "DebtInstrumentMaturityDate",
    "DebtInstrumentNameDomain",
    "LongTermDebt",
    "LongTermDebtCurrent",
    "LongTermDebtNoncurrent",
    "ShortTermBorrowings",
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo",
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree",
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour",
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive",
    "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive",
]

# Mix of known-UNKNOWN tickers + some known-good for comparison
DEFAULT_TICKERS = [
    # Known UNKNOWN from scan
    "AIG", "BNY", "AKAM", "BR", "APO", "AXON", "AOS", "BRK-B",
    "BALL", "AEE", "ABNB", "ADM", "AMP", "AMCR",
    # Known working (baseline)
    "MU", "LLY", "AAPL", "NVDA",
]


def get_cik(ticker: str) -> str | None:
    """Get CIK from SEC EDGAR company search."""
    try:
        url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&forms=10-K"
        r   = requests.get(
            f"https://data.sec.gov/submissions/CIK{{}}.json",
            headers=HEADERS, timeout=10
        )
        # Use the ticker→CIK mapping file
        r2 = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=HEADERS, timeout=15
        )
        mapping = r2.json()
        for entry in mapping.values():
            if entry.get("ticker", "").upper() == ticker.upper():
                return str(entry["cik_str"]).zfill(10)
        return None
    except Exception as e:
        return None


_TICKER_CIK_CACHE: dict = {}

def get_cik_fast(ticker: str) -> str | None:
    """Fetch all tickers once and cache."""
    global _TICKER_CIK_CACHE
    if not _TICKER_CIK_CACHE:
        try:
            r = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers=HEADERS, timeout=15
            )
            for entry in r.json().values():
                t = entry.get("ticker", "").upper()
                _TICKER_CIK_CACHE[t] = str(entry["cik_str"]).zfill(10)
        except Exception as e:
            print(f"  CIK fetch error: {e}")
    return _TICKER_CIK_CACHE.get(ticker.upper())


def get_xbrl_facts(cik: str) -> dict | None:
    """Fetch company facts from SEC XBRL API."""
    try:
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        r   = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None


def probe_ticker(ticker: str, out) -> dict:
    def w(s=""):
        print(s)
        out.write(str(s) + "\n")

    result = {
        "ticker":           ticker,
        "cik":              None,
        "has_tranche_rate": False,
        "has_tranche_face": False,
        "has_maturity":     False,
        "has_total_debt":   False,
        "n_rate_entries":   0,
        "n_face_entries":   0,
        "concepts_found":   [],
        "status":           "ok",
    }

    w(f"\n{'='*65}")
    w(f"TICKER: {ticker}")
    w(f"{'='*65}")

    cik = get_cik_fast(ticker)
    if not cik:
        w("  CIK not found")
        result["status"] = "no_cik"
        return result
    result["cik"] = cik
    w(f"  CIK: {cik}")

    facts = get_xbrl_facts(cik)
    if not facts:
        w("  XBRL facts not available")
        result["status"] = "no_xbrl"
        return result

    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    w(f"  Total us-gaap concepts: {len(us_gaap)}")

    # Check each debt concept
    for concept in DEBT_CONCEPTS:
        if concept in us_gaap:
            data    = us_gaap[concept]
            units   = data.get("units", {})
            n_vals  = sum(len(v) for v in units.values())
            result["concepts_found"].append(concept)

            # Get most recent values
            recent_vals = []
            for unit_type, entries in units.items():
                # Filter to 10-K annual filings, get recent ones
                annual = [e for e in entries
                          if e.get("form") in ("10-K", "10-K/A")
                          and e.get("accn")]
                if annual:
                    # Sort by end date descending
                    annual.sort(key=lambda x: x.get("end", ""), reverse=True)
                    recent_vals.append({
                        "unit":  unit_type,
                        "value": annual[0].get("val"),
                        "end":   annual[0].get("end"),
                        "n_total": len(annual),
                    })

            w(f"\n  ✓ {concept}")
            w(f"    Total entries: {n_vals}  |  Units: {list(units.keys())}")
            for rv in recent_vals[:3]:
                w(f"    Most recent (10-K): {rv['end']}  val={rv['value']}  "
                  f"(n={rv['n_total']} annual filings)")

            # Flag key concepts
            if concept == "DebtInstrumentInterestRateStatedPercentage":
                result["has_tranche_rate"] = True
                result["n_rate_entries"]   = n_vals
            elif concept == "DebtInstrumentFaceAmount":
                result["has_tranche_face"] = True
                result["n_face_entries"]   = n_vals
            elif concept == "DebtInstrumentMaturityDate":
                result["has_maturity"]     = True
            elif concept in ("LongTermDebt", "LongTermDebtNoncurrent"):
                result["has_total_debt"]   = True
        else:
            w(f"  ✗ {concept}")

    # Summary
    w(f"\n  SUMMARY:")
    w(f"    Tranche rate (DebtInstrumentInterestRateStatedPercentage): "
      f"{'YES (' + str(result['n_rate_entries']) + ' entries)' if result['has_tranche_rate'] else 'NO'}")
    w(f"    Tranche face (DebtInstrumentFaceAmount): "
      f"{'YES (' + str(result['n_face_entries']) + ' entries)' if result['has_tranche_face'] else 'NO'}")
    w(f"    Maturity date: {'YES' if result['has_maturity'] else 'NO'}")
    w(f"    Total LT debt: {'YES' if result['has_total_debt'] else 'NO'}")

    if result["has_tranche_rate"] and result["has_tranche_face"]:
        w(f"    → FULL TRANCHE DATA AVAILABLE via XBRL ✓✓")
    elif result["has_total_debt"]:
        w(f"    → Total debt only (no per-tranche breakdown)")
    else:
        w(f"    → Limited XBRL debt data")

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", nargs="*", default=None)
    args = parser.parse_args()

    tickers = [t.upper() for t in args.ticker] if args.ticker else DEFAULT_TICKERS

    print(f"Fetching CIK mapping...")
    get_cik_fast(tickers[0])   # warm cache
    print(f"Probing {len(tickers)} tickers → {OUTPUT}\n")

    results = []
    with open(OUTPUT, "w", encoding="utf-8") as out:
        out.write("XBRL DEBT CONCEPTS PROBE\n")
        out.write("=" * 65 + "\n\n")

        for i, ticker in enumerate(tickers, 1):
            print(f"  [{i:>2}/{len(tickers)}] {ticker}", end="", flush=True)
            r = probe_ticker(ticker, out)
            results.append(r)
            rate_ok = "rate✓" if r["has_tranche_rate"] else "rate✗"
            face_ok = "face✓" if r["has_tranche_face"] else "face✗"
            print(f"  {rate_ok}  {face_ok}  concepts={len(r['concepts_found'])}")
            time.sleep(DELAY_SECS)

        # Coverage summary
        out.write("\n\n" + "="*65 + "\n")
        out.write("COVERAGE SUMMARY\n")
        out.write("="*65 + "\n\n")
        ok       = [r for r in results if r["status"] == "ok"]
        full     = [r for r in ok if r["has_tranche_rate"] and r["has_tranche_face"]]
        rate_only= [r for r in ok if r["has_tranche_rate"] and not r["has_tranche_face"]]
        total_only=[r for r in ok if r["has_total_debt"] and not r["has_tranche_rate"]]
        nothing  = [r for r in ok if not r["has_total_debt"] and not r["has_tranche_rate"]]

        out.write(f"{'Ticker':<10} {'Rate':>6} {'Face':>6} "
                  f"{'Mat':>5} {'Total':>7} {'Concepts':>9}\n")
        out.write("-" * 50 + "\n")
        for r in results:
            out.write(
                f"{r['ticker']:<10} "
                f"{'✓' if r['has_tranche_rate'] else '✗':>6} "
                f"{'✓' if r['has_tranche_face'] else '✗':>6} "
                f"{'✓' if r['has_maturity'] else '✗':>5} "
                f"{'✓' if r['has_total_debt'] else '✗':>7} "
                f"{len(r['concepts_found']):>9}\n"
            )

        out.write(f"\nFull tranche data (rate+face): {len(full)}/{len(ok)}")
        out.write(f"\nRate only:                     {len(rate_only)}/{len(ok)}")
        out.write(f"\nTotal debt only:               {len(total_only)}/{len(ok)}")
        out.write(f"\nNo useful debt data:           {len(nothing)}/{len(ok)}\n")

    print(f"\n{'='*50}")
    print(f"  Full tranche (rate+face): {len(full)}/{len(ok)}")
    print(f"  Rate only:                {len(rate_only)}/{len(ok)}")
    print(f"  Total debt only:          {len(total_only)}/{len(ok)}")
    print(f"  No useful debt data:      {len(nothing)}/{len(ok)}")
    print(f"\n  Output → {OUTPUT}")


if __name__ == "__main__":
    main()
