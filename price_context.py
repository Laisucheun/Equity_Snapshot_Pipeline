"""
price_context.py — 52-week price range, moving averages, and volume context

Source: yfinance Ticker.history(period="1y") — daily OHLC + volume.
No new dependency. Same yfinance tier as ownership_loader, short_interest_loader.

Returned dict — public interface
---------------------------------
{
    "current":        373.02,
    "low_52w":        241.42,
    "high_52w":       468.35,
    "pct_above_low":  0.546,    # (current - low) / low
    "pct_below_high": -0.204,   # (current - high) / high  (negative = below high)
    "position_pct":   0.503,    # 0.0 = at 52w low, 1.0 = at 52w high
    "sma_30":         382.14,
    "sma_90":         401.33,
    "sma_200":        420.88,
    "sma_30_pct":    -0.025,    # (current - sma) / sma
    "sma_90_pct":    -0.071,
    "sma_200_pct":   -0.114,
    "volume_last":    18200000,
    "volume_avg_90":  22100000,
    "volume_vs_avg":  -0.176,   # (last - avg) / avg
    "_source": "yfinance history(period=1y)",
    "_as_of":  "2026-07-01",
}
# Empty dict {} if yfinance returns no history — never raises.
# SMAs only present when enough history exists (sma_200 needs ~200 trading days).
"""

import logging
import datetime

logger = logging.getLogger(__name__)


class PriceContextLoader:
    """Fetches 52-week price range, SMAs, and volume context via yfinance."""

    def fetch(self, ticker: str) -> dict:
        """Never raises. Returns {} when no usable data is found."""
        ticker = ticker.upper()
        try:
            import yfinance as yf
            import pandas as pd
        except ImportError:
            logger.warning("PriceContextLoader: yfinance not installed")
            return {}

        try:
            hist = yf.Ticker(ticker).history(period="1y")
        except Exception as e:
            logger.warning("PriceContextLoader: history fetch failed for %s — %s", ticker, e)
            return {}

        if hist is None or hist.empty or "Close" not in hist.columns:
            return {}

        close = hist["Close"].dropna()
        if len(close) < 5:
            return {}

        current   = float(close.iloc[-1])
        low_52w   = float(close.min())
        high_52w  = float(close.max())
        price_range = high_52w - low_52w

        result: dict = {
            "current":        round(current, 2),
            "low_52w":        round(low_52w, 2),
            "high_52w":       round(high_52w, 2),
            "pct_above_low":  round((current - low_52w)  / low_52w,  4) if low_52w  else None,
            "pct_below_high": round((current - high_52w) / high_52w, 4) if high_52w else None,
            "position_pct":   round((current - low_52w)  / price_range, 4) if price_range else None,
            "_source":        "yfinance history(period=1y)",
            "_as_of":         datetime.date.today().isoformat(),
        }

        # Moving averages — only add if enough data points exist
        for window, key in [(30, "sma_30"), (90, "sma_90"), (200, "sma_200")]:
            if len(close) >= window:
                sma = float(close.iloc[-window:].mean())
                result[key] = round(sma, 2)
                result[f"{key}_pct"] = round((current - sma) / sma, 4) if sma else None

        # Volume
        if "Volume" in hist.columns:
            vol = hist["Volume"].dropna()
            if not vol.empty:
                result["volume_last"] = int(vol.iloc[-1])
                if len(vol) >= 90:
                    avg_90 = float(vol.iloc[-90:].mean())
                    result["volume_avg_90"] = int(avg_90)
                    result["volume_vs_avg"] = round(
                        (vol.iloc[-1] - avg_90) / avg_90, 4
                    ) if avg_90 else None

        return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MSFT"

    loader = PriceContextLoader()
    r = loader.fetch(ticker)
    if r:
        print(f"\n{ticker} — price context (as of {r['_as_of']})")
        print(f"  52w Low:  ${r['low_52w']:,.2f}")
        print(f"  Current:  ${r['current']:,.2f}  "
              f"({r['pct_above_low']*100:+.1f}% vs low | "
              f"{r['pct_below_high']*100:+.1f}% vs high | "
              f"position {r['position_pct']*100:.0f}% of range)")
        print(f"  52w High: ${r['high_52w']:,.2f}")
        for sma in ["sma_30", "sma_90", "sma_200"]:
            if sma in r:
                print(f"  {sma.upper()}: ${r[sma]:,.2f}  ({r[f'{sma}_pct']*100:+.1f}%)")
        if "volume_last" in r:
            print(f"  Volume (last): {r['volume_last']:,}")
        if "volume_avg_90" in r:
            print(f"  Volume (90d avg): {r['volume_avg_90']:,}  "
                  f"({r['volume_vs_avg']*100:+.1f}% vs avg)")
    else:
        print(f"No price context data found for {ticker}.")
