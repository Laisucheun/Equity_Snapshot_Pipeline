"""
xbrl_debt_fetcher.py — Fetches debt data via SEC XBRL company facts API

Tag waterfall follows the curated dictionary from us_gaap_debt_tags.csv:
  1. Aggregate Debt Balances (waterfall: LongTermDebt → LeaseObligations → NotesPayable)
  2. Debt Maturities (Y1-Y5 + thereafter — most rigidly standardised table in US GAAP)
  3. Interest Expense waterfall (InterestExpense → InterestExpenseDebt → InterestPaidNet)

Source: https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
"""

import logging
import datetime
import requests

logger = logging.getLogger(__name__)

_FACTS_BASE  = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_TIMEOUT     = 20
_HEADERS     = {"User-Agent": ""}

# ── Tag waterfalls ─────────────────────────────────────────────────────────────

# 1a. Non-current long-term debt (waterfall)
_LT_NONCURRENT = [
    "LongTermDebt",                          # gold standard — net carrying value
    "LongTermDebtNoncurrent",                # explicit non-current
    "LongTermDebtAndCapitalLeaseObligations",# includes leases
    "DebtAndCapitalLeaseObligations",        # broader
    "DebtInstrumentCarryingAmount",          # footnote-level carrying amount
    "NotesPayable",                          # fallback
]

# 1d. Gross total debt — some filers tag total face value (current+noncurrent)
# under this tag but NOT under LongTermDebt + LongTermDebtCurrent separately.
# Used as a post-hoc sanity check: if gross_total > computed total, use it.
# IMPORTANT: exclude lease-inclusive tags — MU's capital leases inflate
# LongTermDebtAndCapitalLeaseObligations above the pure debt figure.
_GROSS_TOTAL = [
    "LongTermDebtGross",   # face value before discount/issuance costs — no leases
]

# 1b. Current portion of LT debt (waterfall)
_LT_CURRENT = [
    "LongTermDebtCurrent",                          # gold standard
    "LongTermDebtAndCapitalLeaseObligationsCurrent",
    "LongTermDebtCurrentMaturities",                # alternate tag name
    "OtherShortTermBorrowings",
]

# 1c. Short-term / commercial paper
_ST_BORROWINGS = [
    "ShortTermBorrowings",
    "LineOfCreditFacilityAmountOutstanding",
]

# 2. Maturity schedule — gold standard, Year 1 through Year 5 + thereafter
_MATURITY_CONCEPTS = [
    ("LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths", 1),
    ("LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo",          2),
    ("LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree",        3),
    ("LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour",         4),
    ("LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive",         5),
    ("LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive",      6),
    # Fallback: remainder of fiscal year (off-cycle filers)
    ("LongTermDebtMaturitiesRepaymentsOfPrincipalRemainderOfFiscalYear", 0),
]

# 3. Interest expense waterfall (for IS-derived cost of debt)
_INTEREST_EXPENSE = [
    "InterestExpense",           # most universal
    "InterestExpenseDebt",       # excludes lease / deposit interest
    "InterestExpenseNonoperating",
    "InterestExpenseOperating",
    "InterestPaidNet",           # CF statement — actual cash paid
    "InterestPaid",              # CF fallback
]

# ── Session caches ─────────────────────────────────────────────────────────────
_cik_cache:   dict[str, str]  = {}
_facts_cache: dict[str, dict] = {}

# Hardcoded CIKs — used when SEC fetch fails (403, timeout)
_CIK_FALLBACK: dict[str, str] = {
    "AAPL":"0000320193","MSFT":"0000789019","NVDA":"0001045810",
    "AMZN":"0001018724","META":"0001326801","GOOGL":"0001652044",
    "GOOG":"0001652044","LLY":"0000059478","AVGO":"0001730168",
    "TSLA":"0001318605","WMT":"0000104169","JPM":"0000019617",
    "V":"0001403161","UNH":"0000072971","XOM":"0000034088",
    "MA":"0001141391","COST":"0000909832","HD":"0000354950",
    "PG":"0000080424","JNJ":"0000200406","ABBV":"0001551152",
    "BAC":"0000070858","NFLX":"0001065280","KO":"0000021344",
    "CRM":"0001108524","CVX":"0000093410","MRK":"0000310158",
    "AMD":"0000002488","PEP":"0000077476","TMO":"0000097745",
    "MCD":"0000063754","CSCO":"0000858877","WFC":"0000072971",
    "IBM":"0000051143","GE":"0000040987","ABT":"0000001800",
    "CAT":"0000018230","VZ":"0000732717","HON":"0000773840",
    "NEE":"0000753308","GS":"0000886982","MS":"0000895421",
    "BLK":"0001364742","AXP":"0000004962","RTX":"0000101829",
    "QCOM":"0000804328","TXN":"0000097476","AMGN":"0000820081",
    "T":"0000732717","SPGI":"0000064040","BKNG":"0001075531",
    "MU":"0000723125","KLAC":"0000319201","AMAT":"0000796343",
    "LRCX":"0000707549","ADI":"0000006951","INTC":"0000050863",
    "BA":"0000012927","NKE":"0000320187","DIS":"0001744489",
    "BNY":"0001390777","NFLX":"0001065280","CVX":"0000093410",
    "XOM":"0000034088","NEE":"0000753308","LIN":"0000060714",
    "ABBV":"0001551152","NKE":"0000320187","SLB":"0000087347",
    "HON":"0000773840","CAT":"0000018230","MCD":"0000063754",
    "LLY":"0000059478","JNJ":"0000200406","PFE":"0000078003",
    "MRK":"0000310158","BMY":"0000014272","AMGN":"0000820081",
    "GILD":"0000882184","TMO":"0000097745","DHR":"0000313616",
    "ABT":"0000001800","MDT":"0001613103","SYK":"0000310764",
    "UNH":"0000072971","ELV":"0001071739","CI":"0001739940",
    "WBA":"0000945114","CVS":"0000064803","HCA":"0000860730",
    "DIS":"0001744489","CMCSA":"0001166691","PARA":"0000813828",
    "VZ":"0000732717","CHTR":"0001091907","T":"0000732717",
    "WM":"0000823768","RSG":"0001060349","RTX":"0000101829",
    "LMT":"0000936395","NOC":"0001133421","GD":"0000040533",
    "BA":"0000012927","UPS":"0001090727","FDX":"0000230056",
    "DE":"0000315189","EMR":"0000032604","ETN":"0000031462",
    "COP":"0001163165","EOG":"0000821189","OXY":"0000797468",
    "HAL":"0000045012","BKR":"0001710708","DVN":"0001090012",
    "SO":"0000092122","DUK":"0001326160","D":"0000715957",
    "AEP":"0000004904","EXC":"0001109357","XEL":"0000072741",
    "PCG":"0001004440","ED":"0001047862","WEC":"0000783325",
}


def set_identity(identity: str) -> None:
    """Set SEC User-Agent. Required — SEC blocks requests without it."""
    _HEADERS["User-Agent"] = identity


def fetch_xbrl_debt(ticker: str, filing_date: str) -> dict | None:
    """
    Fetch total debt, maturity schedule, and interest expense from SEC XBRL.

    Returns dict:
        total_debt_m      : float — total debt in millions
        current_debt_m    : float | None
        noncurrent_debt_m : float | None
        maturities        : {year_label: float_millions}
        interest_expense_m: float | None — annual interest expense in millions
        source            : str
    Or None if no useful data found.
    """
    cik = _get_cik(ticker)
    if not cik:
        logger.debug("xbrl_debt: no CIK for %s", ticker)
        return None

    facts = _get_facts(cik)
    if not facts:
        return None

    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    if not us_gaap:
        return None

    try:
        filing_year = int(str(filing_date)[:4])
    except Exception:
        filing_year = datetime.date.today().year

    # ── 1. Total debt waterfall ────────────────────────────────────────────────
    noncurrent_m = _waterfall(us_gaap, _LT_NONCURRENT, filing_date, scale=1e-6)
    current_m    = _waterfall(us_gaap, _LT_CURRENT,    filing_date, scale=1e-6)
    st_m         = _waterfall(us_gaap, _ST_BORROWINGS,  filing_date, scale=1e-6)

    # LongTermDebt sometimes already includes current portion (total LT debt).
    # Only add current_m if it's not already embedded in noncurrent_m.
    # Heuristic: if current_m is >5% of noncurrent_m, it's likely separate.
    if noncurrent_m is not None and current_m is not None:
        ratio = current_m / noncurrent_m if noncurrent_m > 0 else 0
        # If current is very small relative to non-current, it may already be included
        # Use maturity sum to resolve ambiguity later via cross-check
        total_debt_m = noncurrent_m + current_m + (st_m or 0)
    elif noncurrent_m is not None:
        total_debt_m = noncurrent_m + (st_m or 0)
    elif current_m is not None:
        total_debt_m = current_m + (st_m or 0)
    else:
        total_debt_m = (st_m or None)

    # ── 1e. Gross-total cross-check ───────────────────────────────────────────
    # Some filers (e.g. LLY) only tag LongTermDebtNoncurrent (= carrying value
    # after discount, non-current only) and do NOT separately tag
    # LongTermDebtCurrent. The gross-total tags below sometimes capture the
    # all-in face value (current + non-current). If gross > our computed total,
    # use it — it means we missed the current portion.
    gross_m = _waterfall(us_gaap, _GROSS_TOTAL, filing_date, scale=1e-6)
    if gross_m is not None and gross_m > (total_debt_m or 0):
        logger.info("xbrl_debt: %s gross total $%.0fM > waterfall $%.0fM — using gross",
                    ticker, gross_m, total_debt_m or 0)
        total_debt_m = gross_m

    # ── 2. Maturity schedule ───────────────────────────────────────────────────
    maturities: dict[str, float] = {}
    for concept, offset in _MATURITY_CONCEPTS:
        val_m = _waterfall(us_gaap, [concept], filing_date, scale=1e-6)
        if val_m is None or val_m <= 0:
            continue
        if offset == 0:
            year_label = str(filing_year)       # remainder of current year
        elif offset <= 5:
            year_label = str(filing_year + offset)
        else:
            year_label = f"{filing_year + 6} and thereafter"
        maturities[year_label] = val_m

    # ── 3. Cross-check: use maturity sum if > carrying value ──────────────────
    maturity_sum = sum(maturities.values()) if maturities else 0
    if maturity_sum > (total_debt_m or 0):
        logger.info("xbrl_debt: %s maturity sum $%.0fM > carrying $%.0fM — using sum",
                    ticker, maturity_sum, total_debt_m or 0)
        total_debt_m = maturity_sum

    # ── 4. Interest expense waterfall ─────────────────────────────────────────
    ie_m = _waterfall(us_gaap, _INTEREST_EXPENSE, filing_date, scale=1e-6,
                      take_abs=True)

    if total_debt_m is None and not maturities and ie_m is None:
        return None

    result = {
        "total_debt_m":       total_debt_m,
        "current_debt_m":     current_m,
        "noncurrent_debt_m":  noncurrent_m,
        "maturities":         maturities,
        "interest_expense_m": ie_m,
        "source":             f"SEC XBRL (CIK {cik})",
    }

    logger.info("xbrl_debt: %s — total=$%.0fM  maturities=%d  ie=$%.0fM",
                ticker, total_debt_m or 0, len(maturities), ie_m or 0)
    return result


def _waterfall(us_gaap: dict, concepts: list[str], filing_date: str,
               scale: float = 1.0, take_abs: bool = False) -> float | None:
    """Try concepts in order, return first non-None value scaled."""
    for concept in concepts:
        raw = _latest_value(us_gaap, concept, filing_date)
        if raw is not None:
            val = raw * scale
            return abs(val) if take_abs else val
    return None


def _latest_value(us_gaap: dict, concept: str,
                  filing_date: str) -> float | None:
    """Most recent 10-K value for a concept on or before filing_date."""
    data = us_gaap.get(concept)
    if not data:
        return None

    units = data.get("units", {})
    for unit_key in ("USD", "pure"):
        entries = units.get(unit_key)
        if not entries:
            continue

        annual = [e for e in entries
                  if e.get("form") in ("10-K", "10-K/A")
                  and e.get("end", "") <= filing_date]
        if not annual:
            annual = [e for e in entries
                      if e.get("form") in ("10-K", "10-K/A")]
        if not annual:
            continue

        annual.sort(key=lambda x: x.get("end", ""), reverse=True)
        val = annual[0].get("val")
        if val is not None:
            return float(val)

    return None


def _get_cik(ticker: str) -> str | None:
    ticker = ticker.upper()
    if ticker in _cik_cache:
        return _cik_cache[ticker]
    if not _cik_cache:
        _load_cik_mapping()
    return _cik_cache.get(ticker) or _CIK_FALLBACK.get(ticker)


def _load_cik_mapping() -> None:
    if not _HEADERS.get("User-Agent"):
        logger.warning("xbrl_debt: no User-Agent — call set_identity() first")
        return
    try:
        r = requests.get(_TICKERS_URL, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        for entry in r.json().values():
            t = (entry.get("ticker") or "").upper()
            if t:
                _cik_cache[t] = str(entry["cik_str"]).zfill(10)
        logger.info("xbrl_debt: loaded %d CIK mappings", len(_cik_cache))
    except Exception as e:
        logger.warning("xbrl_debt: CIK mapping fetch failed — %s", e)


def _get_facts(cik: str) -> dict | None:
    if cik in _facts_cache:
        return _facts_cache[cik]
    try:
        url = _FACTS_BASE.format(cik=cik)
        r   = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            _facts_cache[cik] = data
            return data
        logger.debug("xbrl_debt: HTTP %d for CIK %s", r.status_code, cik)
    except Exception as e:
        logger.warning("xbrl_debt: fetch error CIK %s — %s", cik, e)
    return None
