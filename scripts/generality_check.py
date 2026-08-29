"""
generality_check.py — universe sweep for the split-contamination and
interest-expense-contradiction guards.

Runs data-only (no PDF, no guidance/insider/peer fetching) across every
ticker in ratings.csv and reports, per ticker:

    split_detected          did the share-count ratio break >3x / <0.33x
                            between adjacent periods, with the older side
                            sourced from as-filed XBRL
    periods_suppressed      which fiscal years had per-share/EV metrics
                            suppressed as a result
    implied_ratio           the split factor implied by that ratio break
    interest_contradiction  interest expense resolved to 0/None while
                            total debt > 0 in the most recent period
    peers_dropped           reserved; see NOTE below

The point is to confirm the guards are general — that they fire on the
share-count and debt/interest data alone, for any filer, rather than on a
hand-maintained list of tickers.

NOTE on peers_dropped: peer-set validation is not implemented in this
pipeline, so this column is emitted empty rather than fabricated. It is
kept in the schema so the column set matches the agreed report format.

Usage
-----
    python scripts/generality_check.py                  # full ratings.csv
    python scripts/generality_check.py --tickers NVDA AVGO WMT
    python scripts/generality_check.py --limit 50 --resume
"""

import argparse
import csv
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agents import (                                    # noqa: E402
    _resolve_diluted_shares,
    _resolve_share_anomalies,
    _split_boundary_info,
    _safe_val,
)
from core.data_layer import CompanyFinancialProfile          # noqa: E402
from data.facts_processor import (                           # noqa: E402
    FactsDataProcessor, set_identity as _facts_set_identity,
)
from market.peer_comparator import PeerComparisonLoader      # noqa: E402

# One loader for the whole sweep so the session peer-validation cache and the
# same-day metrics cache are shared across every ticker.
_PEER_LOADER = PeerComparisonLoader()

_ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RATINGS    = os.path.join(_ROOT, "ratings.csv")
_DEFAULT_OUT = os.path.join(_ROOT, "scripts", "generality_check_report.csv")

_COLUMNS = ["ticker", "split_detected", "classification", "periods_corrected",
            "periods_suppressed", "implied_ratio", "interest_contradiction",
            "gm_drop_bps", "gm_flag_at_500", "gm_flag_at_300",
            "peers_derived", "peers_valid", "peers_dropped",
            "dropped_names", "peer_basis", "error"]


def _load_universe() -> list:
    with open(_RATINGS, newline="", encoding="utf-8") as fh:
        return [(row.get("ticker") or "").strip()
                for row in csv.DictReader(fh) if (row.get("ticker") or "").strip()]


def check_ticker(ticker: str) -> dict:
    """Data-only checks for one ticker. Never raises."""
    row = {c: "" for c in _COLUMNS}
    row["ticker"] = ticker
    try:
        proc = FactsDataProcessor(ticker)
        if not proc.load_data(max_years=5):
            row["error"] = "ingestion failed"
            return row

        profile = CompanyFinancialProfile(
            ticker             = ticker,
            sector             = proc.sector,
            market_cap         = proc.market_cap,
            financials_payload = proc.financials,
            fine_industry      = proc._fine_industry,
        )
        periods = profile.periods
        if not periods:
            row["error"] = "no periods"
            return row

        # Same share-resolution path the ValuationAgent uses, so the sweep
        # sees exactly what the report sees (including the unit-correction
        # and implied-count fallbacks, which need market data).
        info = {}
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info or {}
        except Exception:
            pass
        current_price = info.get("currentPrice") or info.get("regularMarketPrice")
        market_cap    = proc.market_cap or info.get("marketCap")

        shares_rows, shares_source = _resolve_diluted_shares(
            profile, periods, ticker, current_price, market_cap
        )
        raw_shares = dict(shares_rows)
        anomaly      = _resolve_share_anomalies(shares_rows, periods,
                                                shares_source, ticker)
        contaminated = anomaly["contaminated"]
        corrections  = anomaly["corrections"]
        classes      = anomaly["classes"]
        boundary     = (_split_boundary_info(anomaly["shares"], periods)
                        if contaminated else {})

        # "Detected" means an anomaly was found at all -- corrected or
        # suppressed -- so the count stays comparable to the pre-classifier
        # sweep.
        detected = bool(classes)
        row["split_detected"]     = "Y" if detected else "N"
        # One classification per ticker; join if a ticker somehow has several.
        row["classification"]     = " ".join(sorted({v for v in classes.values()}))
        row["periods_corrected"]  = " ".join(f"FY{str(p)[:4]}"
                                             for p in sorted(corrections))
        row["periods_suppressed"] = " ".join(f"FY{str(p)[:4]}"
                                             for p in sorted(contaminated))
        if boundary.get("factor"):
            row["implied_ratio"] = (f"{boundary['factor']:g}:1"
                                    f"{' (reverse)' if boundary.get('direction') == 'reverse split' else ''}")
        elif detected:
            _r = max((abs(v) for v in anomaly["ratios"].values()), default=None)
            if _r:
                row["implied_ratio"] = f"{_r:,.4g}x (corrected)"

        # Interest expense = 0/None while debt outstanding, most recent period.
        ie   = _safe_val(profile.income_statement.interest_expense, 0)
        debt = _safe_val(profile.balance_sheet.total_debt, 0)
        row["interest_contradiction"] = "Y" if ((not ie) and debt and debt > 0) else "N"

        # Gross-margin YoY move, to size the effect of the 500 -> 300bps
        # threshold change. Uses the same Revenue/GrossProfit basis as
        # FundamentalAgent's general branch (revenue - cogs when no gross
        # profit line is filed) but not its per-sector relabelling, so this
        # is an upper bound on how many tickers the flag could newly reach.
        if len(periods) >= 2:
            inc = profile.income_statement
            gp, rev, cogs = inc.gross_profit, inc.revenue, inc.cogs

            def _gm(i):
                r = _safe_val(rev, i)
                g = _safe_val(gp, i)
                if not r:
                    return None
                if not g:
                    c = _safe_val(cogs, i)
                    g = (r - c) if c else None
                if not g:
                    return None
                m = g / r
                return m if 0 < m <= 1.0 else None

            cur, prv = _gm(0), _gm(1)
            if cur is not None and prv is not None:
                drop_bps = (prv - cur) * 10000
                row["gm_drop_bps"]    = f"{drop_bps:.0f}"
                row["gm_flag_at_500"] = "Y" if drop_bps > 500 else "N"
                row["gm_flag_at_300"] = "Y" if drop_bps > 300 else "N"

        # Peer validation. Uses the real loader so the sweep measures the
        # same code path the report renders, including the session-scoped
        # validation cache that makes repeated peers cheap across a batch.
        try:
            peers = _PEER_LOADER.fetch(ticker) or {}
            if peers:
                _drop = peers.get("dropped", []) or []
                row["peers_derived"] = peers.get("n_derived", "")
                row["peers_valid"]   = peers.get("n_valid", "")
                row["peers_dropped"] = len(_drop)
                row["dropped_names"] = " ".join(p for p, _ in _drop)
                row["peer_basis"]    = peers.get("peer_basis", "")
            else:
                row["peers_dropped"] = 0
                row["peer_basis"]    = "none"
        except Exception as exc:
            row["peer_basis"] = f"error: {type(exc).__name__}"

    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc(limit=1)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="*", help="Explicit tickers (default: ratings.csv)")
    ap.add_argument("--limit", type=int, help="Stop after N tickers")
    ap.add_argument("--out", default=_DEFAULT_OUT)
    ap.add_argument("--resume", action="store_true",
                    help="Skip tickers already present in the output CSV")
    args = ap.parse_args()

    _facts_set_identity("Research Pipeline research_pipeline@example.com")

    tickers = args.tickers or _load_universe()

    done = set()
    if args.resume and os.path.exists(args.out):
        with open(args.out, newline="", encoding="utf-8") as fh:
            done = {r["ticker"] for r in csv.DictReader(fh) if r.get("ticker")}
        tickers = [t for t in tickers if t not in done]

    if args.limit:
        tickers = tickers[:args.limit]

    write_header = not (args.resume and os.path.exists(args.out))
    mode = "a" if (args.resume and os.path.exists(args.out)) else "w"

    t0 = time.time()
    with open(args.out, mode, newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_COLUMNS)
        if write_header:
            w.writeheader()
        for n, t in enumerate(tickers, 1):
            row = check_ticker(t)
            w.writerow(row)
            fh.flush()
            print(f"[{n}/{len(tickers)}] {t:6s} "
                  f"anom={row['split_detected'] or '-'} "
                  f"{row['classification']:<13s} "
                  f"corr={row['periods_corrected']:<8s} "
                  f"supp={row['periods_suppressed']:<8s} "
                  f"ratio={row['implied_ratio'] or '-':<14s} "
                  f"int={row['interest_contradiction'] or '-'} "
                  f"gm={row['gm_drop_bps'] or '-':>6s} "
                  f"{row['error']}", flush=True)

    summarise(args.out)
    print(f"\nElapsed: {(time.time() - t0)/60:.1f} min")


def summarise(path: str):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    ok      = [r for r in rows if not r["error"]]
    splits  = [r for r in ok if r["split_detected"] == "Y"]
    interest = [r for r in ok if r["interest_contradiction"] == "Y"]
    n = len(ok) or 1
    print("\n" + "=" * 64)
    print("  GENERALITY SWEEP SUMMARY")
    print("=" * 64)
    print(f"  tickers attempted            : {len(rows)}")
    print(f"  tickers analysed OK          : {len(ok)}")
    print(f"  tickers with errors          : {len(rows) - len(ok)}")
    print(f"  share anomaly detected       : {len(splits)} ({len(splits)/n*100:.1f}%)")
    for k in ("unit_error", "likely_split", "unknown"):
        c = [r for r in splits if r.get("classification") == k]
        print(f"      {k:<14s}         : {len(c)}"
              f"{'  (corrected, not suppressed)' if k == 'unit_error' else '  (suppressed)'}")
    print(f"  interest contradiction       : {len(interest)} ({len(interest)/n*100:.1f}%)")

    # -- Peer validation --------------------------------------------------
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    with_drops = [r for r in ok if _int(r.get("peers_dropped")) > 0]
    fellback   = [r for r in ok if r.get("peer_basis") == "industry_median"]
    no_peers   = [r for r in ok if r.get("peer_basis") == "none"]
    print(f"  tickers with >=1 peer dropped: {len(with_drops)} "
          f"({len(with_drops)/n*100:.1f}%)")
    print(f"  fell back to industry median : {len(fellback)}")
    print(f"  no usable peer table         : {len(no_peers)}")
    freq: dict = {}
    for r in ok:
        for name in (r.get("dropped_names") or "").split():
            freq[name] = freq.get(name, 0) + 1
    if freq:
        print("  most frequently dropped peers:")
        for name, c in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
            print(f"    {name:8s} dropped for {c} ticker(s)")

    # -- Gross-margin threshold impact -----------------------------------
    gm  = [r for r in ok if r.get("gm_drop_bps") not in (None, "")]
    f500 = [r for r in gm if r.get("gm_flag_at_500") == "Y"]
    f300 = [r for r in gm if r.get("gm_flag_at_300") == "Y"]
    newly = [r for r in gm if r.get("gm_flag_at_300") == "Y"
             and r.get("gm_flag_at_500") != "Y"]
    print(f"\n  gross-margin YoY computable  : {len(gm)}")
    print(f"  flags at 500bps (old)        : {len(f500)} ({len(f500)/n*100:.1f}% of universe)")
    print(f"  flags at 300bps (new)        : {len(f300)} ({len(f300)/n*100:.1f}% of universe)")
    print(f"  NEWLY flagged at 300bps      : {len(newly)} ({len(newly)/n*100:.1f}% of universe)")
    if len(newly) > 60:
        print("    !! >60 newly flagged (>12% of universe) — 300bps is too loose;")
        print("       revert gross_margin_drop_bps to 500 rather than ship a noisy flag")
    else:
        print(f"    ok  {len(newly)} newly flagged is at or below the ~60 ceiling")

    if splits:
        print("\n  Share anomalies:")
        for r in sorted(splits, key=lambda r: (r.get("classification", ""), r["ticker"])):
            action = (f"corrected: {r['periods_corrected']}"
                      if r.get("periods_corrected")
                      else f"suppressed: {r['periods_suppressed']}")
            print(f"    {r['ticker']:6s} {r.get('classification',''):<13s} "
                  f"{r['implied_ratio']:<16s} {action}")
    print("\n  Sanity thresholds:")
    pct_split = len(splits) / n * 100
    pct_int   = len(interest) / n * 100
    if pct_split > 15:
        print("    !! >15% show a split — detector likely over-firing; tighten bounds")
    elif pct_split == 0:
        print("    !! 0% show a split — detector may not be firing at all")
    else:
        print(f"    ok  split rate {pct_split:.1f}% is within the 0-15% expected band")
    if pct_int > 30:
        print("    !! >30% show the interest contradiction — tag chain likely incomplete")
    else:
        print(f"    ok  interest contradiction rate {pct_int:.1f}% is at or below 30%")


if __name__ == "__main__":
    main()
