"""
insider_transactions.py — Insider Activity Fetcher (SEC Form 4 via edgartools)

Architecture
------------
Source : SEC EDGAR Form 4 filings, parsed via edgartools (already a pipeline
         dependency — same library used by debt_note_fetcher.py and
         quarterly_processor.py). Relies on edgar.set_identity() already
         being called once in orchestrator.py; no separate auth needed here.
Cache  : SQLite (insider_transactions.db) — one normalised table:

    transactions — one row per (ticker, accession_number, insider_cik, line)

Scope
-----
Only open-market buy/sell transactions are counted toward the signal —
option exercises, RSU vesting, and gifts are routine compensation events,
not discretionary bets on the company. Transactions flagged as part of a
Rule 10b5-1 trading plan are excluded from the "cluster" signal since they
are pre-scheduled well in advance and don't reflect a real-time view, but
are still recorded for completeness (flagged separately).

Returned dict — public interface
---------------------------------
{
    "lookback_days":      90,
    "buy_count":          3,           # DISCRETIONARY buyers only — distinct
                                        # insiders with a net open-market buy
                                        # that was NOT under a 10b5-1 plan.
                                        # A 10b5-1 buy doesn't count toward
                                        # this even if it moved real dollars.
    "sell_count":         1,           # ALL distinct sellers, 10b5-1 or not
                                        # (see "Flag rules" below for why
                                        # sells aren't filtered the same way).
    "buy_value":          2_140_000.0, # USD, ALL open-market buys — 10b5-1
                                        # INCLUDED. This can exceed what
                                        # buy_count's insiders alone account
                                        # for, since buy_count excludes 10b5-1
                                        # buyers but buy_value doesn't filter
                                        # by plan type. "buy_count" answers
                                        # "how many people made a discretionary
                                        # bet"; "buy_value" answers "how much
                                        # money moved, period." They are
                                        # deliberately different scopes — not
                                        # a bug, but don't assume buy_value /
                                        # buy_count gives a meaningful average
                                        # discretionary trade size.
    "sell_value":         480_000.0,   # USD, all open-market sells.
    "net_value":          1_660_000.0,
    "cluster_buying":     True,        # >=3 distinct insiders bought within the window
    "transactions": [
        {
            "date":          "2026-06-12",
            "insider_name":  "Jane Smith",
            "position":      "Chief Financial Officer",
            "transaction":   "BUY",         # BUY | SELL
            "shares":        10_000,
            "price":         142.50,
            "value":         1_425_000.0,
            "is_10b5_1":     False,
        },
        ...
    ],
    "_source":   "SEC EDGAR Form 4 (edgartools)",
    "_as_of":    "current (Form 4 filed within 2 business days of transaction)",
}
# Empty dict {} returned when no usable data found — never raises.

Flag rules (suggested — applied by the calling agent, not here)
-----------------------------------------------------------------
    cluster_buying is True                      → cluster insider buying signal
    net_value > 0 and buy_count >= 2             → net insider accumulation
    net_value < 0 and sell_count >= 3            → broad insider distribution
      (sell-side signal is noisier — many sells are pre-scheduled 10b5-1 or
      tax-related diversification, so this threshold is intentionally higher)
"""

import logging
import os
import sqlite3
import datetime

logger = logging.getLogger(__name__)

_CLUSTER_MIN_INSIDERS = 3   # distinct buyers within the window to flag a cluster
_LARGE_POSITION_CHANGE_PCT = 0.50   # 50%+ of prior holding in one transaction


class _FootnoteGetWrapper:
    """
    Thin dict-like wrapper around edgartools' Footnotes.get(id) method.

    The real Footnotes object only exposes get(id) — confirmed live
    (2026-06-30) attrs=['extract', 'get', 'summary'] — with no way to
    enumerate all IDs. This wrapper lets _parse_trade_row call
    .get(id, default) exactly as it would on a plain dict, deferring the
    actual lookup to the underlying object on each call instead of
    pre-building a full {id: text} dict.
    """

    def __init__(self, footnotes_obj):
        self._fns = footnotes_obj

    def get(self, key: str, default: str = "") -> str:
        try:
            val = self._fns.get(key)
            return str(val) if val is not None else default
        except Exception as e:
            logger.debug("InsiderTransactionLoader: footnotes.get('%s') failed — %s",
                         key, e)
            return default

    def __bool__(self) -> bool:
        # Always truthy if we have a real Footnotes object to query —
        # used by `if footnote_lookup:` checks elsewhere in this module.
        return self._fns is not None


class InsiderTransactionLoader:
    """
    Fetches open-market insider Form 4 transactions and maintains a
    normalised SQLite history.

    Parameters
    ----------
    db_path : Path to insider_transactions.db.
              Defaults to insider_transactions.db next to this file.
    """

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "insider_transactions.db"
            )
        self._db_path = db_path
        self._init_db()

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, ticker: str, lookback_days: int = 90) -> dict:
        """
        Fetch open-market insider transactions within the lookback window.
        Never raises. Returns {} when no usable data is found.
        """
        ticker = ticker.upper()
        current = self._fetch_edgar(ticker, lookback_days)
        if not current:
            logger.warning("InsiderTransactionLoader: no data for %s", ticker)
            return {}

        self._store_transactions(ticker, current["transactions"])
        return current

    # ── Fetch via edgartools ─────────────────────────────────────────────────

    def _fetch_edgar(self, ticker: str, lookback_days: int) -> dict:
        try:
            import edgar
        except ImportError:
            logger.warning("InsiderTransactionLoader: edgartools not installed")
            return {}

        try:
            cutoff = (datetime.date.today()
                      - datetime.timedelta(days=lookback_days)).isoformat()
            company = edgar.Company(ticker)
            filings = company.get_filings(form="4", filing_date=f"{cutoff}:",
                                          amendments=False)
        except Exception as e:
            logger.warning("InsiderTransactionLoader: filing fetch failed for %s — %s",
                          ticker, e)
            print(f"    [InsiderTx] {ticker}: filing fetch raised — {e}")
            return {}

        n_filings = len(filings) if filings is not None else 0
        print(f"    [InsiderTx] {ticker}: {n_filings} Form 4 filings found "
              f"(since {cutoff})")
        if filings:
            try:
                dates = sorted(str(f.filing_date) for f in filings)
                print(f"    [InsiderTx] {ticker}: filing dates: {dates}")
            except Exception:
                pass
        if not filings:
            return {}

        transactions  = []
        n_parse_err   = 0
        n_no_trades   = 0
        n_attr_err    = 0
        _printed_sample_row = [False]   # mutable flag for one-time diagnostic
        for filing in filings:
            try:
                form4 = filing.obj()
            except Exception as e:
                n_parse_err += 1
                logger.debug("InsiderTransactionLoader: parse error %s — %s",
                             getattr(filing, "accession_number", "?"), e)
                continue

            insider_name = getattr(form4, "insider_name", None) or "Unknown"
            position     = getattr(form4, "position", None) or ""

            try:
                market_trades = form4.market_trades
            except Exception as e:
                n_attr_err += 1
                logger.debug("InsiderTransactionLoader: market_trades attr error "
                             "on %s — %s", getattr(filing, "accession_number", "?"), e)
                continue
            if market_trades is None or market_trades.empty:
                n_no_trades += 1
                continue

            # market_trades' `footnotes` column holds ID references (e.g. "F4"),
            # not the actual footnote text — the real text lives on form4's
            # top-level `footnotes` attribute, keyed by ID. Resolve once per
            # filing rather than per row.
            footnote_lookup = self._resolve_footnotes(form4)
            if not _printed_sample_row[0] and footnote_lookup:
                # Smoke-test against whatever footnote ID the first row
                # actually references, rather than a hardcoded guess.
                test_id = None
                if not market_trades.empty:
                    test_id = str(market_trades.iloc[0].get("footnotes", "")).split(",")[0].strip()
                if test_id:
                    print(f"    [InsiderTx] {ticker}: footnote_lookup.get"
                          f"('{test_id}') = {footnote_lookup.get(test_id)!r}")

            for _, row in market_trades.iterrows():
                if not _printed_sample_row[0]:
                    print(f"    [InsiderTx] {ticker}: sample raw row: {dict(row)}")
                    _printed_sample_row[0] = True
                parsed = self._parse_trade_row(row, insider_name, position,
                                               footnote_lookup)
                if parsed:
                    transactions.append(parsed)

        # Dedup — guards against amended filings or footnote-driven duplicate
        # rows producing the same economic transaction twice. Keyed on the
        # natural identity of a real trade: same insider, date, shares, price.
        seen = set()
        deduped = []
        n_dupes = 0
        for t in transactions:
            key = (t["insider_name"], t["date"], t["shares"], t["price"])
            if key in seen:
                n_dupes += 1
                continue
            seen.add(key)
            deduped.append(t)
        transactions = deduped

        # Transaction-date filter — the SEC fetch above filters by *filing*
        # date, but a single Form 4 can report a transaction that happened
        # well before its filing date (e.g. a dividend-reinvestment-plan
        # purchase batched into a later filing). Without this, a 2-year-old
        # $875 DRIP buy can surface as "recent insider activity." Re-filter
        # on the actual transaction date so the signal stays genuinely recent.
        n_stale = 0
        recent_transactions = []
        for t in transactions:
            if t["date"] >= cutoff:
                recent_transactions.append(t)
            else:
                n_stale += 1
        transactions = recent_transactions

        print(f"    [InsiderTx] {ticker}: {len(transactions)} transactions parsed "
              f"({n_dupes} duplicates removed, {n_stale} stale transaction-dates "
              f"excluded) | {n_parse_err} filing parse errors | {n_attr_err} "
              f"market_trades attribute errors | {n_no_trades} filings with no "
              f"market trades")

        if not transactions:
            return {}

        buy_value  = sum(t["value"] for t in transactions if t["transaction"] == "BUY")
        sell_value = sum(t["value"] for t in transactions if t["transaction"] == "SELL")
        # buyers excludes 10b5-1 (pre-scheduled) buys — cluster_buying is meant
        # to flag genuine, discretionary conviction buying, not pre-scheduled
        # plans. sellers does NOT apply the same filter (see Flag rules in the
        # module docstring — sell-side noise is handled via a higher count
        # threshold instead of exclusion). buy_value/sell_value above are
        # deliberately NOT filtered by plan type either way — they total all
        # open-market dollars moved, period. So buy_count and buy_value can
        # describe different sets of transactions on purpose: buy_count is
        # "how many people made a discretionary bet," buy_value is "how much
        # money moved." Don't divide one by the other expecting a meaningful
        # average trade size.
        buyers     = {t["insider_name"] for t in transactions
                      if t["transaction"] == "BUY" and not t["is_10b5_1"]}
        sellers    = {t["insider_name"] for t in transactions if t["transaction"] == "SELL"}

        # Large position changes — a transaction representing a substantial
        # share of the insider's prior holding is a stronger signal than
        # dollar value alone (a $10M sale is routine for someone with $500M
        # left; it's a near-total exit for someone with $11M left). Not
        # restricted to non-10b5-1 trades: even a scheduled plan liquidating
        # most of a position is worth surfacing, just with the weaker-signal
        # caveat already noted via the [10b5-1] tag.
        large_changes = [
            t for t in transactions
            if t.get("pct_of_position") is not None
            and t["pct_of_position"] >= _LARGE_POSITION_CHANGE_PCT
        ]

        return {
            "lookback_days":  lookback_days,
            "buy_count":      len(buyers),
            "sell_count":     len(sellers),
            "buy_value":      buy_value,
            "sell_value":     sell_value,
            "net_value":      buy_value - sell_value,
            "cluster_buying": len(buyers) >= _CLUSTER_MIN_INSIDERS,
            "large_position_change": len(large_changes) > 0,
            "transactions":   sorted(transactions, key=lambda t: t["date"], reverse=True),
            "_source":  "SEC EDGAR Form 4 (edgartools)",
            "_as_of":   "current (Form 4 filed within 2 business days of transaction)",
        }

    @staticmethod
    def _resolve_footnotes(form4) -> dict:
        """
        Resolve form4.footnotes into an {id: text} lookup.

        Confirmed live (2026-06-30, edgartools, DDOG Form 4): form4.footnotes
        is a custom Footnotes object with public methods extract/get/summary.
        get(id) returns the resolved text for a footnote ID (e.g. "F3").
        This implementation calls get() for whatever IDs we end up needing
        rather than pre-building the full lookup, since the object doesn't
        expose a public way to enumerate all IDs.
        """
        fns = getattr(form4, "footnotes", None)
        if fns is None or not hasattr(fns, "get"):
            return {}
        return _FootnoteGetWrapper(fns)

    @staticmethod
    def _parse_trade_row(row, insider_name: str, position: str,
                         footnote_lookup: dict | None = None) -> dict | None:
        """
        Parse a single market_trades DataFrame row into a transaction dict.
        Returns None if the row is missing required fields.

        Confirmed live column names (2026-06-30, edgartools, NVDA Form 4s):
            Security, Date, Shares, Remaining, Price, AcquiredDisposed,
            DirectIndirect, NatureOfOwnership, form, Code, EquitySwap,
            footnotes, TransactionType
        AcquiredDisposed's actual value format (e.g. "A"/"D" vs "Acquired"/
        "Disposed") was not yet confirmed at the time of this fix — the
        startswith() check below handles both single-letter and full-word
        forms. If parsing still comes back empty, check the one-time raw
        row dump this function prints on its first call.
        """
        try:
            shares = row.get("Shares")
            price  = row.get("Price")
            date   = str(row.get("Date", ""))[:10]
            if not shares or not price or not date:
                logger.debug("InsiderTransactionLoader: row skipped — missing "
                             "Shares/Price/Date. Row: %s", dict(row))
                return None

            # Acquired = buy, Disposed = sell — standard Section 16 semantics.
            # Handle both single-letter ("A"/"D") and full-word forms.
            acq_disp = str(row.get("AcquiredDisposed", "")).upper().strip()
            if acq_disp.startswith("A"):
                txn = "BUY"
            elif acq_disp.startswith("D"):
                txn = "SELL"
            else:
                logger.debug("InsiderTransactionLoader: row skipped — "
                             "AcquiredDisposed='%s' not recognized. Row: %s",
                             acq_disp, dict(row))
                return None

            # `footnotes` on the row is an ID reference (e.g. "F4", or
            # "F1,F2" for multiple), not the footnote text itself. Resolve
            # each ID via footnote_lookup; fall back to treating the raw
            # value as text if no lookup was provided or resolution fails
            # (keeps behavior safe if edgartools' footnote shape changes).
            footnote_ids = str(row.get("footnotes", "") or "")
            if footnote_lookup:
                resolved = [
                    footnote_lookup.get(fid.strip(), "")
                    for fid in footnote_ids.split(",") if fid.strip()
                ]
                footnote_text = " ".join(resolved) or footnote_ids
            else:
                footnote_text = footnote_ids
            is_10b5_1 = "10b5-1" in footnote_text.lower()

            # `Remaining` is the insider's post-transaction holding of this
            # security (direct or indirect, per DirectIndirect), straight
            # from the filing. Used to compute what fraction of their
            # position this single transaction represents — the same
            # 500,000-share sale means routine trimming for someone with
            # 10M shares left, or a near-total exit for someone with 50,000
            # left. Pre-transaction holdings are backed out from shares +/-
            # remaining depending on direction.
            remaining = row.get("Remaining")
            pct_of_position = None
            try:
                if remaining is not None and shares:
                    remaining = float(remaining)
                    shares_f  = float(shares)
                    if txn == "SELL":
                        pre_holding = shares_f + remaining
                    else:   # BUY
                        pre_holding = remaining - shares_f
                    if pre_holding > 0:
                        pct_of_position = shares_f / pre_holding
                    # pre_holding <= 0 (e.g. a brand-new position via this
                    # buy) leaves pct_of_position as None — "% of prior
                    # position" is undefined when there was no prior position.
            except (TypeError, ValueError):
                pct_of_position = None

            return {
                "date":            date,
                "insider_name":    insider_name,
                "position":        position,
                "transaction":     txn,
                "shares":          int(shares),
                "price":           float(price),
                "value":           float(shares) * float(price),
                "is_10b5_1":       is_10b5_1,
                "remaining":       remaining,
                "pct_of_position": pct_of_position,
            }
        except Exception as e:
            logger.debug("InsiderTransactionLoader: row parse exception — %s. "
                         "Row: %s", e, dict(row))
            return None

    # ── SQLite cache ──────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self._db_path) as con:
                con.execute("""
                    CREATE TABLE IF NOT EXISTS transactions (
                        ticker          TEXT NOT NULL,
                        date            TEXT NOT NULL,
                        insider_name    TEXT,
                        position        TEXT,
                        txn_type        TEXT,
                        shares          INTEGER,
                        price           REAL,
                        value           REAL,
                        is_10b5_1       INTEGER,
                        remaining       REAL,
                        pct_of_position REAL,
                        fetched_at      TEXT,
                        PRIMARY KEY (ticker, date, insider_name, shares, price)
                    )
                """)
                # Migrate: add columns that may be missing from older DB schema
                existing_cols = {
                    row[1]
                    for row in con.execute("PRAGMA table_info(transactions)")
                }
                if "remaining" not in existing_cols:
                    con.execute("ALTER TABLE transactions ADD COLUMN remaining REAL")
                if "pct_of_position" not in existing_cols:
                    con.execute("ALTER TABLE transactions ADD COLUMN pct_of_position REAL")
                con.commit()
        except Exception as e:
            logger.warning("InsiderTransactionLoader: DB init failed — %s", e)

    def _store_transactions(self, ticker: str, transactions: list[dict]) -> None:
        if not transactions:
            return
        try:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            with sqlite3.connect(self._db_path) as con:
                con.executemany("""
                    INSERT OR REPLACE INTO transactions
                        (ticker, date, insider_name, position, txn_type,
                         shares, price, value, is_10b5_1, remaining,
                         pct_of_position, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    (ticker, t["date"], t["insider_name"], t["position"],
                     t["transaction"], t["shares"], t["price"], t["value"],
                     int(t["is_10b5_1"]), t.get("remaining"),
                     t.get("pct_of_position"), now)
                    for t in transactions
                ])
                con.commit()
        except Exception as e:
            logger.warning("InsiderTransactionLoader: DB store failed for %s — %s",
                          ticker, e)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    identity = sys.argv[2] if len(sys.argv) > 2 else "Your Name your@email.com"

    import edgar
    edgar.set_identity(identity)

    loader = InsiderTransactionLoader()
    result = loader.fetch(ticker)
    if result:
        print(f"\n{ticker} — insider activity (last {result['lookback_days']} days)")
        print(f"Buyers: {result['buy_count']}  |  Sellers: {result['sell_count']}")
        print(f"Buy value:  ${result['buy_value']:,.0f}")
        print(f"Sell value: ${result['sell_value']:,.0f}")
        print(f"Net value:  ${result['net_value']:,.0f}")
        print(f"Cluster buying: {result['cluster_buying']}")
        print(f"\nTransactions ({len(result['transactions'])}):")
        for t in result["transactions"][:20]:
            plan = " [10b5-1]" if t["is_10b5_1"] else ""
            print(f"  {t['date']}  {t['transaction']:<4} {t['insider_name']:<25} "
                  f"{t['position']:<30} {t['shares']:>10,} @ ${t['price']:.2f}{plan}")
    else:
        print(f"No insider transaction data found for {ticker}.")
