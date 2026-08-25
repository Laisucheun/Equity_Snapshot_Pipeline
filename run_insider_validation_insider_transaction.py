"""
run_insider_validation.py — Batch insider activity validation across S&P 500 + Nasdaq 100 + Dow 30

For each ticker fetches open-market insider Form 4 transactions (last 90 days):
  - Buy count / sell count (distinct insiders)
  - Buy value / sell value / net value
  - Cluster buying flag
  - Top transaction (largest by value) for spot-checking

Output: insider_summary.txt  (same format convention as debt_summary.txt, one row per ticker)

Usage:
    python run_insider_validation.py
    python run_insider_validation.py --tickers NVDA NKE UNH   # subset
    python run_insider_validation.py --resume                  # skip already-done tickers
    python run_insider_validation.py --lookback 60              # custom window (default 90)

Runtime: ~25-40 min for full universe (SEC rate limit: 10 req/sec — each ticker
requires N filing fetches + N filing.obj() parses, so this is slower per-ticker
than the single-call debt validation; consider --tickers for a quick subset test
before a full run)
"""

import os
import sys
import time
import argparse
import logging

logging.basicConfig(
    level=logging.WARNING,           # suppress INFO noise during batch
    format="%(levelname)s %(name)s %(message)s",
)

IDENTITY = "Your Name your@email.com"   # ← replace before running
OUTPUT   = "insider_summary.txt"
DELAY    = 0.15    # seconds between tickers — stays under SEC 10 req/sec limit

# ── Universe ──────────────────────────────────────────────────────────────────
# S&P 500 + Nasdaq 100 + Dow 30, deduplicated.
# Last updated: Jun 2026. Edit as needed.
# (Identical to run_debt_validation.py's universe — kept in sync manually.)

_SP500 = [
    "MMM","AOS","ABT","ABBV","ACN","ADBE","AMD","AES","AFL","A","APD","ABNB",
    "AKAM","ALB","ARE","ALGN","ALLE","LNT","ALL","GOOGL","GOOG","MO","AMZN",
    "AMCR","AEE","AAL","AEP","AXP","AIG","AMT","AWK","AMP","AME","AMGN","APH",
    "ADI","ANSS","AON","APA","APO","AAPL","AMAT","APTV","ACGL","ADM","ANET",
    "AJG","AIZ","T","ATO","ADSK","ADP","AZO","AVB","AVY","AXON","BKR","BALL",
    "BAC","BK","BBWI","BAX","BDX","BRK-B","BBY","BIO","TECH","BIIB","BLK",
    "BX","BA","BCR","BMY","AVGO","BR","BRO","BF-B","BLDR","BG","CDNS","CZR",
    "CPT","CPB","COF","CAH","KMX","CCL","CARR","CTLT","CAT","CBOE","CBRE",
    "CDW","CE","COR","CNC","CNP","CF","CHRW","CRL","SCHW","CHTR","CVX","CMG",
    "CB","CHD","CI","CINF","CTAS","CSCO","C","CFG","CLX","CME","CMS","KO",
    "CTSH","CL","CMCSA","CAG","COP","ED","STZ","CEG","COO","CPRT","GLW","CPAY",
    "CTVA","CSGP","COST","CTRA","CRWD","CCI","CSX","CMI","CVS","DHR","DRI",
    "DVA","DAY","DECK","DE","DAL","DVN","DXCM","FANG","DLR","DFS","DG","DLTR",
    "D","DPZ","DOV","DOW","DHI","DTE","DUK","DD","EMN","ETN","EBAY","ECL",
    "EIX","EW","EA","ELV","LLY","EMR","ENPH","ETR","EOG","EPAM","EQT","EFX",
    "EQIX","EQR","ESS","EL","ETSY","EG","EVRG","ES","EXC","EXPE","EXPD","EXR",
    "XOM","FFIV","FDS","FICO","FAST","FRT","FDX","FIS","FITB","FSLR","FE",
    "FI","FLT","FMC","F","FTNT","FTV","FOXA","FOX","BEN","FCX","GRMN","IT",
    "GE","GEHC","GEV","GEN","GNRC","GD","GIS","GM","GPC","GILD","GPN","GL",
    "GDDY","GS","HAL","HIG","HAS","HCA","DOC","HSIC","HSY","HES","HPE","HLT",
    "HOLX","HD","HON","HRL","HST","HWM","HPQ","HUBB","HUM","HBAN","HII","IBM",
    "IEX","IDXX","ITW","INCY","IR","PODD","INTC","ICE","IFF","IP","IPG","INTU",
    "ISRG","IVZ","INVH","IQV","IRM","JBHT","JBL","JKHY","J","JNJ","JCI","JPM",
    "JNPR","K","KVUE","KDP","KEY","KEYS","KMB","KIM","KMI","KLAC","KHC","KR",
    "LHX","LH","LRCX","LW","LVS","LDOS","LEN","LNC","LIN","LYV","LKQ","LMT",
    "L","LOW","LULU","LYB","MTB","MRO","MPC","MKTX","MAR","MMC","MLM","MAS",
    "MA","MTCH","MKC","MCD","MCK","MDT","MRK","META","MET","MTD","MGM","MCHP",
    "MU","MSFT","MAA","MRNA","MHK","MOH","TAP","MDLZ","MPWR","MNST","MCO",
    "MS","MOS","MSI","MSCI","NDAQ","NTAP","NFLX","NWS","NWSA","NEE","NKE",
    "NI","NDSN","NSC","NTRS","NOC","NCLH","NRG","NUE","NVDA","NVR","NXPI",
    "ORLY","OXY","ODFL","OMC","ON","OKE","ORCL","OTIS","PCAR","PKG","PANW",
    "PARA","PH","PAYX","PAYC","PYPL","PNR","PEP","PFE","PCG","PM","PSX","PNW",
    "PNC","POOL","PPG","PPL","PFG","PG","PGR","PLD","PRU","PEG","PTVE","PTC",
    "PSA","PHM","QRVO","PWR","QCOM","DGX","RL","RJF","RTX","O","REG","REGN",
    "RF","RSG","RMD","RVTY","ROK","ROL","ROP","ROST","RCL","SPGI","CRM","SBAC",
    "SLB","STX","SRE","NOW","SHW","SPG","SWKS","SJM","SW","SNA","SOLV","SO",
    "LUV","SWK","SBUX","STT","STLD","STE","SYK","SMCI","SYF","SNPS","SYY",
    "TMUS","TROW","TTWO","TPR","TRGP","TGT","TEL","TDY","TFX","TER","TSLA",
    "TXN","TXT","TMO","TJX","TSCO","TT","TDG","TRV","TRMB","TFC","TYL","TSN",
    "USB","UBER","UDR","UHS","UNP","UAL","UPS","URI","UNH","UHS","VLO","VTR",
    "VLTO","VRSN","VRSK","VZ","VRTX","VTRS","VICI","V","VMC","WRB","GWW",
    "WAB","WBA","WMT","DIS","WBD","WM","WAT","WEC","WFC","WELL","WST","WDC",
    "WRK","WY","WHR","WMB","WTW","WYNN","XEL","XYL","YUM","ZBRA","ZBH","ZTS",
]

_NDX100 = [
    "ADBE","AMD","ABNB","GOOGL","GOOG","AMZN","AMGN","ADI","ANSS","AAPL",
    "AMAT","ARM","ASML","AZN","TEAM","ADSK","ADP","AXON","BIIB","BKNG",
    "AVGO","CDNS","CDW","CHTR","CTAS","CSCO","CCEP","CTSH","CMCSA","CEG",
    "CPRT","CSGP","COST","CRWD","CSX","DDOG","DXCM","FANG","DLTR","EA",
    "EXC","FAST","FTNT","GEHC","GILD","GFS","HON","IDXX","ILMN","INTC",
    "INTU","ISRG","KDP","KLAC","KHC","LRCX","LIN","LULU","MAR","MRVL",
    "MELI","META","MCHP","MU","MSFT","MRNA","MDLZ","MDB","MNST","NFLX",
    "NVDA","NXPI","ORLY","ON","PCAR","PANW","PAYX","PYPL","PDD","QCOM",
    "REGN","ROP","ROST","CRM","SBUX","SMCI","SNPS","TTWO","TMUS","TSLA",
    "TXN","TTD","VRSK","VRTX","WBA","WDAY","XEL","ZS","ZM",
]

_DOW30 = [
    "AMZN","AMGN","AAPL","BA","CAT","CSCO","CVX","GS","HD","HON",
    "IBM","INTC","JNJ","JPM","KO","MCD","MMM","MRK","MSFT","NKE",
    "PG","CRM","TRV","UNH","VZ","V","WMT","DIS","DOW","AXP",
]

# Deduplicate, preserve rough market-cap order (S&P first)
_SEEN = set()
TICKERS = []
for t in _SP500 + _NDX100 + _DOW30:
    if t not in _SEEN:
        _SEEN.add(t)
        TICKERS.append(t)


# ── Core validation logic ─────────────────────────────────────────────────────

def validate_ticker(ticker: str, lookback_days: int) -> dict:
    """
    Run insider transaction fetch for one ticker. Returns summary dict.
    Never raises — all errors captured in 'error' key.
    """
    result = {
        "ticker":         ticker,
        "buy_count":      None,
        "sell_count":     None,
        "buy_value":      None,
        "sell_value":     None,
        "net_value":      None,
        "cluster_buying": None,
        "n_transactions": None,
        "top_txn":        None,   # largest single transaction by value, for spot-checking
        "error":          None,
    }
    try:
        from insider_transactions import InsiderTransactionLoader
        import edgar
        edgar.set_identity(IDENTITY)

        loader = InsiderTransactionLoader()
        data = loader.fetch(ticker, lookback_days=lookback_days)

        if data:
            result["buy_count"]      = data.get("buy_count")
            result["sell_count"]     = data.get("sell_count")
            result["buy_value"]      = data.get("buy_value")
            result["sell_value"]     = data.get("sell_value")
            result["net_value"]      = data.get("net_value")
            result["cluster_buying"] = data.get("cluster_buying")
            txns = data.get("transactions", [])
            result["n_transactions"] = len(txns)
            if txns:
                result["top_txn"] = max(txns, key=lambda t: t["value"])

    except Exception as e:
        result["error"] = str(e)

    return result


def fmt_row(r: dict) -> str:
    ticker     = r["ticker"]
    buy_str    = f"{r['buy_count']}"  if r["buy_count"]  is not None else "—"
    sell_str   = f"{r['sell_count']}" if r["sell_count"] is not None else "—"
    buy_v_str  = f"${r['buy_value']:,.0f}"  if r["buy_value"]  else "$0"
    sell_v_str = f"${r['sell_value']:,.0f}" if r["sell_value"] else "$0"
    net_v      = r["net_value"]
    net_str    = (
        f"{'+' if net_v >= 0 else '-'}${abs(net_v):,.0f}"
        if net_v is not None else "—"
    )
    cluster_str = "CLUSTER" if r["cluster_buying"] else ""
    n_str       = f"{r['n_transactions']}" if r["n_transactions"] is not None else "0"
    err_str     = f"  ERROR: {r['error']}" if r["error"] else ""
    return (
        f"{ticker:<10}  buyers={buy_str:>3}  sellers={sell_str:>3}  "
        f"buy={buy_v_str:>14}  sell={sell_v_str:>14}  net={net_str:>16}  "
        f"n={n_str:>3}  {cluster_str:<8}{err_str}"
    )


def fmt_top_txn(t: dict) -> str:
    name  = (t.get("insider_name", "") or "")[:30]
    pos   = (t.get("position", "") or "")[:35]
    plan  = " [10b5-1]" if t.get("is_10b5_1") else ""
    return (
        f"       top: {t.get('date',''):<12} {t.get('transaction',''):<4} "
        f"{name:<30} {pos:<35} {t.get('shares',0):>10,} @ "
        f"${t.get('price',0):.2f}  (${t.get('value',0):,.0f}){plan}"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None,
                        help="Run on specific tickers only")
    parser.add_argument("--resume", action="store_true",
                        help="Skip tickers already in output file")
    parser.add_argument("--lookback", type=int, default=90,
                        help="Lookback window in days (default: 90)")
    args = parser.parse_args()

    tickers = [t.upper() for t in args.tickers] if args.tickers else TICKERS

    # Resume: read already-completed tickers from existing output
    done = set()
    if args.resume and os.path.exists(OUTPUT):
        with open(OUTPUT, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0] in _SEEN:
                    done.add(parts[0])
        print(f"Resuming — {len(done)} tickers already done, {len(tickers)-len(done)} remaining")
        tickers = [t for t in tickers if t not in done]

    header = (
        f"\n{'='*120}\n"
        f"{'Ticker':<10}  {'Buyers':>10}  {'Sellers':>10}  {'Buy $':>16}  "
        f"{'Sell $':>16}  {'Net $':>18}  {'N':>5}  Flag\n"
        f"{'='*120}"
    )

    mode = "a" if (args.resume and done) else "w"
    with open(OUTPUT, mode, encoding="utf-8") as out:
        if mode == "w":
            out.write(f"Insider Activity Validation — {len(tickers)} tickers "
                      f"(lookback: {args.lookback} days)\n")
            out.write(header + "\n")
        print(header)

        errors      = []
        no_data     = []
        has_data    = 0
        n_clusters  = 0

        for i, ticker in enumerate(tickers, 1):
            print(f"  [{i:>3}/{len(tickers)}] {ticker:<6}", end="", flush=True)

            r = validate_ticker(ticker, args.lookback)

            row = fmt_row(r)
            print(f"  {row}")
            out.write(row + "\n")

            if r["top_txn"]:
                line = fmt_top_txn(r["top_txn"])
                print(line)
                out.write(line + "\n")

            if r["error"]:
                errors.append(ticker)
            elif not r["n_transactions"]:
                no_data.append(ticker)
            else:
                has_data += 1
                if r["cluster_buying"]:
                    n_clusters += 1

            out.flush()
            time.sleep(DELAY)

        # Summary
        summary = (
            f"\n{'='*120}\n"
            f"SUMMARY\n"
            f"  Total tickers:           {len(tickers)}\n"
            f"  Insider activity found:  {has_data}\n"
            f"  No activity in window:   {len(no_data)}\n"
            f"  Cluster buying signals:  {n_clusters}\n"
            f"  Errors:                  {len(errors)}\n"
            + f"\nErrors:       {', '.join(errors[:20])}"
            + (f" +{len(errors)-20} more" if len(errors) > 20 else "")
            + f"\n{'='*120}\n"
        )
        print(summary)
        out.write(summary)

    print(f"\nOutput → {OUTPUT}")


if __name__ == "__main__":
    main()
