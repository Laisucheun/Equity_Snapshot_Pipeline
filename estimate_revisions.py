"""
estimate_revisions.py — Sell-side EPS & revenue estimate revisions (yfinance)

Sources:
  yfinance Ticker.eps_trend        — EPS consensus + 7/30/60/90d lookback
  yfinance Ticker.revenue_estimate — Revenue consensus (current only; no lookback)

Returns current consensus estimates vs. 30/60/90 days ago for EPS, and
current consensus for revenue, across three horizons: current quarter,
current year, next year.

The revision DIRECTION is the primary signal — not the absolute estimate.
Rising estimates = street becoming more optimistic. Falling = deteriorating
expectations. Stable = no meaningful change.

Trend classification (EPS only — revenue has no lookback):
    Rising  — current estimate > 90d-ago estimate by more than +1%
    Falling — current estimate < 90d-ago estimate by more than -1%
    Stable  — within ±1% of 90d-ago estimate (or insufficient history)

yfinance API facts (verified from source, v1.5.1+):
  eps_trend DataFrame:
    Index:   0q  +1q  0y  +1y
    Columns: current  7daysAgo  30daysAgo  60daysAgo  90daysAgo
  revenue_estimate DataFrame:
    Index:   0q  +1q  0y  +1y
    Columns: numberOfAnalysts  avg  low  high  yearAgoRevenue  growth  currency

Returned dict — public interface
---------------------------------
{
    "periods": {
        "0q": {                            # current quarter
            "label":           "Current Quarter",
            "eps": {
                "current":     3.10,
                "ago_30d":     3.05,
                "ago_60d":     2.98,
                "ago_90d":     2.91,
                "trend":       "Rising",   # Rising | Falling | Stable | N/A
                "pct_change":  0.065,      # (current - ago_90d) / abs(ago_90d)
            },
            "revenue": {
                "current":     68100000000.0,
                "ago_30d":     None,       # not available from yfinance
                "ago_60d":     None,
                "ago_90d":     None,
                "trend":       "N/A",
                "pct_change":  None,
            },
        },
        "0y": { ... },   # current year
        "+1y": { ... },  # next year
    },
    "_source": "yfinance eps_trend + revenue_estimate",
    "_as_of":  "2026-07-01",
}
# Empty dict {} if yfinance returns no trend data — never raises.
# Individual sub-fields may be None when yfinance doesn't have that
# specific data point — rendered as N/A.
"""

import logging
import datetime

logger = logging.getLogger(__name__)

# Period keys as returned by the yfinance API (verified from source)
# Maps API key → display label
_PERIODS = {
    "0q":  "Current Quarter",
    "0y":  "Current Year",
    "+1y": "Next Year",
}

# Trend threshold — within ±1% of 90d-ago = Stable
_TREND_THRESHOLD = 0.01


class EstimateRevisionsLoader:
    """Fetches EPS and revenue estimate revisions via yfinance."""

    def fetch(self, ticker: str) -> dict:
        """Never raises. Returns {} when no usable data found."""
        ticker = ticker.upper()
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("EstimateRevisionsLoader: yfinance not installed")
            return {}

        try:
            tk = yf.Ticker(ticker)
            eps_trend     = tk.eps_trend
            revenue_est   = tk.revenue_estimate
        except Exception as e:
            logger.warning("EstimateRevisionsLoader: fetch failed for %s — %s", ticker, e)
            return {}

        eps_empty = eps_trend is None or (hasattr(eps_trend, "empty") and eps_trend.empty)
        rev_empty = revenue_est is None or (hasattr(revenue_est, "empty") and revenue_est.empty)

        if eps_empty and rev_empty:
            logger.debug("EstimateRevisionsLoader: no data for %s", ticker)
            return {}

        # Convert to row-keyed dicts for period lookup
        eps_dict = eps_trend.to_dict(orient="index") if not eps_empty else {}
        rev_dict = revenue_est.to_dict(orient="index") if not rev_empty else {}

        periods_out = {}
        for period_key, label in _PERIODS.items():
            eps_row = eps_dict.get(period_key, {})
            rev_row = rev_dict.get(period_key, {})

            eps_data = self._extract_eps(eps_row)
            rev_data = self._extract_revenue(rev_row)

            if not eps_data and not rev_data:
                continue

            periods_out[period_key] = {
                "label":   label,
                "eps":     eps_data,
                "revenue": rev_data,
            }

        if not periods_out:
            return {}

        return {
            "periods": periods_out,
            "_source": "yfinance eps_trend + revenue_estimate",
            "_as_of":  datetime.date.today().isoformat(),
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _extract_eps(self, row: dict) -> dict | None:
        """
        Extract EPS consensus + lookback from an eps_trend row.
        eps_trend columns: current  7daysAgo  30daysAgo  60daysAgo  90daysAgo
        """
        if not row:
            return None
        current = self._get(row, "current")
        if current is None:
            return None

        ago_30d = self._get(row, "30daysAgo")
        ago_60d = self._get(row, "60daysAgo")
        ago_90d = self._get(row, "90daysAgo")

        trend, pct = self._classify(current, ago_90d)

        return {
            "current":    current,
            "ago_30d":    ago_30d,
            "ago_60d":    ago_60d,
            "ago_90d":    ago_90d,
            "trend":      trend,
            "pct_change": pct,
        }

    def _extract_revenue(self, row: dict) -> dict | None:
        """
        Extract revenue consensus from a revenue_estimate row.
        revenue_estimate columns: avg  low  high  numberOfAnalysts  yearAgoRevenue  growth
        No lookback columns — 30/60/90d ago are always None.
        trend/pct_change use YoY growth estimate (different signal from EPS revision;
        renderer should label this column accordingly).
        """
        if not row:
            return None
        current = self._get(row, "avg")
        if current is None:
            return None

        growth = self._get(row, "growth")
        if growth is None:
            trend, pct = "N/A", None
        elif growth > _TREND_THRESHOLD:
            trend, pct = "Growing", round(growth, 4)
        elif growth < -_TREND_THRESHOLD:
            trend, pct = "Declining", round(growth, 4)
        else:
            trend, pct = "Stable", round(growth, 4)

        return {
            "current":    current,
            "year_ago":   self._get(row, "yearAgoRevenue"),
            "ago_30d":    None,
            "ago_60d":    None,
            "ago_90d":    None,
            "trend":      trend,
            "pct_change": pct,
        }

    @staticmethod
    def _get(row: dict, key: str):
        """Safe get — returns None for missing, NaN, or infinite values."""
        import math
        val = row.get(key)
        if val is None:
            return None
        try:
            f = float(val)
            if math.isnan(f) or math.isinf(f):
                return None
            return f
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _classify(current, ago_90d) -> tuple[str, float | None]:
        """Return (trend_label, pct_change) based on 90d delta."""
        if current is None or ago_90d is None or ago_90d == 0:
            return "N/A", None
        try:
            pct = (current - ago_90d) / abs(ago_90d)
            if pct > _TREND_THRESHOLD:
                return "Rising", round(pct, 4)
            elif pct < -_TREND_THRESHOLD:
                return "Falling", round(pct, 4)
            else:
                return "Stable", round(pct, 4)
        except Exception:
            return "N/A", None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MSFT"

    loader = EstimateRevisionsLoader()
    result = loader.fetch(ticker)
    if result:
        print(f"\n{ticker} — estimate revisions (as of {result['_as_of']})")
        for pk, pdata in result["periods"].items():
            print(f"\n  {pdata['label']} ({pk})")
            for metric in ["eps", "revenue"]:
                m = pdata.get(metric)
                if not m:
                    continue
                cur = m.get("current")
                a90 = m.get("ago_90d")
                pct = m.get("pct_change")
                trend = m.get("trend", "N/A")
                print(f"    {metric.upper():<10} current={cur}  "
                      f"90d ago={a90}  "
                      f"chg={f'{pct*100:+.1f}%' if pct is not None else 'N/A'}  "
                      f"trend={trend}")
    else:
        print(f"No estimate revision data found for {ticker}.")
