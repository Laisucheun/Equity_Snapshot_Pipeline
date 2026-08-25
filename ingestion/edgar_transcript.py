"""
edgar_transcript.py — Fetch earnings prepared remarks from EDGAR 8-K filings

Strategy (no search engines, no third-party sites):
  1. Resolve ticker → CIK via data.sec.gov/submissions
  2. Find the most recent earnings 8-K filings (last 4 quarters)
  3. For each 8-K, fetch the filing index to get the exhibit list
  4. Look for Exhibit 99.2 (prepared remarks) or Exhibit 99.3
  5. Fetch and return the exhibit text

Why this works:
  - data.sec.gov is explicitly designed for programmatic access
  - No API key required — just a User-Agent header with contact info
  - Rate limit: 10 req/sec (well within normal use)
  - No blocking, no CAPTCHAs, no consent pages

Exhibit conventions by company:
  - ~60-70% of S&P 500 companies file prepared remarks as Exhibit 99.2
  - Some use Exhibit 99.3 (when 99.1=press release, 99.2=slides)
  - Companies that don't file it: NKE, COST, WMT (use IR website instead)
  - Companies that do: MSFT, ORCL, GOOGL, JPM, BAC, PEP, MRVL, etc.

Usage
-----
from ingestion.edgar_transcript import EdgarTranscriptLoader

loader = EdgarTranscriptLoader(identity="Your Name your@email.com")
result = loader.fetch("ORCL")
if result["available"]:
    print(result["source"])
    print(result["text"][:500])
"""

import re
import time
import logging
import requests

logger = logging.getLogger(__name__)

_BASE        = "https://data.sec.gov"
_ARCHIVE     = "https://www.sec.gov/Archives/edgar/data"
_TICKER_URL  = "https://www.sec.gov/files/company_tickers.json"
_DELAY       = 0.12   # ~8 req/sec, safely under 10 req/sec limit

# Exhibit descriptions that indicate prepared remarks / earnings call script
_PREPARED_REMARKS_RE = re.compile(
    r"(?:prepared\s+remarks?|earnings\s+call\s+(?:script|transcript|remarks?)|"
    r"conference\s+call\s+(?:script|transcript|remarks?)|"
    r"q[1-4]\s+(?:20\d\d\s+)?(?:fiscal\s+)?(?:earnings\s+)?(?:call\s+)?remarks?|"
    r"exhibit\s+99\.(?:2|3)\s*[–\-]?\s*(?:prepared|remarks?|script|transcript))",
    re.IGNORECASE
)

# Exhibit numbers that typically contain prepared remarks
_REMARKS_EXHIBIT_NUMS = {"EX-99.2", "EX-99.3", "99.2", "99.3"}

# 8-K item codes that indicate earnings results (not other 8-K events)
_EARNINGS_ITEMS_RE = re.compile(r"2\.02|results\s+of\s+operations", re.IGNORECASE)


class EdgarTranscriptLoader:
    """
    Fetches earnings call prepared remarks from EDGAR 8-K exhibits.

    Parameters
    ----------
    identity : str
        Required by SEC EDGAR — "Company Name email@domain.com"
        Used in the User-Agent header per SEC guidelines.
    """

    def __init__(self, identity: str = "EquityPipeline research@equitypipeline.com"):
        self._identity  = identity
        self._headers   = {
            "User-Agent":      identity,
            "Accept-Encoding": "gzip, deflate",
            "Host":            "data.sec.gov",
        }
        self._archive_headers = {
            "User-Agent":      identity,
            "Accept-Encoding": "gzip, deflate",
        }
        self._cik_cache: dict[str, str] = {}   # ticker → zero-padded CIK
        self._result_cache: dict[str, dict] = {}  # ticker → result

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, ticker: str, max_filings: int = 4) -> dict:
        """
        Fetch the most recent earnings call prepared remarks for a ticker.

        Returns
        -------
        dict:
            available : bool
            source    : str  e.g. "EDGAR 8-K Exhibit 99.2 (2026-03-18)"
            url       : str  direct URL to the exhibit
            text      : str  plain text of the prepared remarks
            error     : str  reason if not available
        """
        ticker = ticker.upper()
        if ticker in self._result_cache:
            return self._result_cache[ticker]

        try:
            cik = self._resolve_cik(ticker)
            if not cik:
                return self._cache_result(ticker, _unavailable(
                    f"CIK not found for {ticker}"
                ))

            filings = self._get_recent_8k_filings(cik, max_filings)
            if not filings:
                return self._cache_result(ticker, _unavailable(
                    f"No recent earnings 8-K filings found for {ticker}"
                ))

            for filing in filings:
                result = self._try_filing(cik, filing)
                if result and result["available"]:
                    logger.info(
                        "EdgarTranscriptLoader: %s — %s (%d chars)",
                        ticker, result["source"], len(result["text"])
                    )
                    return self._cache_result(ticker, result)

            return self._cache_result(ticker, _unavailable(
                f"No Exhibit 99.2/99.3 found in recent 8-K filings for {ticker}"
            ))

        except Exception as e:
            logger.warning("EdgarTranscriptLoader: %s — %s", ticker, e)
            return self._cache_result(ticker, _unavailable(str(e)))

    def fetch_all(self, ticker: str, max_filings: int = 4) -> dict:
        """
        Fetch all three EDGAR sources independently for a ticker.

        Returns
        -------
        dict:
            ex99_1 : dict  — press release (Exhibit 99.1)
            ex99_2 : dict  — prepared remarks (Exhibit 99.2/99.3) if filed
        Each sub-dict has: available, source, url, text, error
        """
        ticker  = ticker.upper()
        cache_k = f"all:{ticker}"
        if cache_k in self._result_cache:
            return self._result_cache[cache_k]

        try:
            cik = self._resolve_cik(ticker)
            if not cik:
                result = {
                    "ex99_1": _unavailable(f"CIK not found for {ticker}"),
                    "ex99_2": _unavailable(f"CIK not found for {ticker}"),
                }
                self._result_cache[cache_k] = result
                return result

            filings = self._get_recent_8k_filings(cik, max_filings)
            if not filings:
                result = {
                    "ex99_1": _unavailable(f"No earnings 8-K found for {ticker}"),
                    "ex99_2": _unavailable(f"No earnings 8-K found for {ticker}"),
                }
                self._result_cache[cache_k] = result
                return result

            ex99_1 = _unavailable("Exhibit 99.1 not found")
            ex99_2 = _unavailable("Exhibit 99.2/99.3 not found")

            for filing in filings:
                index = self._fetch_filing_index(cik, filing)
                if not index:
                    continue
                documents = index.get("documents", [])

                # Try 99.1 if not yet found
                if not ex99_1["available"]:
                    r = self._fetch_exhibit_by_num(
                        cik, filing, documents,
                        nums={"EX-99.1", "99.1"},
                        label="99.1 Press Release"
                    )
                    if r:
                        ex99_1 = r

                # Try 99.2 / 99.3 if not yet found
                if not ex99_2["available"]:
                    candidate = self._find_prepared_remarks_exhibit(documents)
                    if candidate:
                        accession_dir = filing["accession"].replace("-", "")
                        cik_int       = str(int(cik))
                        url = (
                            f"https://www.sec.gov/Archives/edgar/data/"
                            f"{cik_int}/{accession_dir}/{candidate['filename']}"
                        )
                        text = self._fetch_exhibit_text(url)
                        if text and len(text.strip()) > 200:
                            ex99_2 = {
                                "available": True,
                                "source":    f"EDGAR 8-K Exhibit {candidate['exhibit']} ({filing['filing_date']})",
                                "url":       url,
                                "text":      text,
                                "error":     None,
                            }

                if ex99_1["available"] and ex99_2["available"]:
                    break

            result = {"ex99_1": ex99_1, "ex99_2": ex99_2}
            self._result_cache[cache_k] = result
            return result

        except Exception as e:
            logger.warning("EdgarTranscriptLoader.fetch_all: %s — %s", ticker, e)
            result = {
                "ex99_1": _unavailable(str(e)),
                "ex99_2": _unavailable(str(e)),
            }
            self._result_cache[cache_k] = result
            return result

    def _fetch_filing_index(self, cik: str, filing: dict) -> dict | None:
        """Fetch and return the filing index JSON, or None on failure."""
        cik_int       = str(int(cik))
        accession_dir = filing["accession"].replace("-", "")
        index_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik_int}/{accession_dir}/{filing['accession']}-index.json"
        )
        try:
            resp = requests.get(index_url, headers=self._archive_headers, timeout=15)
            resp.raise_for_status()
            time.sleep(_DELAY)
            return resp.json()
        except Exception as e:
            logger.debug("EdgarTranscriptLoader: index fetch error — %s", e)
            return None

    def _fetch_exhibit_by_num(
        self, cik: str, filing: dict, documents: list,
        nums: set, label: str
    ) -> dict | None:
        """Fetch the first exhibit whose type is in nums."""
        cik_int       = str(int(cik))
        accession_dir = filing["accession"].replace("-", "")
        for doc in documents:
            exhibit = str(doc.get("type", "")).strip()
            fname   = str(doc.get("filename", "")).strip().lower()
            if exhibit not in nums:
                continue
            if fname.endswith(".pdf"):
                continue
            if not fname.endswith((".htm", ".html", ".txt")):
                continue
            url  = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_int}/{accession_dir}/{doc['filename']}"
            )
            text = self._fetch_exhibit_text(url)
            if text and len(text.strip()) > 200:
                return {
                    "available": True,
                    "source":    f"EDGAR 8-K Exhibit {label} ({filing['filing_date']})",
                    "url":       url,
                    "text":      text,
                    "error":     None,
                }
        return None

    # ── CIK resolution ────────────────────────────────────────────────────────

    def _resolve_cik(self, ticker: str) -> str | None:
        """
        Resolve ticker → zero-padded 10-digit CIK.
        Uses SEC's company_tickers.json — maps all exchange-listed tickers.
        """
        if ticker in self._cik_cache:
            return self._cik_cache[ticker]

        try:
            resp = requests.get(
                _TICKER_URL,
                headers={"User-Agent": self._identity},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            time.sleep(_DELAY)

            for entry in data.values():
                if entry.get("ticker", "").upper() == ticker:
                    cik = str(entry["cik_str"]).zfill(10)
                    self._cik_cache[ticker] = cik
                    logger.debug("EdgarTranscriptLoader: %s → CIK %s", ticker, cik)
                    return cik
        except Exception as e:
            logger.debug("EdgarTranscriptLoader: CIK lookup error — %s", e)

        # Fallback: try submissions endpoint directly with ticker
        try:
            resp = requests.get(
                f"{_BASE}/submissions/CIK{ticker}.json",
                headers=self._headers,
                timeout=15,
            )
            if resp.status_code == 200:
                cik = str(resp.json().get("cik", "")).zfill(10)
                self._cik_cache[ticker] = cik
                return cik
            time.sleep(_DELAY)
        except Exception:
            pass

        return None

    # ── Filing discovery ──────────────────────────────────────────────────────

    def _get_recent_8k_filings(self, cik: str, max_filings: int) -> list[dict]:
        """
        Get the most recent earnings 8-K filings for a CIK.
        Returns list of dicts: {accession, filing_date, items}
        """
        try:
            resp = requests.get(
                f"{_BASE}/submissions/CIK{cik}.json",
                headers=self._headers,
                timeout=15,
            )
            resp.raise_for_status()
            time.sleep(_DELAY)

            data    = resp.json()
            recent  = data.get("filings", {}).get("recent", {})
            forms   = recent.get("form", [])
            dates   = recent.get("filingDate", [])
            accnums = recent.get("accessionNumber", [])
            items   = recent.get("items", [])

            results = []
            for i, form in enumerate(forms):
                if form != "8-K":
                    continue
                item_str = str(items[i]) if i < len(items) else ""
                # Filter to earnings 8-Ks (Item 2.02 = Results of Operations)
                if "2.02" not in item_str:
                    continue
                results.append({
                    "accession":    accnums[i],
                    "filing_date":  dates[i],
                    "items":        item_str,
                })
                if len(results) >= max_filings:
                    break

            return results

        except Exception as e:
            logger.debug("EdgarTranscriptLoader: filings fetch error — %s", e)
            return []

    # ── Exhibit extraction ────────────────────────────────────────────────────

    def _try_filing(self, cik: str, filing: dict) -> dict | None:
        """
        Fetch the filing index and look for Exhibit 99.2 / 99.3 prepared remarks.
        Returns result dict if found, None otherwise.
        """
        accession     = filing["accession"]
        filing_date   = filing["filing_date"]
        cik_int       = str(int(cik))          # unpadded for archive path
        accession_dir = accession.replace("-", "")

        # Fetch filing index JSON
        index_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik_int}/{accession_dir}/{accession}-index.json"
        )
        try:
            resp = requests.get(
                index_url,
                headers=self._archive_headers,
                timeout=15,
            )
            resp.raise_for_status()
            time.sleep(_DELAY)
            index = resp.json()
        except Exception as e:
            logger.debug("EdgarTranscriptLoader: index fetch error %s — %s", accession, e)
            return None

        # Scan exhibit list for prepared remarks
        documents = index.get("documents", [])
        candidate = self._find_prepared_remarks_exhibit(documents)
        if not candidate:
            return None

        # Fetch the exhibit
        exhibit_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik_int}/{accession_dir}/{candidate['filename']}"
        )
        text = self._fetch_exhibit_text(exhibit_url)
        if not text or len(text.strip()) < 200:
            return None

        return {
            "available": True,
            "source":    f"EDGAR 8-K Exhibit {candidate['exhibit']} ({filing_date})",
            "url":       exhibit_url,
            "text":      text,
            "error":     None,
        }

    def _find_prepared_remarks_exhibit(self, documents: list) -> dict | None:
        """
        Scan the filing's document list for the prepared remarks exhibit.

        Checks:
          1. Exhibit number in _REMARKS_EXHIBIT_NUMS (EX-99.2, EX-99.3)
          2. Description matches _PREPARED_REMARKS_RE
          3. File is .htm or .txt (not .pdf — harder to parse)
        """
        # First pass: exact exhibit number + description match
        for doc in documents:
            exhibit = str(doc.get("type", "")).strip()
            desc    = str(doc.get("description", "")).strip()
            fname   = str(doc.get("filename", "")).strip().lower()

            if exhibit not in _REMARKS_EXHIBIT_NUMS:
                continue
            if fname.endswith(".pdf"):
                continue
            if _PREPARED_REMARKS_RE.search(desc) or _PREPARED_REMARKS_RE.search(fname):
                return {"exhibit": exhibit, "filename": doc["filename"]}

        # Second pass: any EX-99.2 or EX-99.3 htm/txt (description may be generic)
        for doc in documents:
            exhibit = str(doc.get("type", "")).strip()
            fname   = str(doc.get("filename", "")).strip().lower()

            if exhibit not in _REMARKS_EXHIBIT_NUMS:
                continue
            if fname.endswith(".pdf"):
                continue
            if fname.endswith((".htm", ".html", ".txt")):
                return {"exhibit": exhibit, "filename": doc["filename"]}

        return None

    @staticmethod
    def _fetch_exhibit_text(url: str) -> str:
        """Fetch exhibit HTML/text and return as plain text."""
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "EquityPipeline research@equitypipeline.com"},
                timeout=20,
            )
            resp.raise_for_status()
            time.sleep(_DELAY)

            raw = resp.text

            # Strip HTML tags
            text = re.sub(r'<[^>]+>', ' ', raw)
            text = re.sub(r'&amp;',  '&',  text)
            text = re.sub(r'&nbsp;', ' ',  text)
            text = re.sub(r'&lt;',   '<',  text)
            text = re.sub(r'&gt;',   '>',  text)
            text = re.sub(r'&#\d+;', ' ',  text)
            text = re.sub(r'\s{3,}', '\n', text)
            return text.strip()

        except Exception as e:
            logger.debug("EdgarTranscriptLoader: exhibit fetch error — %s", e)
            return ""

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _cache_result(self, ticker: str, result: dict) -> dict:
        self._result_cache[ticker] = result
        return result


# ── Module helpers ────────────────────────────────────────────────────────────

def _unavailable(reason: str) -> dict:
    return {
        "available": False,
        "source":    "EDGAR 8-K Exhibit 99.2",
        "url":       None,
        "text":      "",
        "error":     reason,
    }
