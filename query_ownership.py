"""
query_ownership.py — Inspect ownership_history.db

Run from your project root:
    python query_ownership.py                   # all tickers summary
    python query_ownership.py AAPL              # history for one ticker
    python query_ownership.py --holder Vanguard # all tickers where Vanguard appears
    python query_ownership.py --top             # biggest institutional % drops
"""

import sys
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ownership_history.db")


def all_tickers():
    with sqlite3.connect(DB) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT
                ticker,
                COUNT(*)            AS snapshots,
                MIN(run_date)       AS first_run,
                MAX(run_date)       AS last_run,
                ROUND(
                    (SELECT institutional_pct FROM snapshots s2
                     WHERE s2.ticker = s.ticker
                     ORDER BY run_date DESC LIMIT 1) * 100, 1
                )                   AS inst_pct_latest
            FROM snapshots s
            GROUP BY ticker
            ORDER BY ticker
        """).fetchall()

    print(f"\n{'Ticker':<10} {'Snapshots':>10} {'First run':<14} {'Last run':<14} {'Inst % (latest)':>16}")
    print("─" * 68)
    for r in rows:
        inst = f"{r['inst_pct_latest']}%" if r['inst_pct_latest'] else "N/A"
        print(f"{r['ticker']:<10} {r['snapshots']:>10} {r['first_run']:<14} "
              f"{r['last_run']:<14} {inst:>16}")
    print(f"\n{len(rows)} tickers  |  DB: {DB}")


def ticker_history(ticker: str):
    ticker = ticker.upper()
    with sqlite3.connect(DB) as con:
        con.row_factory = sqlite3.Row

        snaps = con.execute("""
            SELECT run_date,
                   ROUND(institutional_pct * 100, 2) AS inst_pct,
                   ROUND(insider_pct * 100, 2)       AS ins_pct,
                   ROUND(top10_concentration_pct * 100, 2) AS top10_pct
            FROM snapshots WHERE ticker = ?
            ORDER BY run_date DESC
        """, (ticker,)).fetchall()

        if not snaps:
            print(f"\n{ticker}: not in DB")
            return

        print(f"\n{ticker} — snapshot history ({len(snaps)} runs)")
        print(f"{'Date':<14} {'Inst %':>8} {'Insider %':>10} {'Top-10 %':>10}")
        print("─" * 46)
        for r in snaps:
            inst  = f"{r['inst_pct']}%"  if r['inst_pct']  is not None else "N/A"
            ins   = f"{r['ins_pct']}%"   if r['ins_pct']   is not None else "N/A"
            top10 = f"{r['top10_pct']}%" if r['top10_pct'] is not None else "N/A"
            print(f"{r['run_date']:<14} {inst:>8} {ins:>10} {top10:>10}")

        # Most recent top-10 holders
        latest_date = snaps[0]["run_date"]
        holders = con.execute("""
            SELECT rank, name,
                   ROUND(pct * 100, 2) AS pct_pct,
                   shares
            FROM holders
            WHERE ticker = ? AND run_date = ?
            ORDER BY rank
        """, (ticker, latest_date)).fetchall()

        if holders:
            print(f"\n{ticker} — top holders as of {latest_date}")
            print(f"{'#':>3}  {'Holder':<45} {'% Out':>7} {'Shares':>16}")
            print("─" * 75)
            for h in holders:
                pct = f"{h['pct_pct']}%" if h['pct_pct'] is not None else "N/A"
                sh  = f"{h['shares']:,}" if h['shares']  is not None else "N/A"
                print(f"{h['rank']:>3}  {(h['name'] or '—'):<45} {pct:>7} {sh:>16}")


def holder_search(name_fragment: str):
    with sqlite3.connect(DB) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT h.ticker, h.run_date, h.rank,
                   h.name,
                   ROUND(h.pct * 100, 2) AS pct_pct,
                   h.shares
            FROM holders h
            -- Only show most recent snapshot per ticker
            JOIN (
                SELECT ticker, MAX(run_date) AS max_date
                FROM snapshots GROUP BY ticker
            ) latest ON latest.ticker = h.ticker
                     AND latest.max_date = h.run_date
            WHERE h.name LIKE ?
            ORDER BY h.pct DESC NULLS LAST
        """, (f"%{name_fragment}%",)).fetchall()

    if not rows:
        print(f"\nNo holders matching '{name_fragment}' found.")
        return

    print(f"\nHolders matching '{name_fragment}' (most recent snapshot per ticker):")
    print(f"{'Ticker':<10} {'Date':<14} {'#':>3}  {'Holder':<40} {'% Out':>7} {'Shares':>16}")
    print("─" * 95)
    for r in rows:
        pct = f"{r['pct_pct']}%" if r['pct_pct'] is not None else "N/A"
        sh  = f"{r['shares']:,}" if r['shares']  is not None else "N/A"
        print(f"{r['ticker']:<10} {r['run_date']:<14} {r['rank']:>3}  "
              f"{(r['name'] or '—'):<40} {pct:>7} {sh:>16}")
    print(f"\n{len(rows)} positions found.")


def top_movers():
    """Tickers with biggest institutional % change between first and last snapshot."""
    with sqlite3.connect(DB) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT
                a.ticker,
                ROUND(a.institutional_pct * 100, 2) AS inst_latest,
                ROUND(b.institutional_pct * 100, 2) AS inst_first,
                ROUND((a.institutional_pct - b.institutional_pct) * 100, 2) AS delta_pp,
                a.run_date AS latest_date,
                b.run_date AS first_date
            FROM snapshots a
            JOIN snapshots b ON a.ticker = b.ticker
            WHERE a.run_date = (SELECT MAX(run_date) FROM snapshots WHERE ticker = a.ticker)
              AND b.run_date = (SELECT MIN(run_date) FROM snapshots WHERE ticker = b.ticker)
              AND a.run_date != b.run_date
            ORDER BY delta_pp ASC
        """).fetchall()

    if not rows:
        print("\nNot enough history for delta analysis (need ≥2 runs per ticker).")
        return

    print(f"\nInstitutional ownership change (first → last snapshot):")
    print(f"{'Ticker':<10} {'First date':<14} {'First %':>8} {'Last date':<14} "
          f"{'Latest %':>9} {'Δ pp':>8}")
    print("─" * 70)
    for r in rows:
        first = f"{r['inst_first']}%" if r['inst_first'] is not None else "N/A"
        last  = f"{r['inst_latest']}%" if r['inst_latest'] is not None else "N/A"
        delta = f"{r['delta_pp']:+.1f}pp" if r['delta_pp'] is not None else "N/A"
        print(f"{r['ticker']:<10} {r['first_date']:<14} {first:>8} "
              f"{r['latest_date']:<14} {last:>9} {delta:>8}")


if __name__ == "__main__":
    if not os.path.exists(DB):
        print(f"DB not found: {DB}")
        print("Run the pipeline at least once to create it.")
        sys.exit(0)

    args = sys.argv[1:]
    if not args:
        all_tickers()
    elif len(args) == 2 and args[0] == "--holder":
        holder_search(args[1])
    elif len(args) == 1 and args[0] == "--top":
        top_movers()
    elif len(args) == 1 and not args[0].startswith("--"):
        ticker_history(args[0])
    else:
        print("Usage:")
        print("  python query_ownership.py                   # all tickers")
        print("  python query_ownership.py AAPL              # ticker history")
        print("  python query_ownership.py --holder Vanguard # holder search")
        print("  python query_ownership.py --top             # biggest movers")
