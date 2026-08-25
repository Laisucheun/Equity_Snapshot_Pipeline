"""
run_batch_validation_50.py — Non-financials validation batch (50 tickers, 9 sectors)

Usage:
    python runners/main.py            # runs the default MAG7 batch
    python runners/main.py NVDA       # runs a single-ticker report (sector auto-detected)

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
from core.orchestrator import EquityAnalystOrchestrator
from utils.tune_tone import _infer_sector

# ── Config ────────────────────────────────────────────────────────────────────

EDGAR_IDENTITY = "Your Name your@email.com"     # ← required by SEC EDGAR
AUTHOR         = "ScL"                   # ← printed on watermark
output_dir     = os.path.join(os.path.expanduser("~"), "Desktop", "EquityReports")
os.makedirs(output_dir, exist_ok=True)
OUTPUT_DIR     = output_dir
DELAY_SECS     = 8                              # pause between runs (SEC rate limit)

# ── Ticker universe — MAG7 default batch ─────────────────────────────────────
#
# Sector routing:
#   "Energy"      → Operating Cost Ratio, Debt/Capital; Z-score suppressed
#   anything else → full general ratio set
#
TICKERS = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "GOOGL": "Communication Services",
    "AMZN": "Consumer Discretionary",
    "NVDA": "Technology",
    "META": "Communication Services",
    "TSLA": "Consumer Discretionary",
}

# ── Run ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Equity Brief Pipeline")
    parser.add_argument("ticker", nargs="?", default=None,
                        help="Single ticker to run (omit to run the full MAG7 batch)")
    parser.add_argument("--no-pdf",    action="store_true",
                        help="Skip PDF rendering — console diagnostics only")
    parser.add_argument("--no-stitch", action="store_true",
                        help="Skip combined PDF stitching")
    args, _ = parser.parse_known_args()

    orch = EquityAnalystOrchestrator(edgar_identity=EDGAR_IDENTITY)

    if args.ticker:
        ticker = args.ticker.upper()
        sector = _infer_sector(ticker)
        if not sector or sector == "Unknown":
            sector = "General"
        run_list = [(ticker, sector)]
    else:
        run_list = list(TICKERS.items())

    results   = []
    n         = len(run_list)

    print(f"\nValidation batch: {n} tickers  |  output → {OUTPUT_DIR}\n")
    print(f"{'#':>3}  {'Ticker':<8}  {'Sector':<28}  Status")
    print("─" * 65)

    for idx, (ticker, sector) in enumerate(run_list, 1):
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