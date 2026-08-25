"""
erp_fetcher.py — Fetches Damodaran implied ERP from ERPbymonth.xlsx

Source: https://pages.stern.nyu.edu/~adamodar/pc/implprem/ERPbymonth.xlsx
Updated: monthly by Damodaran

Sheet strategy:
  Primary:  'Last 12 months data' — col 3 (ERP), clean and current
  Fallback: 'Historical ERP'      — col 9 (ERP T12m), full history

Cache strategy:
  1. Check local erp_cache.xlsx — if age <= 3 months, use directly
  2. If stale (> 3 months) — download fresh
     - If newer month than cache → save and use
     - If same month → keep cache
  3. If download fails → use cache
  4. If no cache → use hardcoded fallback

Returns ERP as decimal: 0.0431 = 4.31%
"""

import io
import logging
import datetime
import pathlib
import requests

logger = logging.getLogger(__name__)

_ERP_URL      = "https://pages.stern.nyu.edu/~adamodar/pc/implprem/ERPbymonth.xlsx"
_CACHE_FILE   = pathlib.Path(__file__).parent.parent / "erp_cache.xlsx"
_TIMEOUT      = 15
_STALE_MONTHS = 3
_FALLBACK     = 0.0431   # Jun 2026 — Damodaran implied ERP (T12m)
_FALLBACK_DT  = "Jun 2026 (hardcoded fallback)"

# Session cache
_session_erp:  float | None = None
_session_date: str   | None = None


def get_erp() -> tuple[float, str]:
    """Returns (erp_decimal, date_label) e.g. (0.0431, 'Jun 2026')."""
    global _session_erp, _session_date
    if _session_erp is not None:
        return _session_erp, _session_date
    erp, label    = _resolve_erp()
    _session_erp  = erp
    _session_date = label
    return erp, label


def _resolve_erp() -> tuple[float, str]:
    today = datetime.date.today()

    # ── Check cache age ───────────────────────────────────────────────────────
    cache_age_months = None
    if _CACHE_FILE.exists():
        mtime            = datetime.date.fromtimestamp(_CACHE_FILE.stat().st_mtime)
        cache_age_months = (today.year - mtime.year) * 12 + (today.month - mtime.month)
        logger.info("erp_fetcher: cache age ~%d months", cache_age_months)

    should_download = cache_age_months is None or cache_age_months > _STALE_MONTHS

    # ── Download if needed ────────────────────────────────────────────────────
    downloaded_raw: bytes | None = None
    if should_download:
        downloaded_raw = _download()

    # ── Parse downloaded file ─────────────────────────────────────────────────
    if downloaded_raw is not None:
        new_erp, new_label = _parse(downloaded_raw)
        if new_erp is not None:
            if _CACHE_FILE.exists():
                cache_erp, cache_label = _parse(_CACHE_FILE.read_bytes())
                new_ym   = _label_to_ym(new_label)
                cache_ym = _label_to_ym(cache_label) if cache_erp else (0, 0)
                if new_ym > cache_ym:
                    logger.info("erp_fetcher: updating cache %s → %s", cache_label, new_label)
                    _CACHE_FILE.write_bytes(downloaded_raw)
                else:
                    logger.info("erp_fetcher: cache is current (%s)", cache_label)
                    if cache_erp:
                        return cache_erp, cache_label
            else:
                _CACHE_FILE.write_bytes(downloaded_raw)
            return new_erp, new_label
        logger.warning("erp_fetcher: downloaded file parsed no valid rows")

    # ── Use cache ─────────────────────────────────────────────────────────────
    if _CACHE_FILE.exists():
        cache_erp, cache_label = _parse(_CACHE_FILE.read_bytes())
        if cache_erp is not None:
            logger.info("erp_fetcher: using cache %s = %.4f", cache_label, cache_erp)
            return cache_erp, cache_label

    # ── Hardcoded fallback ────────────────────────────────────────────────────
    logger.warning("erp_fetcher: all sources failed — using hardcoded fallback")
    return _FALLBACK, _FALLBACK_DT


def _download() -> bytes | None:
    try:
        resp = requests.get(_ERP_URL, timeout=_TIMEOUT)
        resp.raise_for_status()
        logger.info("erp_fetcher: downloaded %d bytes", len(resp.content))
        return resp.content
    except Exception as e:
        logger.warning("erp_fetcher: download failed — %s", e)
        return None


def _parse(raw: bytes) -> tuple[float | None, str | None]:
    """Parse ERPbymonth.xlsx, return (latest_erp, label)."""
    try:
        import pandas as pd
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")   # suppress openpyxl extension warnings
            xls = pd.ExcelFile(io.BytesIO(raw), engine="openpyxl")

        # ── Primary: 'Last 12 months data' sheet ─────────────────────────────
        if "Last 12 months data" in xls.sheet_names:
            df = xls.parse("Last 12 months data", header=0)
            # Columns: Date | S&P 500 | 10-year US Treasury | ERP | (unnamed)
            erp_col  = 3   # 4th column = ERP
            date_col = 0
            best_erp  = None
            best_label = None
            for _, row in df.iterrows():
                try:
                    erp = float(row.iloc[erp_col])
                    if not (0.01 <= erp <= 0.20):
                        continue
                    dt = row.iloc[date_col]
                    if hasattr(dt, "year"):
                        label = _fmt_label(dt.year, dt.month)
                    else:
                        continue
                    best_erp   = erp
                    best_label = label
                except Exception:
                    continue
            if best_erp is not None:
                logger.info("erp_fetcher: last-12-months sheet → %s = %.4f",
                            best_label, best_erp)
                return best_erp, best_label

        # ── Fallback: 'Historical ERP' sheet — col 9 (ERP T12m) ──────────────
        if "Historical ERP" in xls.sheet_names:
            df = xls.parse("Historical ERP", header=0)
            erp_col  = 9   # 'ERP (T12m)'
            date_col = 0
            best_erp  = None
            best_label = None
            for _, row in df.iterrows():
                try:
                    erp = float(row.iloc[erp_col])
                    if not (0.01 <= erp <= 0.20):
                        continue
                    dt = row.iloc[date_col]
                    if hasattr(dt, "year"):
                        label = _fmt_label(dt.year, dt.month)
                    else:
                        continue
                    best_erp   = erp
                    best_label = label
                except Exception:
                    continue
            if best_erp is not None:
                logger.info("erp_fetcher: historical sheet → %s = %.4f",
                            best_label, best_erp)
                return best_erp, best_label

    except Exception as e:
        logger.warning("erp_fetcher: parse error — %s", e)

    return None, None


def _fmt_label(year: int, month: int) -> str:
    months = ["Jan","Feb","Mar","Apr","May","Jun",
              "Jul","Aug","Sep","Oct","Nov","Dec"]
    return f"{months[month-1]} {year}"


def _label_to_ym(label: str | None) -> tuple:
    if not label:
        return (0, 0)
    import re
    months = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
              "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
    m = re.match(r"([A-Za-z]{3})\s+(\d{4})", label or "")
    if m:
        mon = months.get(m.group(1))
        if mon:
            return (int(m.group(2)), mon)
    return (0, 0)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    erp, label = get_erp()
    print(f"\nERP: {erp*100:.2f}%  ({label})")
    print(f"Cache: {_CACHE_FILE}  exists={_CACHE_FILE.exists()}")
