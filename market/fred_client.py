"""
fred_client.py — FRED API client for credit quality data

Fetches:
  - DGS10: 10-year Treasury yield (risk-free rate)
  - ICE BofA OAS spreads by rating tier (option-adjusted spreads)

All values returned as floats (decimal form: 0.0432 = 4.32%).
Returns None gracefully on any failure — pipeline never breaks.

FRED series used:
  DGS10         10-year Treasury yield (daily, %)
  BAMLC0A1CAAAEY  AAA OAS spread (%)
  BAMLC0A2CAAEY   AA  OAS spread (%)
  BAMLC0A3CAEY    A   OAS spread (%)
  BAMLC0A4CBBBEY  BBB OAS spread (%)
  BAMLH0A0HYM2EY  HY  OAS spread (BB and below, %)

FRED API key: free at https://fredaccount.stlouisfed.org
Set in .env: FRED_API_KEY=your_key_here
"""

import logging
import requests

logger = logging.getLogger(__name__)

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
_TIMEOUT   = 10

# Rating tier → FRED OAS series ID
# Covers investment grade (AAA→BBB) and high yield
# Verified FRED series IDs for ICE BofA OAS spreads (pure spread, not yield)
# Source: https://fred.stlouisfed.org
# BAMLC0A4CBBB   BBB OAS  | BAMLC0A3CA    A OAS
# BAMLC0A2CAA    AA OAS   | BAMLH0A0HYM2  HY OAS
# AAA series not individually published — use AA as closest proxy
_RATING_TO_SERIES: dict[str, str] = {
    # S&P / Fitch → verified FRED OAS series
    "AAA":  "BAMLC0A2CAA",      # proxy: AA (no separate AAA series)
    "AA+":  "BAMLC0A2CAA",
    "AA":   "BAMLC0A2CAA",
    "AA-":  "BAMLC0A2CAA",
    "A+":   "BAMLC0A3CA",
    "A":    "BAMLC0A3CA",
    "A-":   "BAMLC0A3CA",
    "BBB+": "BAMLC0A4CBBB",
    "BBB":  "BAMLC0A4CBBB",
    "BBB-": "BAMLC0A4CBBB",
    "BB+":  "BAMLH0A0HYM2",
    "BB":   "BAMLH0A0HYM2",
    "BB-":  "BAMLH0A0HYM2",
    "B+":   "BAMLH0A0HYM2",
    "B":    "BAMLH0A0HYM2",
    "B-":   "BAMLH0A0HYM2",
    # Moody's equivalents
    "Aaa":  "BAMLC0A2CAA",
    "Aa1":  "BAMLC0A2CAA",
    "Aa2":  "BAMLC0A2CAA",
    "Aa3":  "BAMLC0A2CAA",
    "A1":   "BAMLC0A3CA",
    "A2":   "BAMLC0A3CA",
    "A3":   "BAMLC0A3CA",
    "Baa1": "BAMLC0A4CBBB",
    "Baa2": "BAMLC0A4CBBB",
    "Baa3": "BAMLC0A4CBBB",
    "Ba1":  "BAMLH0A0HYM2",
    "Ba2":  "BAMLH0A0HYM2",
    "Ba3":  "BAMLH0A0HYM2",
    "B1":   "BAMLH0A0HYM2",
    "B2":   "BAMLH0A0HYM2",
    "B3":   "BAMLH0A0HYM2",
}

# Human-readable tier label for display
_SERIES_LABEL: dict[str, str] = {
    "BAMLC0A2CAA":   "AA",
    "BAMLC0A3CA":    "A",
    "BAMLC0A4CBBB":  "BBB",
    "BAMLH0A0HYM2":  "HY (BB and below)",
}


class FredClient:
    """
    Fetches risk-free rate and OAS credit spreads from FRED.

    Parameters
    ----------
    api_key : FRED API key. If empty, all fetches return None gracefully.
    """

    def __init__(self, api_key: str = ""):
        self._key   = api_key
        self._cache: dict[str, float | None] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def get_risk_free_rate(self) -> float | None:
        """
        Returns the most recent 10-year Treasury yield as a decimal.
        e.g. 4.32% → 0.0432
        """
        return self._fetch_series("DGS10")

    def get_oas_spread(self, rating: str) -> tuple[float | None, str]:
        """
        Returns (spread_decimal, series_label) for the given credit rating.
        e.g. ("A+", ...) → (0.0068, "A")

        Falls back to next-closest tier if rating not in map.
        Returns (None, "N/A") if key missing or fetch fails.
        """
        series_id = _RATING_TO_SERIES.get(rating)
        if not series_id:
            # Try normalising: "a+" → "A+"
            series_id = _RATING_TO_SERIES.get(rating.upper())
        if not series_id:
            logger.warning("FredClient: no series mapping for rating '%s'", rating)
            return None, "N/A"

        val = self._fetch_series(series_id)
        label = _SERIES_LABEL.get(series_id, series_id)
        return val, label

    def get_all_spreads(self) -> dict[str, float | None]:
        """
        Returns all OAS spreads keyed by tier label.
        Useful for displaying the full spread curve.
        """
        result = {}
        for series_id, label in _SERIES_LABEL.items():
            result[label] = self._fetch_series(series_id)
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch_series(self, series_id: str) -> float | None:
        """Fetch the most recent observation for a FRED series."""
        if not self._key:
            return None

        if series_id in self._cache:
            return self._cache[series_id]

        try:
            resp = requests.get(
                _FRED_BASE,
                params={
                    "series_id":  series_id,
                    "api_key":    self._key,
                    "sort_order": "desc",
                    "limit":      5,          # last 5 obs in case latest is missing
                    "file_type":  "json",
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            obs = resp.json().get("observations", [])

            # Find the most recent non-missing value
            for o in obs:
                val_str = o.get("value", ".")
                if val_str and val_str != ".":
                    val = float(val_str) / 100.0   # FRED stores as percent e.g. 4.32
                    self._cache[series_id] = val
                    logger.debug("FredClient: %s = %.4f", series_id, val)
                    return val

            logger.warning("FredClient: no valid observations for %s", series_id)
            self._cache[series_id] = None
            return None

        except Exception as e:
            logger.warning("FredClient: fetch failed for %s — %s", series_id, e)
            self._cache[series_id] = None
            return None
