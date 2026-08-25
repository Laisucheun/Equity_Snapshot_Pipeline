"""
query_cache.py — Inspect the filing cache (filing_cache.db)

Run from your project root:
    python query_cache.py              # show all cached tickers
    python query_cache.py AAPL         # show detail for one ticker
    python query_cache.py --clear AAPL # force re-fetch for a ticker
"""

import sys
import os
import json
import sqlite3

DB   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filing_cache.db")
XBRL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xbrl_cache")


def all_tickers():
    with sqlite3.connect(DB) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT
                f.ticker,
                f.filing_date,
                f.form_type,
                COALESCE(n.fetched_date, '—')  AS narrative_cached,
                COALESCE(n.source, '—')         AS narrative_source,
                COALESCE(p.fetch_date, '—')     AS prices_cached
            FROM filings f
            LEFT JOIN narratives n ON n.ticker = f.ticker
                                   AND n.filing_date = f.filing_date
            LEFT JOIN (
                SELECT ticker, MAX(fetch_date) as fetch_date FROM prices GROUP BY ticker
            ) p ON p.ticker = f.ticker
            ORDER BY f.ticker
        """).fetchall()

    print(f"\n{'Ticker':<8}  {'Filing date':<14}  {'Form':<8}  "
          f"{'Narrative cached':<18}  {'Source':<6}  {'Prices cached'}")
    print("─" * 80)
    for r in rows:
        print(f"{r['ticker']:<8}  {r['filing_date']:<14}  {r['form_type']:<8}  "
              f"{r['narrative_cached']:<18}  {r['narrative_source']:<6}  {r['prices_cached']}")
    print(f"\n{len(rows)} tickers in cache.  DB: {DB}")

    # Parquet file count
    if os.path.isdir(XBRL):
        n_pq = len([f for f in os.listdir(XBRL) if f.endswith(".parquet")])
        print(f"XBRL parquet files: {n_pq}  ({XBRL})")


def ticker_detail(ticker: str):
    ticker = ticker.upper()
    with sqlite3.connect(DB) as con:
        con.row_factory = sqlite3.Row

        filing = con.execute(
            "SELECT * FROM filings WHERE ticker = ? ORDER BY filing_date DESC LIMIT 1",
            (ticker,)
        ).fetchone()

        narrative = con.execute(
            "SELECT source, fetched_date, filing_date_8k, "
            "LENGTH(earnings_text) as chars FROM narratives WHERE ticker = ?",
            (ticker,)
        ).fetchone()

        prices = con.execute(
            "SELECT fetch_date, periods_json, fy_prices_json "
            "FROM prices WHERE ticker = ? ORDER BY fetch_date DESC LIMIT 1",
            (ticker,)
        ).fetchone()

    print(f"\n── {ticker} ─────────────────────────────────────────")

    if filing:
        print(f"  Filing:    {filing['form_type']}  {filing['filing_date']}")
        # List parquet files
        if os.path.isdir(XBRL):
            pq = [f for f in os.listdir(XBRL)
                  if f.startswith(f"{ticker}_") and f.endswith(".parquet")]
            for f in sorted(pq):
                path = os.path.join(XBRL, f)
                kb = os.path.getsize(path) // 1024
                print(f"  Parquet:   {f}  ({kb} KB)")
    else:
        print("  Filing:    not cached")

    if narrative:
        print(f"  Narrative: {narrative['source']}  fetched {narrative['fetched_date']}  "
              f"8-K date {narrative['filing_date_8k'] or '—'}  "
              f"{narrative['chars']:,} chars")
    else:
        print("  Narrative: not cached")

    if prices:
        periods = json.loads(prices["periods_json"] or "[]")
        plist   = json.loads(prices["fy_prices_json"] or "[]")
        print(f"  Prices:    fetched {prices['fetch_date']}")
        for period, price in zip(periods, plist):
            p_str = f"${price:.2f}" if price else "N/A"
            print(f"             {period[:10]}  {p_str}")
    else:
        print("  Prices:    not cached")


def clear_ticker(ticker: str):
    from filing_cache import FilingCache
    cache = FilingCache()
    cache.clear(ticker.upper())
    print(f"Cleared cache for {ticker.upper()}. Next run will re-fetch from EDGAR.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        all_tickers()
    elif len(args) == 2 and args[0] == "--clear":
        clear_ticker(args[1])
    elif len(args) == 1 and not args[0].startswith("--"):
        ticker_detail(args[0])
    else:
        print("Usage:")
        print("  python query_cache.py              # all tickers")
        print("  python query_cache.py AAPL         # detail for one ticker")
        print("  python query_cache.py --clear AAPL # force re-fetch")
