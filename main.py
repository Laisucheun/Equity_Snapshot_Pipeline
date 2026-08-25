"""
run_batch_validation_50.py — Non-financials validation batch (50 tickers, 9 sectors)

Usage:
    python run_batch_validation_50.py

Financials sector (JPM, BAC, GS, WFC) excluded — handled by a separate swarm.
Failed tickers are logged and skipped — the batch continues regardless.

── Stress-test coverage ─────────────────────────────────────────────────────────
Negative equity              ORCL, SBUX, KO, MCD (existing), PG (existing)
Operating losses / sign flip INTC, PFE, DIS, TGT
D&A derivation (MSFT-style)  DIS (content amortisation), AMZN, CRM
Extreme multiples            LLY (P/E >100×), NVDA (existing)
Franchise / no COGS          SBUX, MCD (existing), CMG
High D/E flag                VZ, ABBV, CMCSA, LOW
Commodity cycle              NEM, SLB, MPC, EOG, COP
Operating leverage distortion INTC (loss→loss), TSLA (volatile), LLY (GLP-1 ramp)
Goodwill / intangibles heavy CRM, ABBV, RTX, TMO
Real operating leverage      CMG, LLY, NVDA (existing)
Altman Z in new sectors      NEE (Utilities), LIN (Materials), VZ (Comms)
────────────────────────────────────────────────────────────────────────────────
"""

import os
import time
import traceback
from orchestrator import EquityAnalystOrchestrator

# ── Config ────────────────────────────────────────────────────────────────────

EDGAR_IDENTITY = "Your Name your@email.com"     # ← required by SEC EDGAR
AUTHOR         = "ScL"                   # ← printed on watermark
OUTPUT_DIR     = r"C:\Users\laisu\Desktop\Reports"
DELAY_SECS     = 8                              # pause between runs (SEC rate limit)

# ── Ticker universe — 50 tickers, no Financials ──────────────────────────────
#
# Sector routing:
#   "Energy"      → Operating Cost Ratio, Debt/Capital; Z-score suppressed
#   anything else → full general ratio set
#
TICKERS = [
    # ── Technology ────────────────────────────────────────────────────────────
    # ("AAPL",  "Technology"),             # Sep FY; conservative guidance; consistent beater
    # ("MSFT",  "Technology"),# Jun FY; cloud/AI; GM guidance working
    # ("NOW",   "Technology"),  
    # ("NVDA",  "Technology"),             # Jan FY; extreme beats; AI supercycle
    # ("MU",    "Technology"),
    # ("BNY",    "General"),                          # Aug FY; memory cycle; 3 beats validated
    # ("CRM",   "Technology"),             # Jan FY; goodwill-heavy; SaaS
    # ("FISV",  "Financial Technology"),   # Dec FY; fintech routing; Altman Z distress

    # # ── Healthcare ────────────────────────────────────────────────────────────
    # ("LLY",   "Healthcare"),             # Dec FY; GLP-1 ramp; premium valuation
    # ("UNH",   "Healthcare"),             # Dec FY; managed care; Altman Z grey zone
    # ("ABBV",  "Healthcare"),             # Dec FY; Humira cliff; high D/E
    # ("NVCT",   "Healthcare"),
    # ("MRK",   "Healthcare"),             # Dec FY; Keytruda; stable margins

    # # ── Consumer Discretionary ────────────────────────────────────────────────
    # ("AMZN",  "Consumer Discretionary"), # Dec FY; retail + AWS; complex structure
    # ("NKE",   "Consumer Discretionary"), # May FY; revenue decline; turnaround story
    ("LULU",   "Consumer Discretionary"),
    # ("MCD",   "Consumer Discretionary"), # Dec FY; franchise; gross margin N/A edge case
    ("WEN",   "Consumer Discretionary"), # Dec FY; D/E 29x; Altman Z distress

    # # ── Consumer Staples / Industrials / Energy ───────────────────────────────
    ("WMT",   "Consumer Staples"),       # Jan FY; massive scale; thin margins
    # ("KO",    "Consumer Staples"),       # Dec FY; negative equity (buybacks); dividend
    # ("LMT",   "Industrials"),            # Dec FY; defense; cost-plus contracts
    # ("CAT",   "Industrials"),            # Dec FY; cyclical; margin compression flags
    # ('SPCX', 'Industrial'),
    # # ── Communication / Utilities ─────────────────────────────────────────────
    # ("NFLX",  "Communication Services"), # Dec FY; streaming; genuine op leverage
    # ("NEE",   "Utilities"),              # Dec FY; Altman Z test; FCF deterioration
]

# ── Run ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Equity Brief Pipeline")
    parser.add_argument("--no-pdf",    action="store_true",
                        help="Skip PDF rendering — console diagnostics only")
    parser.add_argument("--no-stitch", action="store_true",
                        help="Skip combined PDF stitching")
    args, _ = parser.parse_known_args()

    orch = EquityAnalystOrchestrator(edgar_identity=EDGAR_IDENTITY)

    results   = []
    n         = len(TICKERS)

    print(f"\nValidation batch: {n} tickers  |  output → {OUTPUT_DIR}\n")
    print(f"{'#':>3}  {'Ticker':<8}  {'Sector':<28}  Status")
    print("─" * 65)

    for idx, (ticker, sector) in enumerate(TICKERS, 1):
        try:
            path = orch.run(
                ticker,
                sector     = sector,
                author     = AUTHOR,
                output_dir = OUTPUT_DIR,
                skip_render = args.no_pdf,
            )
            label = "✓  diagnostics only" if args.no_pdf else "✓  saved"
            results.append((ticker, "OK", path or ""))
            print(f"{idx:>3}  {ticker:<8}  {sector:<28}  {label}")

        except Exception as e:
            msg = str(e)[:80]
            results.append((ticker, "FAILED", msg))
            print(f"{idx:>3}  {ticker:<8}  {sector:<28}  ✗  {msg}")
            traceback.print_exc()

        if idx < n:
            time.sleep(DELAY_SECS)

    # ── Summary ───────────────────────────────────────────────────────────────
    ok     = [r for r in results if r[1] == "OK"]
    failed = [r for r in results if r[1] == "FAILED"]

    print("\n" + "═" * 65)
    print(f"  Done  —  {len(ok)}/{n} succeeded")
    if failed:
        print(f"\n  Failed tickers ({len(failed)}):")
        for ticker, _, msg in failed:
            print(f"    {ticker:<8}  {msg}")
    print("═" * 65 + "\n")

    # ── Stitch successful PDFs ────────────────────────────────────────────────
    if not args.no_pdf and not args.no_stitch and len(ok) > 1:
        import datetime
        ok_paths    = [path for _, _, path in ok]
        date_str    = datetime.date.today().isoformat()
        stitch_name = f"Validation_50_Combined_{date_str}.pdf"
        stitch_path = os.path.join(OUTPUT_DIR, stitch_name)
        try:
            EquityAnalystOrchestrator.stitch_pdfs(ok_paths, stitch_path)
            print(f"  Combined PDF → {stitch_path}")
        except ImportError as e:
            print(f"  Stitch skipped: {e}")


if __name__ == "__main__":
    main()