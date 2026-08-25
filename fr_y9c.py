"""
fr_y9c.py — FR Y-9C Basel III capital ratio fetcher

Reads locally-downloaded BHCF text files from the FFIEC NIC Financial Data
Download portal to extract Basel III capital ratios for US bank holding companies.

DOWNLOAD INSTRUCTIONS
---------------------
Go to: https://www.ffiec.gov/npw/FinancialReport/FinancialDataDownload
Select the year, download the file, save to a local directory.
File naming from the portal: BHCF20251231.txt, BHCF20250930.txt, etc.

FILE FORMAT (verified against 2025 files)
------------------------------------------
- Delimiter:  caret (^)
- Encoding:   latin-1
- Rows:       ~3,700 bank holding companies
- Columns:    ~2,200 FR Y-9C fields

AVAILABLE CAPITAL RATIO FIELDS (verified)
-------------------------------------------
Field       Description                        Format
BHCA7206    Tier 1 risk-based capital ratio    percent (e.g. 15.52 = 15.52%)
BHCA7205    Total risk-based capital ratio     percent
BHCA7204    Tier 1 leverage ratio              percent
BHCAA223    Risk-weighted assets               dollars in thousands (NOT a ratio)

NOTE: CET1 ratio is NOT present in BHCF for G-SIBs (JPM, BAC, WFC, GS, C etc.).
Large advanced-approaches banks file CET1 under FFIEC 101, a separate form.
The Basel III table therefore shows Tier 1, Total Capital, and Tier 1 Leverage —
the three ratios directly available — and marks CET1 as N/A with a note.

RSSD IDs (holding company level, confirmed from XML_ATTRIBUTES_ACTIVE.XML)
---------------------------------------------------------------------------
JPM  1039502   BAC  1073757   WFC  1120754   GS  2380443
"""

import io
import os
import logging
import datetime
import zipfile

import pandas as pd

logger = logging.getLogger(__name__)


# ── Hardcoded fallback RSSD map ───────────────────────────────────────────────
_FALLBACK_RSSD_MAP = {
    "JPM": 1039502,
    "BAC": 1073757,
    "WFC": 1120754,
    "GS":  2380443,
}

# ── Verified field codes ──────────────────────────────────────────────────────
FIELD_TIER1    = "BHCA7206"   # Tier 1 risk-based ratio (%)
FIELD_TOTCAP   = "BHCA7205"   # Total capital ratio (%)
FIELD_LEVERAGE = "BHCA7204"   # Tier 1 leverage ratio (%)
# CET1 ratio (BHCAG476) is absent for G-SIBs in BHCF — not included
FIELD_RSSD     = "RSSD9001"
FIELD_DATE     = "RSSD9999"


class NICRegistry:
    """
    Parses XML_ATTRIBUTES_ACTIVE.XML to resolve ticker → RSSD ID dynamically.
    Uses CUSIP lookup via yfinance, with name-match fallback.
    """

    def __init__(self, xml_path: str):
        import xml.etree.ElementTree as ET
        self._entries = []
        try:
            root = ET.parse(xml_path).getroot()
        except Exception as e:
            logger.error("NICRegistry: failed to parse %s: %s", xml_path, e)
            return

        for attr in root.findall('attributes'):
            if (attr.findtext('bhc_ind') or '0').strip() != '1':
                continue
            if (attr.findtext('entity_type') or '').strip() != 'FHD':
                continue
            rssd  = attr.get('id_rssd')
            nm    = (attr.findtext('nm_lgl') or '').strip().upper()
            cusip = (attr.findtext('id_cusip') or '').strip()
            sec   = (attr.findtext('sec_rptg_status') or '0').strip()
            self._entries.append({
                'rssd':   int(rssd),
                'name':   nm,
                'cusip':  cusip if cusip != '0' else '',
                'sec':    sec,
            })
        logger.info("NICRegistry: loaded %d FHD entries", len(self._entries))

    def lookup(self, ticker: str) -> int | None:
        ticker = ticker.upper()
        # 1. CUSIP via yfinance
        cusip6 = self._get_cusip6(ticker)
        if cusip6:
            for e in self._entries:
                if e['cusip'].upper() == cusip6.upper():
                    logger.info("NICRegistry: %s → RSSD %d (CUSIP %s)", ticker, e['rssd'], cusip6)
                    return e['rssd']
        # 2. Name fallback
        name = self._get_company_name(ticker)
        if name:
            nu = name.upper()
            for e in [x for x in self._entries if x['sec'] == '3']:
                if nu[:20] in e['name'] or e['name'][:20] in nu:
                    logger.info("NICRegistry: %s → RSSD %d (name match)", ticker, e['rssd'])
                    return e['rssd']
        return None

    @staticmethod
    def _get_cusip6(ticker):
        try:
            import yfinance as yf
            c = yf.Ticker(ticker).info.get('cusip', '')
            return c[:6] if c and len(c) >= 6 else None
        except Exception:
            return None

    @staticmethod
    def _get_company_name(ticker):
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info
            return info.get('longName') or info.get('shortName')
        except Exception:
            return None


class FRY9CFetcher:
    """
    Fetches Basel III capital ratios from locally-downloaded BHCF text files.

    Parameters
    ----------
    nic_xml_path : path to XML_ATTRIBUTES_ACTIVE.XML for dynamic RSSD lookup.
    bhcf_dir     : directory containing BHCF files from the FFIEC portal.
                   Expected naming: BHCF20251231.txt, BHCF20241231.txt, etc.
    bhcf_files   : explicit (year, month) → filepath mapping, overrides bhcf_dir.
    """

    def __init__(self,
                 nic_xml_path: str = None,
                 bhcf_dir: str = None,
                 bhcf_files: dict = None):
        self._bhcf_dir   = bhcf_dir
        self._bhcf_files = bhcf_files or {}
        self._cache: dict[tuple, pd.DataFrame] = {}
        self._rssd_cache: dict[str, int | None] = {}
        self._registry = NICRegistry(nic_xml_path) if nic_xml_path else None

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, ticker: str, periods: list[str]) -> dict:
        """
        Parameters
        ----------
        ticker  : e.g. "JPM", "BAC", "WFC", "GS"
        periods : CompanyFinancialProfile.periods list

        Returns
        -------
        dict keyed by period string → {"tier1": float|None, "total_capital": ...,
                                        "lev_ratio": ..., "cet1": None}
        All ratio values in decimal form (0.155 = 15.5%).
        cet1 is always None — not available in BHCF for G-SIBs.
        """
        rssd = self._resolve_rssd(ticker)
        if rssd is None:
            return {}

        result = {}
        for period in periods:
            try:
                dt = datetime.date.fromisoformat(period[:10])
            except ValueError:
                result[period] = _empty_row()
                continue
            result[period] = self._fetch_row(rssd, _nearest_q4(dt))

        return result

    def rssd_for(self, ticker: str) -> int | None:
        return self._resolve_rssd(ticker)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _resolve_rssd(self, ticker: str) -> int | None:
        ticker = ticker.upper()
        if ticker not in self._rssd_cache:
            rssd = None
            if self._registry:
                rssd = self._registry.lookup(ticker)
            if rssd is None:
                rssd = _FALLBACK_RSSD_MAP.get(ticker)
                if rssd:
                    logger.info("fr_y9c: %s → RSSD %d (hardcoded)", ticker, rssd)
            self._rssd_cache[ticker] = rssd
        return self._rssd_cache[ticker]

    def _fetch_row(self, rssd: int, report_dt: datetime.date) -> dict:
        year, month = report_dt.year, report_dt.month
        df = self._cache.get((year, month))
        if df is None:
            df = self._load_file(year, month)
            self._cache[(year, month)] = df

        if df is None or df.empty:
            return _empty_row()

        subset = df[df[FIELD_RSSD] == rssd]
        if subset.empty:
            logger.debug("fr_y9c: RSSD %d not in %d-%02d", rssd, year, month)
            return _empty_row()

        row = subset.iloc[-1]
        return {
            "cet1":          None,   # not available in BHCF for G-SIBs
            "tier1":         _pct_to_decimal(row, FIELD_TIER1),
            "total_capital": _pct_to_decimal(row, FIELD_TOTCAP),
            "lev_ratio":     _pct_to_decimal(row, FIELD_LEVERAGE),
        }

    def _load_file(self, year: int, month: int) -> pd.DataFrame | None:
        """
        Locate and parse the BHCF file for the given year/month.

        Naming conventions tried:
          BHCF{YYYY}{MM:02d}{DD:02d}.txt  (full date, FFIEC portal format)
          BHCF{YYYY}{MM:02d}.txt / .csv / .zip
          bhcf{YY}{MM:02d}.txt / .csv     (Chicago Fed legacy)
        """
        # Last day of the quarter for full-date filenames
        last_day = {3: 31, 6: 30, 9: 30, 12: 31}.get(month, 31)
        yyyy = str(year)
        yy   = yyyy[2:]
        mm   = f"{month:02d}"
        dd   = f"{last_day:02d}"

        candidates = []

        # Explicit mapping
        if (year, month) in self._bhcf_files:
            candidates.append(self._bhcf_files[(year, month)])

        # bhcf_dir candidates — checked in priority order.
        # Supports the project structure:
        #   Industry Files/Financials/Basel_III/
        #       2025/BHCF20251231.txt   <- year subfolder (primary)
        #       BHCF20251231.txt        <- flat (fallback)
        if self._bhcf_dir:
            d = self._bhcf_dir.rstrip("/").rstrip("\\")
            fname_full  = f"BHCF{yyyy}{mm}{dd}.txt"
            fname_short = f"BHCF{yy}{mm}"
            candidates += [
                # Year subfolder -- matches project structure
                f"{d}/{yyyy}/{fname_full}",
                f"{d}/{yyyy}/{fname_full.upper()}",
                # Flat in bhcf_dir
                f"{d}/{fname_full}",
                f"{d}/{fname_full.upper()}",
                # Short naming variants (zipped or csv)
                f"{d}/{yyyy}/{fname_short}.zip",
                f"{d}/{fname_short}.zip",
                f"{d}/{fname_short}.ZIP",
                f"{d}/{fname_short}.CSV",
                f"{d}/{fname_short}.csv",
                # Chicago Fed legacy
                f"{d}/bhcf{yy}{mm}.csv",
                f"{d}/bhcf{yy}{mm}.txt",
            ]

        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                df = _parse_bhcf(path)
                if df is not None and not df.empty:
                    logger.info("fr_y9c: loaded %d rows from %s", len(df), path)
                    return df
            except Exception as exc:
                logger.debug("fr_y9c: error reading %s: %s", path, exc)

        logger.warning(
            "fr_y9c: no BHCF file found for %d-%02d. "
            "Download from https://www.ffiec.gov/npw/FinancialReport/FinancialDataDownload "
            "and pass bhcf_dir='<folder>' to FRY9CFetcher.",
            year, month
        )
        return None


# ── Module-level helpers ──────────────────────────────────────────────────────

def _parse_bhcf(path: str) -> pd.DataFrame | None:
    """
    Parse a BHCF file. Handles:
      - Caret-delimited .txt (FFIEC portal, verified format)
      - Zip containing a CSV/TXT
      - Comma-delimited CSV (Chicago Fed legacy)
    """
    needed = {FIELD_RSSD, FIELD_DATE, FIELD_TIER1, FIELD_TOTCAP, FIELD_LEVERAGE}

    if path.upper().endswith('.ZIP'):
        with zipfile.ZipFile(path) as zf:
            inner = next(
                (n for n in zf.namelist()
                 if n.upper().endswith(('.CSV', '.TXT'))),
                None
            )
            if inner is None:
                return None
            with zf.open(inner) as f:
                return _parse_bhcf_bytes(f.read(), needed)
    else:
        with open(path, 'rb') as f:
            return _parse_bhcf_bytes(f.read(), needed)


def _parse_bhcf_bytes(raw: bytes, needed: set) -> pd.DataFrame | None:
    """Detect delimiter and parse, keeping only needed columns."""
    try:
        # Detect delimiter from first line
        first_line = raw[:500].decode('latin-1').split('\n')[0]
        sep = '^' if '^' in first_line else ','

        df = pd.read_csv(
            io.BytesIO(raw),
            sep=sep,
            dtype=str,
            encoding='latin-1',
            low_memory=False,
            usecols=lambda c: c.upper() in needed,
        )
        df.columns = [c.upper() for c in df.columns]

        # Coerce RSSD ID to int for fast filtering
        if FIELD_RSSD in df.columns:
            df[FIELD_RSSD] = pd.to_numeric(
                df[FIELD_RSSD].str.strip(), errors='coerce'
            ).fillna(0).astype(int)
            df = df[df[FIELD_RSSD] > 0]

        return df
    except Exception as exc:
        logger.debug("fr_y9c: parse error: %s", exc)
        return None


def _pct_to_decimal(row: pd.Series, field: str) -> float | None:
    """
    Convert a percent field to decimal. BHCF stores ratios as percent
    (e.g. 15.52 = 15.52%), so we divide by 100.
    Returns None if missing, zero, or outside plausible range.
    """
    val = row.get(field)
    if val is None or str(val).strip() in ('', 'nan', 'NaN'):
        return None
    try:
        fv = float(str(val).strip())
    except ValueError:
        return None
    if fv == 0:
        return None
    decimal = fv / 100.0
    # Sanity: capital ratios are between 3% and 40%
    return decimal if 0.03 <= decimal <= 0.40 else None


def _nearest_q4(dt: datetime.date) -> datetime.date:
    """Map a FY-end date to the Q4 filing date (December 31)."""
    if dt.month <= 3:
        return datetime.date(dt.year - 1, 12, 31)
    return datetime.date(dt.year, 12, 31)


def _empty_row() -> dict:
    return {"cet1": None, "tier1": None, "total_capital": None, "lev_ratio": None}
