"""
analyst_targets.py — Sell-side analyst price targets (yfinance)

Thin wrapper around yfinance's Ticker.analyst_price_targets property:
https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.analyst_price_targets.html

Same reliability tier as every other yfinance-sourced field already used in
this pipeline (ownership_loader.py, short_interest_loader.py,
peer_comparator.py) — no new dependency, no SEC/EDGAR involvement.

Note on dates: analyst_price_targets itself carries no timestamp (unlike
short_interest_loader.py's `_as_of`, which is a real FINRA settlement
date) — it's yfinance's current snapshot at call time. `fetched_date`
below is when THIS PIPELINE fetched it, not a date yfinance provides.

Returned dict — public interface
---------------------------------
{
    "current":       373.02,   # current price, as returned by yfinance
    "low":           320.00,
    "high":          520.00,
    "mean":          445.30,
    "median":        450.00,
    "upside_pct":    0.1937,   # (mean - current) / current, decimal
    "fetched_date":  "2026-07-01",   # when THIS PIPELINE fetched it — see
                                      # note above, not a yfinance-provided date
    "_source":       "yfinance analyst_price_targets",
}
# Empty dict {} if yfinance has no target data for the ticker, or the
# property/field isn't present on the installed yfinance version — never
# raises. upside_pct omitted if current or mean is missing/zero.
"""

import logging
import datetime

logger = logging.getLogger(__name__)


class AnalystTargetsLoader:
    """Fetches sell-side analyst price targets via yfinance."""

    def fetch(self, ticker: str) -> dict:
        """Never raises. Returns {} when no usable target data is found."""
        ticker = ticker.upper()
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("AnalystTargetsLoader: yfinance not installed")
            return {}

        try:
            data = yf.Ticker(ticker).analyst_price_targets
        except Exception as e:
            logger.warning("AnalystTargetsLoader: fetch failed for %s — %s", ticker, e)
            return {}

        if not data:
            return {}

        current = data.get("current")
        low     = data.get("low")
        high    = data.get("high")
        mean    = data.get("mean")
        median  = data.get("median")

        if all(v is None for v in (current, low, high, mean, median)):
            return {}

        result = {
            "current": current,
            "low":     low,
            "high":    high,
            "mean":    mean,
            "median":  median,
            "fetched_date": datetime.date.today().isoformat(),
            "_source": "yfinance analyst_price_targets",
        }

        if current and mean:
            try:
                result["upside_pct"] = (mean - current) / current
            except (TypeError, ZeroDivisionError):
                pass

        return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MSFT"

    loader = AnalystTargetsLoader()
    result = loader.fetch(ticker)
    if result:
        print(f"\n{ticker} — analyst price targets:")
        print(f"  Current: ${result.get('current')}")
        print(f"  Low:     ${result.get('low')}")
        print(f"  Mean:    ${result.get('mean')}")
        print(f"  Median:  ${result.get('median')}")
        print(f"  High:    ${result.get('high')}")
        if "upside_pct" in result:
            print(f"  Upside (mean vs current): {result['upside_pct']*100:+.1f}%")
    else:
        print(f"No analyst price target data found for {ticker}.")
