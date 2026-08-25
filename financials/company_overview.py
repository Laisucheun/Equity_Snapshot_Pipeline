"""
company_overview.py — "What do they do / where does revenue come from" summary

Two independent pieces, fetched separately, each gracefully optional:

1. Business summary (SAFE, verified pattern)
   -----------------------------------------
   yfinance .info["longBusinessSummary"] — a ready-made paragraph describing
   the company's products/segments. Same reliability tier as every other
   yfinance-sourced field already used in this pipeline (ownership_loader.py,
   short_interest_loader.py, peer_comparator.py).

2. Segment revenue table (EXPERIMENTAL — NOT YET LIVE-VERIFIED)
   ---------------------------------------------------------------
   Attempts to pull dimensional (segment-level) revenue breakdown from the
   most recent 10-K via edgartools. This is a best-effort attempt, not a
   confirmed-working integration:

   - quarterly_processor.py's _get_df() filters XBRL statement dataframes TO
     rows where standard_concept.notna() — specifically to DROP dimensional
     (segment/product-axis) rows and keep only the consolidated top-level
     line. This module does the opposite: it looks at the rows quarterly_
     processor.py throws away, on the theory that dimensional breakdowns
     live in exactly those discarded rows. That's an inference from how the
     rest of this codebase already uses edgartools, not something fetched
     and confirmed against a live filing.
   - Per every prior session's lessons-learned notes on edgartools (see
     handoff docs): "every property we used was wrong on first guess and
     required live diagnostic round-trips." No live round-trip has happened
     for this module yet. Treat _fetch_segments() as a first draft that
     will very likely need correction against real output before trusting
     its results in a report.
   - Fails soft in every case: returns (None, None) rather than raising or
     fabricating a segment breakdown. The renderer omits the segment table
     entirely when this returns nothing — same "honest N/A" convention as
     debt_note_fetcher.py's Format D handling.

Returned dict — public interface
---------------------------------
{
    "summary":        "Full business summary paragraph...",
    "segments":        {"iPhone": 201183000000.0, "Services": 96169000000.0} | None,
    "segment_period":  "2025-09-27" | None,
    "_segment_source": "10-K XBRL (experimental, unverified)" | None,
}
# summary may be present even when segments is None, and vice versa.
# Empty dict {} only if BOTH fail.
"""

import logging

logger = logging.getLogger(__name__)

# Same convention as quarterly_processor.py's _META_COLS — kept in sync
# manually since both modules independently parse the same kind of
# edgartools statement dataframe.
_META_COLS = {
    'concept', 'label', 'standard_concept', 'unit', 'balance', 'weight',
    'preferred_sign', 'parent_concept', 'parent_abstract_concept',
}

_REVENUE_HINTS = ("revenue", "sales", "net sales")


class CompanyOverviewLoader:
    """
    Fetches a business-description paragraph (yfinance) and a best-effort
    segment revenue breakdown (edgartools XBRL, experimental).
    """

    def fetch(self, ticker: str) -> dict:
        """Never raises. Returns {} only if both summary and segments fail."""
        ticker = ticker.upper()
        result: dict = {}

        summary = self._fetch_summary(ticker)
        if summary:
            result["summary"] = summary

        segments, period = self._fetch_segments(ticker)
        if segments:
            result["segments"]        = segments
            result["segment_period"]  = period
            result["_segment_source"] = "10-K XBRL (experimental, unverified)"

        return result

    # ── 1. Business summary (yfinance) ───────────────────────────────────────

    def _fetch_summary(self, ticker: str) -> str | None:
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("CompanyOverviewLoader: yfinance not installed")
            return None
        try:
            info = yf.Ticker(ticker).info
        except Exception as e:
            logger.warning("CompanyOverviewLoader: .info fetch failed for %s — %s", ticker, e)
            return None
        summary = (info or {}).get("longBusinessSummary")
        return summary.strip() if summary else None

    # ── 2. Segment revenue (edgartools XBRL — experimental) ──────────────────

    def _fetch_segments(self, ticker: str) -> tuple[dict | None, str | None]:
        try:
            import edgar
            import pandas as pd

            company = edgar.Company(ticker)
            filings = company.get_filings(form="10-K", amendments=False)
            if not filings:
                return None, None
            filing = list(filings)[0]
            xbrl = filing.xbrl()
            stmts = xbrl.statements

            is_obj = getattr(stmts, "income_statement", None)
            if is_obj is None:
                return None, None
            if callable(is_obj):
                is_obj = is_obj()
            df = is_obj.to_dataframe()

            if "standard_concept" not in df.columns:
                # Can't distinguish dimensional rows from top-level rows
                # without this column — bail rather than guess.
                return None, None

            # Dimensional (segment/product-axis) rows are exactly the ones
            # quarterly_processor.py's _get_df() filters OUT via
            # standard_concept.notna(). This is the inverse filter.
            seg_df = df[df["standard_concept"].isna()]
            if seg_df.empty:
                return None, None

            label_col = "label" if "label" in seg_df.columns else "concept"
            if label_col not in seg_df.columns:
                return None, None

            revenue_rows = seg_df[
                seg_df[label_col].astype(str).str.lower()
                .str.contains("|".join(_REVENUE_HINTS), na=False)
            ]
            if revenue_rows.empty:
                return None, None

            date_cols = [c for c in revenue_rows.columns if c not in _META_COLS]
            if not date_cols:
                return None, None
            period = date_cols[0]   # newest period, per the same convention
                                     # quarterly_processor.py relies on

            segments: dict[str, float] = {}
            for _, row in revenue_rows.iterrows():
                name = str(row.get(label_col) or "").strip()
                val  = row.get(period)
                if not name or val is None or (hasattr(pd, "isnull") and pd.isnull(val)):
                    continue
                try:
                    fval = float(val)
                except (ValueError, TypeError):
                    continue
                # Skip anything that isn't a plausible dollar figure (guards
                # against accidentally picking up a percentage or ratio row
                # if the label-matching above was too loose).
                if fval <= 0:
                    continue
                segments[name] = fval

            if not segments:
                return None, None
            return segments, str(period)[:10]

        except Exception as e:
            logger.debug("CompanyOverviewLoader: segment extraction failed for %s — %s",
                        ticker, e)
            return None, None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MSFT"

    loader = CompanyOverviewLoader()
    result = loader.fetch(ticker)
    if result.get("summary"):
        print(f"\n{ticker} — business summary:\n{result['summary'][:500]}...\n")
    else:
        print(f"\n{ticker} — no business summary found.\n")

    if result.get("segments"):
        print(f"Segments (as of {result['segment_period']}):")
        for name, val in result["segments"].items():
            print(f"  {name:<40} ${val:,.0f}")
    else:
        print("No segment revenue data found (expected — this part is experimental, "
              "needs live debugging against real edgartools output).")
