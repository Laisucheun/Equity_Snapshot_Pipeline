"""
facts_processor.py -- SEC EDGAR Company Facts API data processor

Bypasses edgartools' single-filing limitation by querying the company
facts API directly.  Returns 10-25 years of annual financial data from
a single HTTP call per ticker.

Drop-in compatible with RobustDataProcessor.  Produces the same
financials_payload dict format so CompanyFinancialProfile works unchanged.

Architecture
------------
Source: data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
One HTTP call returns ALL historical XBRL data for a company.

For each line item (Revenue, NetIncomeLoss, etc.):
  1. Try raw XBRL concepts in priority order (waterfall)
  2. Extract all annual (10-K) values across all filed periods
  3. Deduplicate: keep most recently filed for same period-end date
  4. Produce a DataFrame row with standard_concept matching edgartools labels

Output DataFrames have the same shape as edgartools' xbrl().statements
output, so existing StatementProfile / CompanyFinancialProfile classes
work without modification -- they just see more period columns.

Usage
-----
    from data.facts_processor import FactsDataProcessor

    proc = FactsDataProcessor("AAPL")
    if proc.load_data(max_years=10):
        profile = CompanyFinancialProfile(
            ticker="AAPL",
            sector="Technology",
            market_cap=proc.market_cap,
            financials_payload=proc.financials,
        )
        # profile.periods now has up to 10 years instead of 3
"""

import json
import logging
import datetime
import requests
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# SEC API configuration
# -----------------------------------------------------------------------------

_FACTS_URL   = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_TIMEOUT     = 25
_HEADERS     = {"User-Agent": ""}

_cik_cache:   dict[str, list[str]] = {}
_facts_cache: dict[str, dict]      = {}

# Below this many us-gaap concept keys, a CIK match is treated as a
# shell/subsidiary registrant rather than the primary operating filer.
_MIN_US_GAAP_CONCEPTS = 50

# Manual CIK corrections, verified against SEC EDGAR directly.
# Used when SEC's company_tickers.json maps a ticker to the wrong entity
# (a subsidiary/shelf registrant) or omits it entirely.
_CIK_OVERRIDES = {
    # Wrong entity in SEC ticker JSON — maps to subsidiary/shelf entity
    "XOM":   "34088",      # Exxon Mobil Corp (was mapping to Holdings subsidiary)

    # Missing from SEC ticker JSON — correct CIK confirmed via SEC filings
    "ANSS":  "1013462",    # ANSYS Inc (delisted Feb 2025 after Synopsys acquisition — use last 10-K)
    "BK":    "1390777",    # Bank of New York Mellon Corp
    "DFS":   "1393612",    # Discover Financial Services (acquired by CapOne 2024 — use last 10-K)
    "EA":    "712515",     # Electronic Arts Inc
    "FI":    "798354",     # Fiserv Inc
    "MMC":   "62709",      # Marsh & McLennan Companies
    "MRO":   "101778",     # Marathon Oil Corp (acquired by ConocoPhillips 2024 — use last 10-K)
    "PTVE":  "1527508",    # Pactiv Evergreen Inc
    "WBA":   "1618921",    # Walgreens Boots Alliance (acquired by Sycamore 2025 — use last 10-K)
    "CTLT":  "1596783",    # Catalent Inc (acquired by Novo Holdings Dec 2024)
    "DAY":   "1725057",    # Dayforce Inc (formerly Ceridian, acquired by Thoma Bravo 2025)
    "EQR":   "906107",     # Equity Residential
    "HES":   "4447",       # Hess Corporation (acquired by Chevron Oct 2024)
    "HOLX":  "859737",     # Hologic Inc (acquired by Blackstone/TPG 2026)
    "IPG":   "51644",      # Interpublic Group (acquired by Omnicom Nov 2025)
    "JNPR":  "1043604",    # Juniper Networks (acquired by HPE Jan 2026)
    "PKI":   "31791",      # PerkinElmer Inc
    "BCR":   "9892",       # C.R. Bard Inc (acquired by BD 2017 -- last 10-K FY2016, stale)
    "CTRA":  "858470",     # Coterra Energy Inc (formerly Cabot Oil & Gas CIK)
    "FLT":   "1175454",    # Corpay Inc, formerly FleetCor Technologies -- ticker now CPAY
    "K":     "55067",      # Kellanova, formerly Kellogg Co (acquired by Mars Oct 2024)
    "WRK":   "1732845",    # WestRock Co (merged into Smurfit WestRock Jul 2024 -- ticker now SW)

    # Standard known overrides
    "GOOGL": "1652044",    # Alphabet Inc Class A
    "GOOG":  "1652044",    # Alphabet Inc Class C (same entity)
    "BRK-B": "1067983",    # Berkshire Hathaway Inc
    "BRK-A": "1067983",    # Berkshire Hathaway Inc
}

# Tickers known to be delisted/acquired. The last available 10-K is still
# useful for historical analysis, so _get_cik() warns rather than failing.
KNOWN_DELISTINGS = {
    "BCR":  "C.R. Bard — acquired by Becton Dickinson (2017) — too old, skip",
    "CTRA": "Coterra Energy — acquired by Devon Energy (May 2026) — very recently delisted",
    "CTLT": "Catalent — acquired by Novo Holdings (Dec 2024) — last 10-K available",
    "DAY":  "Dayforce — acquired by Thoma Bravo (2025) — last 10-K available",
    "FLT":  "FleetCor/Corpay — rebranded; check if filing under new CIK",
    "HES":  "Hess Corp — acquired by Chevron (Oct 2024) — last 10-K available",
    "HOLX": "Hologic — acquired by Blackstone/TPG (2026) — last 10-K available",
    "IPG":  "Interpublic Group — acquired by Omnicom (Nov 2025) — last 10-K available",
    "JNPR": "Juniper Networks — acquired by HPE (Jan 2026) — last 10-K available",
    "K":    "Kellanova — acquired by Mars (Oct 2024) — delisted, last 10-K FY2023",
    "MRO":  "Marathon Oil — acquired by ConocoPhillips (Nov 2024) — last 10-K available",
    "WBA":  "Walgreens Boots Alliance — acquired by Sycamore (Aug 2025) — last 10-K available",
    "WRK":  "WestRock — merged into Smurfit WestRock (Jul 2024) — delisted",
}

# Tickers that file 20-F (foreign private issuers) rather than 10-K.
# _get_cik() notes this so the pipeline routes to the 20-F ingestion path.
KNOWN_20F_FILERS = {
    "ASML": "ASML Holding NV (Netherlands) — files 20-F not 10-K",
    "AZN":  "AstraZeneca PLC (UK) — files 20-F not 10-K",
    "CCEP": "Coca-Cola Europacific Partners PLC — files 20-F not 10-K",
    "GFS":  "GlobalFoundries Inc — files 20-F not 10-K",
}


def set_identity(identity: str) -> None:
    """Set SEC User-Agent.  Required -- SEC blocks requests without it."""
    _HEADERS["User-Agent"] = identity


# -----------------------------------------------------------------------------
# CIK + facts helpers (same pattern as xbrl_debt_fetcher.py)
# -----------------------------------------------------------------------------

def _load_cik_mapping() -> None:
    if not _HEADERS.get("User-Agent"):
        logger.warning("facts_processor: no User-Agent -- call set_identity()")
        return
    try:
        r = requests.get(_TICKERS_URL, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        for entry in r.json().values():
            t = (entry.get("ticker") or "").upper()
            if t:
                cik = str(entry["cik_str"]).zfill(10)
                bucket = _cik_cache.setdefault(t, [])
                if cik not in bucket:
                    bucket.append(cik)
        logger.info("facts_processor: loaded %d CIK mappings", len(_cik_cache))
    except Exception as e:
        logger.warning("facts_processor: CIK fetch failed -- %s", e)


def _get_cik(ticker: str) -> str | None:
    """
    Resolve ticker -> CIK.

    Check order:
      1. KNOWN_DELISTINGS -- print a warning and proceed (last 10-K is
         still useful for historical analysis).
      2. KNOWN_20F_FILERS -- print a note that this filer uses 20-F.
      3. _CIK_OVERRIDES -- return the verified override CIK directly.
      4. Normal resolution against SEC's company_tickers.json.

    SEC's company_tickers.json occasionally maps more than one CIK to the
    same ticker symbol (e.g. a shelf/subsidiary registrant sharing the
    parent's ticker). When a ticker has multiple CIK candidates, fetch
    company facts for each and select the one with the most us-gaap
    concept keys -- the primary operating filer. A candidate resolving to
    fewer than _MIN_US_GAAP_CONCEPTS concepts is treated as a shell entity
    and the next candidate is tried before settling on a final choice.
    """
    ticker = ticker.upper()

    if ticker in KNOWN_DELISTINGS:
        print(f"[facts_processor] NOTE: {ticker} is delisted -- {KNOWN_DELISTINGS[ticker]}. "
              f"Using last available 10-K data for historical analysis.")

    if ticker in KNOWN_20F_FILERS:
        print(f"[facts_processor] NOTE: {ticker} is a 20-F filer -- {KNOWN_20F_FILERS[ticker]}. "
              f"Pipeline will use the 20-F ingestion path.")

    if ticker in _CIK_OVERRIDES:
        return _CIK_OVERRIDES[ticker].zfill(10)

    if not _cik_cache:
        _load_cik_mapping()

    candidates = _cik_cache.get(ticker)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    best_cik, best_count = candidates[0], -1
    for cik in candidates:
        facts = _get_facts(cik)
        count = len(facts.get("facts", {}).get("us-gaap", {})) if facts else 0
        if count > best_count:
            best_cik, best_count = cik, count

    return best_cik


def _get_facts(cik: str) -> dict | None:
    if cik in _facts_cache:
        return _facts_cache[cik]
    try:
        url = _FACTS_URL.format(cik=cik)
        r   = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            # Merge dei: namespace share counts into us-gaap facts so
            # _extract_annual can resolve EntityCommonStockSharesOutstanding
            # and other dei: concepts without special-casing.
            dei = data.get("facts", {}).get("dei", {})
            if dei:
                us_gaap = data.setdefault("facts", {}).setdefault("us-gaap", {})
                for concept, cdata in dei.items():
                    # Only merge share-count and identity concepts; skip text fields
                    units = cdata.get("units", {})
                    if "shares" in units or "pure" in units:
                        key = f"dei_{concept}"  # prefix to avoid collisions
                        if key not in us_gaap:
                            us_gaap[key] = cdata
            _facts_cache[cik] = data
            return data
        logger.debug("facts_processor: HTTP %d for CIK %s", r.status_code, cik)
    except Exception as e:
        logger.warning("facts_processor: fetch error CIK %s -- %s", cik, e)
    return None


# -----------------------------------------------------------------------------
# Core extraction: company facts blob -> {period_end: value} per concept
# -----------------------------------------------------------------------------

_ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A"}

# Concepts where SEC's XBRL companyfacts API can return a segment/product-line
# dimensional fact alongside the consolidated total under the same raw concept
# name (companyfacts does not expose dimensional context, so both look
# identical apart from value). For these concepts, duplicate same-period-end
# entries are disambiguated by magnitude (largest wins) instead of filing
# recency -- safe because a segment/component subtotal can never exceed the
# consolidated total it rolls up into.
_MAX_MAGNITUDE_CONCEPTS = {
    # Revenue family — consolidated total always largest
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "Revenue",
    "RevenueFromRelatedParties",
    "RevenueFromContractsWithCustomers",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
    "RealEstateRevenueNet",
    "OperatingLeasesIncomeStatementLeaseRevenue",
    "OperatingLeaseLeaseIncome",
    "RegulatedAndUnregulatedOperatingRevenue",
    "ElectricUtilityRevenue",
    "RegulatedUtilityRevenue",

    # COGS family — consolidated total always largest
    "CostOfGoodsSold",
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization",
    "CostOfServices",
}


def _is_annual_period(entry: dict) -> bool:
    """
    True if a raw XBRL fact entry represents a full annual (~12-month) period.

    Duration is checked FIRST and is authoritative whenever both start/end
    are present. `fp` (fiscal period focus) is a FILING-level tag, not a
    per-fact one -- it reflects which period the filing as a whole covers,
    not the duration of this specific fact. A prior version of this check
    trusted fp in ('FY', 'CY') unconditionally, which let quarterly facts
    through: REIT 10-Ks commonly include a "selected quarterly financial
    data" footnote table, and every fact in it inherits fp='FY' from the
    encompassing FY filing despite spanning ~90 days. Confirmed live for
    AMT: its FY2024 10-K (filed 2025-02-25) contains Revenues entries for
    2024-01-01..2024-03-31, 2024-04-01..2024-06-30, and 2024-07-01..
    2024-09-30, all three tagged fp='FY' -- these were being accepted as
    annual periods, contaminating period discovery with quarterly dates
    for AMT and SPG.

    340-380 days covers standard calendar years (365), leap years (366),
    and 52/53-week fiscal years (357/364/371) with margin.

    fp is used only as a fallback when no start date is present (some CF
    non-cash add-backs are filed duration-less, fp='FY' only).
    """
    start = entry.get("start")
    end   = entry.get("end")
    fp    = entry.get("fp", "")

    if start and end:
        try:
            days = (datetime.date.fromisoformat(end)
                    - datetime.date.fromisoformat(start)).days
            return 340 <= days <= 380
        except (ValueError, TypeError):
            pass  # unparseable -- fall through to fp fallback below

    if fp in ("FY", "CY"):
        return True
    return False


def _extract_annual(us_gaap: dict, concept: str, unit: str = "USD",
                    is_instant: bool = False,
                    use_max_magnitude: bool = False) -> dict[str, float]:
    """
    Extract all annual values for a single raw XBRL concept.

    Returns {period_end_date: value} sorted newest-first.
    Deduplicates: if same period_end appears in both a 10-K and a 10-K/A,
    keeps the most recently filed entry (the amendment supersedes) --
    unless use_max_magnitude is True, in which case same-period-end
    duplicates are resolved by largest absolute value instead (see
    _MAX_MAGNITUDE_CONCEPTS).

    Parameters
    ----------
    is_instant : True for balance-sheet (point-in-time) concepts.
                 False for income-statement / cash-flow (duration) concepts --
                 adds a day-count filter (300-400 days) to exclude quarterly
                 entries that appear under a 10-K form.
    use_max_magnitude : When True, same-period-end duplicate entries are
                 disambiguated by largest absolute value rather than most
                 recently filed -- used for revenue/COGS-family concepts
                 where a segment-level fact can share a concept name with
                 the consolidated total.
    """
    data = us_gaap.get(concept)
    if not data:
        return {}

    entries = data.get("units", {}).get(unit, [])
    if not entries:
        return {}

    # Collect: end_date -> (filed_date, value)
    # Keep most recently filed for each period end
    best: dict[str, tuple[str, float]] = {}

    for e in entries:
        form = e.get("form", "")
        if form not in _ANNUAL_FORMS:
            continue

        end   = e.get("end", "")
        filed = e.get("filed", "")
        val   = e.get("val")
        if val is None or not end:
            continue

        # Duration filter: IS/CF entries must span ~1 year. See
        # _is_annual_period() -- duration is authoritative when start/end
        # are both present; fp is a filing-level tag, not a per-fact one,
        # and is only trusted as a fallback with no start date.
        if not is_instant and not _is_annual_period(e):
            continue

        # Dedup: keep most recently filed for same period end, or the
        # largest absolute value when use_max_magnitude is True (segment
        # vs. consolidated duplicates under the same concept name).
        if use_max_magnitude:
            if end not in best or abs(float(val)) > abs(best[end][1]):
                best[end] = (filed, float(val))
        else:
            if end not in best or filed > best[end][0]:
                best[end] = (filed, float(val))

    return {end: v for end, (_, v) in
            sorted(best.items(), key=lambda x: x[0], reverse=True)}



# -----------------------------------------------------------------------------
# Phase 1: Load edgartools gaap_mappings for sector overrides + company patches
# -----------------------------------------------------------------------------

import importlib.util as _iutil
import os as _os

def _load_gaap_mappings() -> dict:
    """Load gaap_mappings.json from edgartools package or local copy."""
    candidates = []

    # 1. edgartools install: find_spec returns the __init__.py path;
    #    dirname gives the package root on ALL platforms (Windows + Unix)
    try:
        spec = _iutil.find_spec("edgar")
        if spec and spec.origin:
            pkg_dir = _os.path.dirname(spec.origin)
            candidates.append(
                _os.path.join(pkg_dir, "xbrl", "standardization", "gaap_mappings.json")
            )
    except Exception:
        pass

    # 2. Same directory as facts_processor.py (user-copied file)
    candidates.append(
        _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "gaap_mappings.json")
    )

    # 3. Current working directory
    candidates.append("gaap_mappings.json")

    for path in candidates:
        if path and _os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                logger.info("facts_processor: loaded gaap_mappings.json (%d concepts) from %s",
                            len(data), path)
                return data
            except Exception as e:
                logger.warning("facts_processor: failed to load %s -- %s", path, e)

    logger.warning("facts_processor: gaap_mappings.json not found -- sector overrides disabled. "
                   "Copy gaap_mappings.json to your project directory or install edgartools.")
    return {}


def _load_company_mappings() -> dict[str, dict]:
    """
    Load per-ticker company_mappings/*.json from edgartools.
    Returns {ticker_upper: {raw_concept: [standard_tags]}}
    """
    result: dict[str, dict] = {}
    cm_dir = None
    if _iutil.find_spec("edgar"):
        edgar_dir = _os.path.dirname(_iutil.find_spec("edgar").origin)
        cm_dir = _os.path.join(edgar_dir, "xbrl", "standardization", "company_mappings")
    if not cm_dir or not _os.path.isdir(cm_dir):
        return result

    for fname in _os.listdir(cm_dir):
        if not fname.endswith("_mappings.json"):
            continue
        ticker = fname.replace("_mappings.json", "").upper()
        try:
            data = json.load(open(_os.path.join(cm_dir, fname)))
            concept_map = data.get("concept_mappings", {})
            # Flatten: standard_label -> [raw_concepts]
            flat: dict[str, list[str]] = {}
            for std_label, raw_list in concept_map.items():
                if std_label.startswith("_"):
                    continue
                if isinstance(raw_list, list):
                    flat[std_label] = [c for c in raw_list if not c.startswith("_")]
            if flat:
                result[ticker] = flat
        except Exception:
            pass
    logger.info("facts_processor: loaded company mappings for %s", list(result.keys()))
    return result


def _build_sector_overrides(gaap_mappings: dict) -> dict[str, dict[str, list[str]]]:
    """
    Build {pipeline_sector: {standard_tag: [raw_concepts_priority]}} from
    gaap_mappings industry_overrides, using only high-confidence overrides (>=0.70).
    """
    from collections import defaultdict

    IND_TO_SECTOR = {
        "Banks": "Financial Services", "Fin": "Financial Services",
        "Insur": "Financial Services",
        "RlEst": "Real Estate",
        "Oil": "Energy",
        "Util": "Utilities",
        "Drugs": "Healthcare", "Hlth": "Healthcare", "MedEq": "Healthcare",
        "Comps": "Technology", "Chips": "Technology", "BusSv": "Technology",
        "Rtail": "Consumer Discretionary", "Meals": "Consumer Discretionary",
        "Autos": "Consumer Discretionary",
        "Food": "Consumer Staples", "Hshld": "Consumer Staples",
        "Trans": "Industrials", "Mach": "Industrials", "Aero": "Industrials",
        "Chems": "Materials", "Mines": "Materials", "Gold": "Materials",
        "Steel": "Materials",
        "Telcm": "Communication Services",
    }

    # {sector: {tag: [(raw_concept, count, override_conf)]}}
    acc = defaultdict(lambda: defaultdict(list))

    for raw_concept, meta in gaap_mappings.items():
        if "ifrs" in raw_concept.lower():
            continue
        count = meta.get("company_count", 0)
        base_conf = meta.get("confidence", 0)

        for ind_code, ov in meta.get("industry_overrides", {}).items():
            sector = IND_TO_SECTOR.get(ind_code)
            if not sector:
                continue
            ov_conf = ov.get("confidence", 0)
            ov_tags = ov.get("standard_tags", [])
            if ov_conf >= 0.70 or ov_conf > base_conf + 0.10:
                for tag in ov_tags:
                    existing = [x[0] for x in acc[sector][tag]]
                    if raw_concept not in existing:
                        acc[sector][tag].append((raw_concept, count, ov_conf))

    # Sort by (override_conf desc, count desc), return concept names only
    result: dict[str, dict[str, list[str]]] = {}
    for sector, tag_map in acc.items():
        result[sector] = {}
        for tag, items in tag_map.items():
            items.sort(key=lambda x: (x[2], x[1]), reverse=True)
            result[sector][tag] = [x[0] for x in items]

    # -- Remove inappropriate lease sub-components from non-REIT sectors --------
    # OperatingLeaseLeaseIncomeLeasePayments is a lease payment breakdown concept
    # that gaap_mappings incorrectly associates with Revenue for Banks/Fin.
    _LEASE_REVENUE_CONCEPTS = {
        "OperatingLeaseLeaseIncomeLeasePayments",
        "OperatingLeaseLeaseIncomeVariableLeaseIncome",
        "OperatingLeaseVariableLeaseIncome",
        "SubleaseIncome",
    }
    _REIT_SECTORS = {"Real Estate"}
    for sector, tag_map in result.items():
        if sector in _REIT_SECTORS:
            continue
        for tag in tag_map:
            tag_map[tag] = [c for c in tag_map[tag] if c not in _LEASE_REVENUE_CONCEPTS]

    # -- Hard-coded sector overrides not in gaap_mappings ---------------------
    # Real Estate: REIT rental income concepts take priority over ASC 606 revenue
    # Placed BEFORE RevenueFromContractWithCustomerExcludingAssessedTax so apartment
    # and tower REITs (AVB, EQR, AMT, CCI etc.) get total rental revenue, not
    # the small service-fee sub-component that ASC 606 picks up.
    result.setdefault("Real Estate", {})
    result["Real Estate"]["Revenue"] = [
        "OperatingLeaseLeaseIncome",                   # ASC 842 lease income (76 tickers)
        "OperatingLeasesIncomeStatementLeaseRevenue",  # ASC 840 lease income (51 tickers)
        "Revenues",                                    # Storage/diversified REITs (396 tickers)
        "RealEstateRevenueNet",                        # Diversified REITs (23 tickers)
    ] + [c for c in result["Real Estate"].get("Revenue", [])
         if c not in ("OperatingLeaseLeaseIncome",
                      "OperatingLeasesIncomeStatementLeaseRevenue",
                      "Revenues", "RealEstateRevenueNet")]

    # Financial Services: banks/insurers don't tag total revenue as Revenues.
    # InterestIncomeExpenseNet (143 tickers) is the best single-concept proxy
    # for banks. Prepended so it fires BEFORE RevenueFromContractWithCustomer.
    # Note: this will show NII for banks (correct) and may overstate for
    # diversified FS companies -- acceptable given no better single concept exists.
    result.setdefault("Financial Services", {})
    existing_fs_rev = result["Financial Services"].get("Revenue", [])
    result["Financial Services"]["Revenue"] = [
        "InterestIncomeExpenseNet",     # Banks: NII = net interest income (143 tickers)
        "RevenuesNetOfInterestExpense", # Banks: alternative NII concept (12 tickers)
        "PremiumsEarnedNet",            # Insurers: earned premiums (27 tickers)
    ] + [c for c in existing_fs_rev if c not in
         ("InterestIncomeExpenseNet", "RevenuesNetOfInterestExpense", "PremiumsEarnedNet")]

    return result


# -- Module-level initialisation -----------------------------------------------
_GAAP_MAPPINGS:     dict             = _load_gaap_mappings()
_COMPANY_MAPPINGS:  dict[str, dict]  = _load_company_mappings()
_SECTOR_OVERRIDES:  dict[str, dict]  = _build_sector_overrides(_GAAP_MAPPINGS)


# -----------------------------------------------------------------------------
# Fine-grained SIC-based industry routing (hybrid approach)
# -----------------------------------------------------------------------------

def _load_industry_mappings() -> dict:
    """Load SIC range → fine-grained industry from edgartools."""
    import importlib.util, json, pathlib
    spec = importlib.util.find_spec("edgar")
    if not spec:
        return {}
    edgar_dir = pathlib.Path(spec.origin).parent
    path = edgar_dir / "entity" / "data" / "industry_mappings.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


_INDUSTRY_MAPPINGS: dict = _load_industry_mappings()


def _sic_to_industry(sic: int | None) -> str | None:
    """
    Map SIC code to fine-grained industry name using edgartools ranges.

    Some SIC codes fall inside more than one industry's ranges (edgartools'
    own industry_mappings.json has overlapping ranges -- e.g. investment_companies
    covers 6720-6799, which also contains realestate's more specific 6798-6798
    for REITs). When multiple industries match, the narrowest (most specific)
    matching range wins, so a REIT like SPG (SIC 6798) resolves to "realestate"
    rather than the broader "investment_companies" bucket.
    """
    if not sic or not _INDUSTRY_MAPPINGS:
        return None
    best_industry = None
    best_width = None
    for industry, config in _INDUSTRY_MAPPINGS.get("industries", {}).items():
        for lo, hi in config.get("sic_ranges", []):
            if lo <= sic <= hi:
                width = hi - lo
                if best_width is None or width < best_width:
                    best_width = width
                    best_industry = industry
    return best_industry


# Fine-grained edgartools industry → pipeline sector taxonomy.
# Only industries with an unambiguous sector mapping are listed here --
# anything absent (e.g. "consumergoods", which edgartools does not split
# into staples vs. discretionary) falls through to the broad SIC-range
# mapping in universe/build_sec_universe.py instead of being guessed at.
#
# "payment_networks" deliberately maps to "Financial Technology", not
# "Financial Services" -- _sector_group() in core/agents.py explicitly
# carves fintech/payments out of the banking/insurance routing group, and
# a sector string containing "financial technology" hits that carve-out.
_FINE_INDUSTRY_TO_SECTOR = {
    "banking":              "Financial Services",
    "insurance":            "Financial Services",
    "securities":           "Financial Services",
    "investment_companies": "Financial Services",
    "payment_networks":     "Financial Technology",
    "realestate":           "Real Estate",
    "energy":               "Energy",
    "utilities":            "Utilities",
    "healthcare":           "Healthcare",
    "tech":                 "Technology",
    "semiconductors":       "Technology",
    "telecom":              "Communication Services",
    "transportation":       "Industrials",
    "aerospace":            "Industrials",
    "automotive":           "Consumer Discretionary",
    "mining":               "Materials",
    "retail":               "Consumer Discretionary",
    "hospitality":          "Consumer Discretionary",
}


def _infer_sector_from_sic(sic: int | None, fine_industry: str | None = None) -> str | None:
    """
    Map a SIC code to the pipeline's sector taxonomy, for dynamic sector
    routing that covers every SEC filer rather than only the tickers in a
    static lookup table.

    Priority: fine-grained edgartools industry (_sic_to_industry) first --
    it already resolves overlapping SIC ranges to the narrowest match (e.g.
    SIC 6798 -> "realestate" over the broader "investment_companies" bucket)
    -- then the broad SIC-range sector map from universe/build_sec_universe.py
    for anything not covered above. Returns None if the SIC code is missing
    or unmapped by either source (caller should fall back further).
    """
    if not sic:
        return None

    fine_industry = fine_industry if fine_industry is not None else _sic_to_industry(sic)
    mapped = _FINE_INDUSTRY_TO_SECTOR.get(fine_industry) if fine_industry else None
    if mapped:
        return mapped

    try:
        from universe.build_sec_universe import _sic_to_sector
    except Exception:
        return None
    broad = _sic_to_sector(sic)
    return broad if broad and broad != "General" else None


# Fine-grained industry → {standard_tag: [raw_concepts_priority]}.
# Checked ahead of the broad _SECTOR_OVERRIDES (which only carries generic
# ASC-606 tags reordered by confidence, not genuinely industry-specific
# concepts) but after per-company patches. See PART 2/3 of the diagnostic
# report this was designed from -- these tags are NOT present anywhere in
# gaap_mappings.json's industry_overrides or edgartools' industry_extensions
# trees, both of which lack a dedicated Revenue node for every one of these
# industries due to tagging fragmentation.
_INDUSTRY_CONCEPT_OVERRIDES = {

    "energy": {
        "Revenue": [
            "OilAndGasRevenue",
            "CrudeOilAndNaturalGasRevenue",
            "OilGasAndNGLsRevenue",
            "OilAndGasSalesRevenue",
            "NaturalGasRevenue",
            "OilAndCondensateRevenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "Revenue",
        ],
        "CostOfRevenue": [
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
            "CostOfGoodsSold",
            "ExplorationExpense",
            "DepletionOfOilAndGasProperties",
        ],
    },

    "utilities": {
        "Revenue": [
            "ElectricUtilityRevenue",
            "RegulatedUtilityRevenue",
            "GasUtilitiesRevenue",
            "WaterUtilitiesRevenue",
            "RegulatedAndUnregulatedOperatingRevenue",
            "PublicUtilitiesRevenueRequirementNet",
            "UtilitiesRevenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "Revenue",
        ],
        "CostOfRevenue": [
            "FuelAndPurchasedPowerCost",
            "UtilitiesOperatingExpenseFuelUsed",
            "ElectricityPurchased",
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
        ],
    },

    "banking": {
        "Revenue": [
            "InterestIncomeExpenseNet",
            "NetInterestIncome",
            "RevenuesNetOfInterestExpense",
            "InterestAndDividendIncomeOperating",
            "InterestAndFeeIncomeOtherLoans",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
        ],
        "CostOfRevenue": [
            "InterestExpense",
            "ProvisionForLoanLeaseAndOtherLosses",
            "ProvisionForLoanAndLeaseLosses",
        ],
    },

    "insurance": {
        "Revenue": [
            "PremiumsEarnedNet",
            "NetPremiumsEarned",
            "PremiumsWrittenNet",
            "InsurancePremiumsRevenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
        ],
        "CostOfRevenue": [
            "PolicyholderBenefitsAndClaimsIncurredNet",
            "BenefitsLossesAndExpenses",
        ],
    },

    "realestate": {
        "Revenue": [
            "OperatingLeaseLeaseIncome",
            "OperatingLeasesIncomeStatementLeaseRevenue",
            "RealEstateRevenueNet",
            "RentalProperties",
            "TenantReimbursements",
            "RentalIncome",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "Revenue",
        ],
        "CostOfRevenue": [
            "RealEstateTaxExpense",
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
        ],
    },

    "healthcare": {
        "Revenue": [
            "HealthCareOrganizationPatientServiceRevenue",
            "PharmaceuticalProductRevenue",
            "MedicalDeviceRevenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromCollaborativeArrangementExcludingRevenueFromContractWithCustomer",
            "Revenues",
            "Revenue",
        ],
        "CostOfRevenue": [
            "CostOfGoodsAndServicesSold",
            "CostOfGoodsSoldPharmaceutical",
            "CostOfRevenue",
        ],
    },

    "mining": {
        "Revenue": [
            "MetalsAndMiningRevenue",
            "MiningRevenue",
            "CoalRevenue",
            "MineralRoyaltiesRevenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "Revenue",
        ],
        "CostOfRevenue": [
            "MiningCosts",
            "CostOfCoalSold",
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
        ],
    },

    "transportation": {
        "Revenue": [
            "PassengerRevenue",
            "PassengerAirlineRevenue",
            "CargoRevenue",
            "AirlineRevenue",
            "TransportationRevenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "Revenue",
        ],
        "CostOfRevenue": [
            "AircraftFuelCosts",
            "LaborAndRelatedExpense",
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
        ],
    },

    "hospitality": {
        "Revenue": [
            "HotelRevenue",
            "OccupancyRevenue",
            "FoodAndBeverageRevenue",
            "GamingRevenue",
            "CasinoRevenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "Revenue",
        ],
        "CostOfRevenue": [
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
        ],
    },

    "telecom": {
        "Revenue": [
            "ServiceRevenue",
            "WirelessRevenue",
            "WirelineRevenue",
            "EquipmentRevenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "Revenue",
        ],
        "CostOfRevenue": [
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
            "CostOfServices",
        ],
    },

    "tech": {
        "Revenue": [
            "SubscriptionRevenue",
            "SoftwareLicenseRevenue",
            "LicenseRevenue",
            "ProfessionalServicesRevenue",
            "MaintenanceRevenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "Revenue",
        ],
        "CostOfRevenue": [
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
            "CostOfServices",
        ],
    },

    "semiconductors": {
        "Revenue": [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "Revenue",
        ],
        "CostOfRevenue": [
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
        ],
    },

    "retail": {
        "Revenue": [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "Revenue",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
        ],
        "CostOfRevenue": [
            "CostOfGoodsSold",
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
        ],
    },
}

# Add all new revenue tags to _MAX_MAGNITUDE_CONCEPTS -- same rationale as the
# existing set: a segment/product-line subtotal can never exceed the
# consolidated total it rolls up into, so duplicate same-period-end entries
# under these concepts are disambiguated by magnitude, not filing recency.
_MAX_MAGNITUDE_CONCEPTS.update({
    "OilAndGasRevenue", "CrudeOilAndNaturalGasRevenue",
    "OilGasAndNGLsRevenue", "OilAndGasSalesRevenue",
    "NaturalGasRevenue", "OilAndCondensateRevenue",
    "ElectricUtilityRevenue", "RegulatedUtilityRevenue",
    "GasUtilitiesRevenue", "WaterUtilitiesRevenue",
    "RegulatedAndUnregulatedOperatingRevenue",
    "PublicUtilitiesRevenueRequirementNet", "UtilitiesRevenue",
    "InterestIncomeExpenseNet", "NetInterestIncome",
    "RevenuesNetOfInterestExpense",
    "PremiumsEarnedNet", "NetPremiumsEarned", "PremiumsWrittenNet",
    "OperatingLeaseLeaseIncome", "RealEstateRevenueNet",
    "RentalProperties", "TenantReimbursements", "RentalIncome",
    "HealthCareOrganizationPatientServiceRevenue",
    "PharmaceuticalProductRevenue", "MedicalDeviceRevenue",
    "MetalsAndMiningRevenue", "MiningRevenue", "CoalRevenue",
    "PassengerRevenue", "PassengerAirlineRevenue",
    "CargoRevenue", "AirlineRevenue", "TransportationRevenue",
    "HotelRevenue", "OccupancyRevenue", "GamingRevenue",
    "ServiceRevenue", "WirelessRevenue", "WirelineRevenue",
    "SubscriptionRevenue", "SoftwareLicenseRevenue",
    "LicenseRevenue", "SalesRevenueNet", "SalesRevenueGoodsNet",
})

# REIT real-estate CapEx concepts -- same segment-subtotal-vs-consolidated
# risk as the revenue family above (a property-level or JV-level CapEx fact
# can share a concept name with the consolidated total for the same period).
_MAX_MAGNITUDE_CONCEPTS.update({
    "PaymentsToAcquireRealEstate",
    "PaymentsToDevelopRealEstateAssets",
})


# -----------------------------------------------------------------------------
# Waterfall definitions -- auto-generated from edgartools gaap_mappings.json
# 2,924 raw concepts -> 235 standard_tags, sorted by company_count desc
# conf >= 0.50, IFRS excluded, max 20 concepts per tag
# -----------------------------------------------------------------------------

_IS_WATERFALL = [
    ("AdvertisingExpense", [
        "AdvertisingExpense",
    ], "USD"),
    ("AmortizationOfIntangibles", [
        "AmortizationOfIntangibleAssets",
        "ImpairmentOfIntangibleAssetsFinitelived",
        "ImpairmentOfIntangibleAssetsExcludingGoodwill",
        "ImpairmentOfIntangibleAssetsIndefinitelivedExcludingGoodwill",
        "AmortisationExpense",
    ], "USD"),
    ("AssetImpairmentChargesIS", [
        "AssetImpairmentCharges",
        "GoodwillImpairmentLoss",
    ], "USD"),
    ("BadDebtExpense", [
        "AllowanceForDoubtfulAccountsReceivableWriteOffs",
    ], "USD"),
    ("CommissionsRevenue", [
        "BrokerageCommissionsRevenue",
        "CommissionsAndFees",
    ], "USD"),
    ("CommonDividendsPerShare", [
        "CommonStockDividendsPerShareDeclared",
        "CommonStockDividendsPerShareCashPaid",
        "DividendsPayableAmountPerShare",
    ], "USD/shares"),
    ("CommunicationAndTechnologyExpense", [
        "CommunicationsAndInformationTechnology",
    ], "USD"),
    ("CostOfGoodsAndServicesSold", [
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "LaborAndRelatedExpense",
        "CostOfSales",
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
        "OtherCostOfOperatingRevenue",
        "DirectOperatingCosts",
        "DirectCostsOfLeasedAndRentedPropertyOrEquipment",
        "CostDirectMaterial",
        "ExciseAndSalesTaxes",
        "FuelCosts",
        "RelatedPartyCosts",
        "CostDirectLabor",
        "CostOfOtherPropertyOperatingExpense",
        "CostOfPropertyRepairsAndMaintenance",
        "UtilitiesOperatingExpenseMaintenanceOperationsAndOtherCostsAndExpenses",
        "AffiliateCosts",
        "OperatingInsuranceAndClaimsCostsProduction",
        "ResultsOfOperationsTransportationCosts",
        "DirectTaxesAndLicensesCosts",
    ], "USD"),
    ("CostsSubtotal", [
        "CostsAndExpenses",
        "OtherNoncashExpense",
        "BenefitsLossesAndExpenses",
        "EmployeeBenefitsAndShareBasedCompensation",
    ], "USD"),
    ("CurrentIncomeTaxExpense", [
        "CurrentIncomeTaxExpenseBenefit",
        "CurrentTaxExpenseIncome",
        "CurrentFederalTaxExpenseBenefit",
    ], "USD"),
    ("DeferredIncomeTaxExpense", [
        "DeferredTaxExpenseIncome",
        "DeferredTaxExpenseIncomeRecognisedInProfitOrLoss",
        "DeferredFederalIncomeTaxExpenseBenefit",
        "DeferredStateAndLocalIncomeTaxExpenseBenefit",
    ], "USD"),
    ("DepreciationExpense", [
        "DepreciationDepletionAndAmortization",
        "Depreciation",
        "DepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "OtherDepreciationAndAmortization",
        "DepreciationAndAmortisationExpense",
        "DepreciationExpense",
        "DepreciationNonproduction",
        "CostOfGoodsAndServicesSoldDepreciationAndAmortization",
        "CostDepreciationAmortizationAndDepletion",
        "CostOfGoodsAndServicesSoldDepreciation",
        "ResultsOfOperationsDepreciationDepletionAndAmortizationAndValuationProvisions",
        "ResultsOfOperationsDepreciationDepletionAmortizationAndAccretion",
        "DepletionOfOilAndGasProperties",
        "CapitalizedComputerSoftwareAmortization",
    ], "USD"),
    ("DiscontinuedOperationsIncome", [
        "IncomeLossFromDiscontinuedOperationsNetOfTax",
        "IncomeLossFromDiscontinuedOperationsNetOfTaxAttributableToReportingEntity",
        "ProfitLossFromDiscontinuedOperations",
    ], "USD"),
    ("EarningsPerShareBasic", [
        "EarningsPerShareBasic",
        "IncomeLossFromContinuingOperationsPerBasicShare",
        "BasicEarningsLossPerShare",
        "EarningsPerShareBasicAndDiluted",
    ], "USD/shares"),
    ("EarningsPerShareDiluted", [
        "EarningsPerShareDiluted",
        "IncomeLossFromContinuingOperationsPerDilutedShare",
        "DilutedEarningsLossPerShare",
        "EarningsPerShareBasicAndDiluted",
    ], "USD/shares"),
    ("ElectricUtilityRevenue", [
        "RegulatedAndUnregulatedOperatingRevenue",
    ], "USD"),
    ("EquityMethodInvestmentIncome", [
        "IncomeLossFromEquityMethodInvestments",
        "ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod",
        "ShareOfProfitLossOfAssociatesAccountedForUsingEquityMethod",
    ], "USD"),
    ("ExtraordinaryItemsIncomeExpense(PostTax)", [
        "DiscontinuedOperationGainLossOnDisposalOfDiscontinuedOperationNetOfTax",
        "DiscontinuedOperationIncomeLossFromDiscontinuedOperationBeforeIncomeTax",
        "DiscontinuedOperationTaxEffectOfDiscontinuedOperation",
        "IncomeLossFromDiscontinuedOperationsNetOfTaxAttributableToNoncontrollingInterest",
        "DiscontinuedOperationIncomeLossFromDiscontinuedOperationDuringPhaseOutPeriodNetOfTax",
        "DiscontinuedOperationGainLossFromDisposalOfDiscontinuedOperationBeforeIncomeTax",
        "DiscontinuedOperationTaxEffectOfIncomeLossFromDisposalOfDiscontinuedOperation",
        "DiscontinuedOperationIncomeLossFromDiscontinuedOperationDuringPhaseOutPeriodBeforeIncomeTax",
        "DiscontinuedOperationProvisionForLossGainOnDisposalNetOfTax",
        "DiscontinuedOperationTaxEffectOfIncomeLossFromDiscontinuedOperationDuringPhaseOutPeriod",
        "DiscontinuedOperationAmountOfAdjustmentToPriorPeriodGainLossOnDisposalBeforeIncomeTax",
        "DiscontinuedOperationTaxExpenseBenefitFromProvisionForGainLossOnDisposal",
        "DiscontinuedOperationProvisionForLossGainOnDisposalBeforeIncomeTax",
        "DisposalGroupIncludingDiscontinuedOperationOperatingIncomeLoss",
        "DiscontinuedOperationAmountOfAdjustmentToPriorPeriodGainLossOnDisposalNetOfTax",
        "DisposalGroupIncludingDiscontinuedOperationGrossProfitLoss",
        "DisposalGroupIncludingDiscontinuedOperationOperatingExpense",
        "DiscontinuedOperationTaxEffectOfAdjustmentToPriorPeriodGainLossOnDisposal",
        "DisposalGroupIncludingDiscontinuedOperationCostsOfGoodsSold",
        "DisposalGroupIncludingDiscontinuedOperationGeneralAndAdministrativeExpense",
    ], "USD"),
    ("FinanceLeaseExpense", [
        "FinanceLeaseRightOfUseAssetAmortization",
    ], "USD"),
    ("FinancialServicesRevenue", [
        # Banks: NII is the closest single-concept proxy for total revenue
        "InterestIncomeExpenseNet",
        # Insurers
        "PremiumsEarnedNet",
        "BenefitsClaimsAndLossesIncurred",
        # Brokers/asset managers
        "RevenueOtherFinancialServices",
        "ManagementFeesRevenue",
        "FeesAndCommissions",
        "BrokerageCommissionsRevenue",
        "InvestmentBankingRevenue",
        "NetInvestmentIncome",
        # Catch-all
        "NoninterestIncome",
        "FinancialServicesRevenue",
    ], "USD"),
    ("ForeignCurrencyGainLoss", [
        "ForeignCurrencyTransactionGainLossBeforeTax",
        "ForeignExchangeLoss",
        "NetForeignExchangeGain",
        "ForeignExchangeGain",
        "NetForeignExchangeLoss",
    ], "USD"),
    ("GainLossOnDispositions", [
        "GainLossOnSaleOfPropertyPlantEquipment",
        "GainLossOnSaleOfBusiness",
        "GainsLossesOnDisposalsOfNoncurrentAssets",
        "GainsOnDisposalsOfNoncurrentAssets",
    ], "USD"),
    ("GainLossOnInvestmentsIS", [
        "GainLossOnInvestments",
    ], "USD"),
    ("GoodwillWriteoffs", [
        "GoodwillAndIntangibleAssetImpairment",
        "AdjustmentForAmortization",
        "TangibleAssetImpairmentCharges",
        "CostOfGoodsAndServicesSoldAmortization",
        "ImpairmentLossRecognisedInProfitOrLoss",
        "ResultsOfOperationsImpairmentOfOilAndGasProperties",
        "UnamortizedCostsCapitalizedLessRelatedDeferredIncomeTaxesExceedCeilingLimitationExpense",
    ], "USD"),
    ("GrossProfit", [
        "GrossProfit",
    ], "USD"),
    ("IncomeLossContinuingOperations", [
        "IncomeLossFromContinuingOperations",
        "ProfitLossFromContinuingOperations",
    ], "USD"),
    ("IncomeTaxes", [
        "IncomeTaxExpenseBenefit",
        "IncomeTaxesPaidNet",
        "DeferredIncomeTaxExpenseBenefit",
        "IncomeTaxExpenseContinuingOperations",
        "ValuationAllowanceDeferredTaxAssetChangeInAmount",
        "FederalIncomeTaxExpenseBenefitContinuingOperations",
        "OtherTaxExpenseBenefit",
        "CurrentStateAndLocalTaxExpenseBenefit",
        "AdjustmentsToAdditionalPaidInCapitalTaxEffectFromShareBasedCompensation",
        "EmployeeServiceShareBasedCompensationTaxBenefitFromCompensationExpense",
        "TaxAdjustmentsSettlementsAndUnusualProvisions",
        "DeferredFederalStateAndLocalTaxExpenseBenefit",
        "ForeignIncomeTaxExpenseBenefitContinuingOperations",
        "StateAndLocalIncomeTaxExpenseBenefitContinuingOperations",
        "CurrentForeignTaxExpenseBenefit",
        "UnrecognizedTaxBenefitsIncomeTaxPenaltiesAndInterestExpense",
        "IncomeTaxExpenseBenefitContinuingOperationsAdjustmentOfDeferredTaxAssetLiability",
        "IncomeTaxReconciliationTaxCreditsResearch",
        "TaxCutsAndJobsActOf2017IncomeTaxExpenseBenefit",
        "CurrentFederalStateAndLocalTaxExpenseBenefit",
    ], "USD"),
    ("InterestAndDividendIncome", [
        "InvestmentIncomeInterest",
        "InvestmentIncomeInterestAndDividend",
        "InterestAndDividendIncomeSecurities",
    ], "USD"),
    ("InterestExpense", [
        "InterestPaidNet",
        "InterestExpense",
        "GainsLossesOnExtinguishmentOfDebt",
        "InterestExpenseNonoperating",
        "AmortizationOfFinancingCosts",
        "AmortizationOfDebtDiscountPremium",
        "AmortizationOfFinancingCostsAndDiscounts",
        "InterestIncomeExpenseNonoperatingNet",
        "InterestExpenseOperating",
        "FinanceCosts",
        "InterestPaid",
        "InterestExpenseDebt",
        "InterestExpenseOther",
        "InterestAndDebtExpense",
        "InterestExpenseRelatedParty",
        "InterestExpenseBorrowings",
        "WriteOffOfDeferredDebtIssuanceCost",
        "InterestPaidCapitalized",
        "InterestExpenseSubordinatedNotesAndDebentures",
        "InterestExpenseShortTermBorrowings",
    ], "USD"),
    ("InterestExpenseDeposits", [
        "InterestExpenseDeposits",
    ], "USD"),
    ("InterestIncome", [
        "InterestIncomeOther",
        "FinanceIncome",
        "InterestAndOtherIncome",
        "OtherInterestAndDividendIncome",
        "InvestmentIncomeDividend",
        "InterestIncomeSecuritiesOtherUSGovernment",
        "InterestIncomeSecuritiesMortgageBacked",
        "InterestIncomeRelatedParty",
        "InterestIncomeSecuritiesUSTreasury",
        "InterestIncomeOtherDomesticDeposits",
        "InterestIncomeSecuritiesStateAndMunicipal",
        "InterestIncomeMoneyMarketDeposits",
        "InterestIncomeOperatingAndNonoperating",
        "LitigationSettlementInterest",
        "InterestIncomeForeignDeposits",
    ], "USD"),
    ("LaborExpenses", [
        "SalariesAndWages",
        "SalariesWagesAndOfficersCompensation",
    ], "USD"),
    ("LicensingRevenue", [
        "LicenseMember",
    ], "USD"),
    ("LossOnDebtExtinguishment", [
        "GainLossOnExtinguishmentOfDebt",
    ], "USD"),
    ("MarketingExpenses", [
        "MarketingAndAdvertisingExpense",
        "MarketingExpense",
        "CooperativeAdvertisingExpense",
    ], "USD"),
    ("MinorityInterestIncomeExpense", [
        "NetIncomeLossAttributableToNoncontrollingInterest",
        "ComprehensiveIncomeNetOfTaxAttributableToNoncontrollingInterest",
        "ProfitLossAttributableToNoncontrollingInterests",
        "NetIncomeLossAttributableToRedeemableNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsAttributableToNoncontrollingEntity",
        "EquityMethodInvestmentOtherThanTemporaryImpairment",
        "NetIncomeLossAttributableToNonredeemableNoncontrollingInterest",
        "TemporaryEquityForeignCurrencyTranslationAdjustments",
        "NoncontrollingInterestInNetIncomeLossOtherNoncontrollingInterestsRedeemable",
        "NoncontrollingInterestInNetIncomeLossOtherNoncontrollingInterestsNonredeemable",
        "IncomeLossFromSubsidiariesNetOfTax",
    ], "USD"),
    ("NetIncome", [
        "NetIncomeLoss",
        "ProfitLossAttributableToOwnersOfParent",
        "IncomeLossAttributableToParent",
    ], "USD"),
    ("NetIncomeToCommonShareholders", [
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "NetIncomeLossFromContinuingOperationsAvailableToCommonShareholdersBasic",
    ], "USD"),
    ("NetInterestIncome", [
        "InterestIncomeExpenseNet",
        "InterestAndDividendIncomeOperating",
    ], "USD"),
    ("NetInterestIncomeAfterProvision", [
        "InterestIncomeExpenseAfterProvisionForLoanLoss",
    ], "USD"),
    ("NonInterestExpense", [
        "NoninterestExpense",
    ], "USD"),
    ("NonInterestIncome", [
        "NoninterestIncome",
    ], "USD"),
    ("NonoperatingIncomeExpense", [
        "OtherNonoperatingIncomeExpense",
        "NonoperatingIncomeExpense",
        "FairValueAdjustmentOfWarrants",
        "AccretionAmortizationOfDiscountsAndPremiumsInvestments",
        "GainLossOnDispositionOfAssets1",
        "OtherNonoperatingIncome",
        "BusinessCombinationContingentConsiderationArrangementsChangeInAmountOfContingentConsiderationLiability1",
        "UnrealizedGainLossOnInvestments",
        "DerivativeGainLossOnDerivativeNet",
        "UnrealizedGainLossOnDerivatives",
        "OtherNoninterestExpense",
        "ForeignCurrencyTransactionGainLossUnrealized",
        "InvestmentIncomeNet",
        "OtherNonoperatingExpense",
        "GainLossOnSaleOfInvestments",
        "GainLossOnDerivativeInstrumentsNetPretax",
        "RealizedInvestmentGainsLosses",
        "GainLossOnSaleOfOtherAssets",
        "GainLossRelatedToLitigationSettlement",
        "DisposalGroupNotDiscontinuedOperationGainLossOnDisposal",
    ], "USD"),
    ("OccupancyExpense", [
        "OccupancyNet",
        "LeaseAndRentalExpense",
    ], "USD"),
    ("OperatingIncomeLoss", [
        "OperatingIncomeLoss",
        "ProfitLossFromOperatingActivities",
    ], "USD"),
    ("OperatingLeaseExpense", [
        "OperatingLeaseExpense",
        "OperatingLeaseCost",
        "DepreciationRightofuseAssets",
        "InterestExpenseOnLeaseLiabilities",
    ], "USD"),
    ("OtherExpenseIS", [
        "OtherCostAndExpenseOperating",
        "OtherExpenses",
    ], "USD"),
    ("OtherIncomeIS", [
        "OtherIncome",
        "OtherOperatingIncomeExpenseNet",
    ], "USD"),
    ("OtherOperatingExpense", [
        "NoninterestIncomeOtherOperatingIncome",
        "InformationTechnologyAndDataProcessing",
        "OperatingLeaseImpairmentLoss",
        "AccretionExpense",
        "ProvisionForOtherCreditLosses",
        "AssetRetirementObligationAccretionExpense",
        "OtherExpenseByFunction",
        "RealEstateTaxExpense",
        "PreOpeningCosts",
        "RoyaltyExpense",
        "UtilitiesOperatingExpenseMaintenanceAndOperations",
        "PensionExpense",
        "FranchisorCosts",
        "AcquisitionCosts",
        "CompensationExpenseExcludingCostOfGoodAndServiceSold",
        "ProductionCosts",
        "EnvironmentalRemediationExpense",
        "DevelopmentCosts",
        "ExplorationCosts",
        "LossOnContractTermination",
    ], "USD"),
    ("PensionExpense", [
        "NetPeriodicDefinedBenefitsExpenseReversalOfExpenseExcludingServiceCostComponent",
        "PensionAndOtherPostretirementBenefitExpense",
        "DefinedBenefitPlanNetPeriodicBenefitCost",
    ], "USD"),
    ("PolicyBenefitsAndClaims", [
        "PolicyholderBenefitsAndClaimsIncurredNet",
        "PolicyholderBenefitsAndClaimsIncurredGross",
    ], "USD"),
    ("PreferredDividendExpense", [
        "PreferredStockDividendsIncomeStatementImpact",
        "DividendsPreferredStock",
        "TemporaryEquityAccretionToRedemptionValueAdjustment",
        "DividendsPreferredStockCash",
        "PreferredStockDividendsAndOtherAdjustments",
        "RedeemablePreferredStockDividends",
        "TemporaryEquityDividendsAdjustment",
        "DividendsPreferredStockStock",
        "PreferredStockRedemptionPremium",
        "OtherPreferredStockDividendsAndAdjustments",
        "PreferredStockConversionsInducements",
        "PreferredStockRedemptionDiscount",
        "GeneralPartnerDistributions",
    ], "USD"),
    ("PretaxIncomeLoss", [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "ProfitLossBeforeTax",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
    ], "USD"),
    ("ProfessionalFees", [
        "ProfessionalFees",
        "ProfessionalAndContractServicesExpense",
    ], "USD"),
    ("ProfitLoss", [
        "ProfitLoss",
        "IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest",
    ], "USD"),
    ("ProvisionForCreditLosses", [
        "ProvisionForLoanAndLeaseLosses",
        "ProvisionForCreditLosses",
    ], "USD"),
    ("RentalAndLeasingRevenue", [
        "OperatingLeaseLeaseIncome",
        "OperatingLeasesIncomeStatementLeaseRevenue",
    ], "USD"),
    ("ResearchAndDevelopmentExpenses", [
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        "ResearchAndDevelopmentInProcess",
        "ExplorationExpense",
        "ResearchAndDevelopmentAssetAcquiredOtherThanThroughBusinessCombinationWrittenOff",
        "ResearchAndDevelopmentExpenseSoftwareExcludingAcquiredInProcessCost",
    ], "USD"),
    ("RestructuringExpenseBenefit", [
        "InventoryWriteDown",
        "RestructuringCharges",
        "BusinessCombinationAcquisitionRelatedCosts",
        "OtherAssetImpairmentCharges",
        "ImpairmentOfRealEstate",
        "DeconsolidationGainOrLossAmount",
        "RestructuringSettlementAndImpairmentProvisions",
        "RestructuringCosts",
        "RestructuringCostsAndAssetImpairmentCharges",
        "BusinessCombinationIntegrationRelatedCosts",
        "ImpairmentOfOilAndGasProperties",
        "ReorganizationItems",
        "AmortizationOfAcquisitionCosts",
        "DisposalGroupNotDiscontinuedOperationLossGainOnWriteDown",
        "RestructuringAndRelatedCostIncurredCost",
        "SeveranceCosts1",
        "ImpairmentOfLeasehold",
        "BusinessExitCosts1",
        "ExplorationAbandonmentAndImpairmentExpense",
        "ImpairmentOfOngoingProject",
    ], "USD"),
    ("Revenue", [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenue",
        "RevenueFromRelatedParties",
        "RevenueFromContractsWithCustomers",
        "PremiumsEarnedNet",
        "GainsLossesOnSalesOfAssets",
        "RevenueFromCollaborativeArrangementExcludingRevenueFromContractWithCustomer",
        "PrincipalTransactionsRevenue",
        "InsuranceServicesRevenue",
        "OperatingLeaseLeaseIncomeLeasePayments",
        "InterestAndFeeIncomeOtherLoans",
        "ResearchAndDevelopmentArrangementContractToPerformForOthersCompensationEarned",
        "OilAndGasSalesRevenue",
        "OperatingLeasesIncomeStatementMinimumLeaseRevenue",
        "PercentageRent",
        "ReimbursementRevenue",
        "RetailRevenue",
        "SaleOfTrustAssetsToPayExpenses",
    ], "USD"),
    ("RoyaltyRevenue", [
        "RoyaltyRevenue",
    ], "USD"),
    ("SellingGeneralAndAdminExpenses", [
        "GeneralAndAdministrativeExpense",
        "SellingGeneralAndAdministrativeExpense",
        "SellingAndMarketingExpense",
        "SellingExpense",
        "OtherGeneralAndAdministrativeExpense",
        "AdministrativeExpense",
        "TaxesExcludingIncomeAndExciseTaxes",
        "EmployeeBenefitsExpense",
        "TaxesOther",
        "GeneralInsuranceExpense",
        "OtherSellingGeneralAndAdministrativeExpense",
        "TravelAndEntertainmentExpense",
        "ProductionTaxExpense",
        "SalesCommissionsAndFees",
        "RealEstateTaxesAndInsurance",
        "DistributionCosts",
        "PumpTaxes",
    ], "USD"),
    ("SharesAverage", [
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageShares",
        "WeightedAverageBasicSharesOutstandingProForma",
    ], "shares"),
    ("SharesDilutionAdjustment", [
        "WeightedAverageNumberDilutedSharesOutstandingAdjustment",
        "IncrementalCommonSharesAttributableToShareBasedPaymentArrangements",
    ], "USD/shares"),
    ("SharesFullyDilutedAverage", [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
        "AdjustedWeightedAverageShares",
    ], "shares"),
    ("SharesIssued", [
        "CommonStockSharesIssued",
        "PreferredStockSharesIssued",
        "SharesIssued",
        "NumberOfSharesIssued",
        "NumberOfSharesIssuedAndFullyPaid",
    ], "shares"),
    ("SharesYearEnd", [
        "dei_EntityCommonStockSharesOutstanding",   # dei: namespace -- most reliable
        "CommonStockSharesOutstanding",
        "SharesOutstanding",
        "PreferredStockSharesOutstanding",
        "NumberOfSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    ], "shares"),
    ("SpecialItemsIncomeExpense(Pretax)", [
        "UnusualOrInfrequentItemInsuranceProceeds",
        "UnusualOrInfrequentItemNetOfInsuranceProceeds",
    ], "USD"),
    ("StockBasedCompensationExpense", [
        "ShareBasedCompensation",
        "AllocatedShareBasedCompensationExpense",
        "ExpenseFromSharebasedPaymentTransactionsWithEmployees",
    ], "USD"),
    ("TotalInterestIncomeOperating", [
        "InterestIncomeOperating",
    ], "USD"),
    ("TotalOperatingExpenses", [
        "OperatingExpenses",
        "OperatingCostsAndExpenses",
        "OperatingExpense",
    ], "USD"),
    ("ValuationAllowanceDTA", [
        "DeferredTaxAssetsValuationAllowance",
    ], "USD"),
    ("NetIncomeLoss", [
        "NetIncomeLoss",
        "ProfitLoss",
    ], "USD"),
    ("Revenues", [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
    ], "USD"),
    ("RegulatedAndUnregulatedOperatingRevenue", [
        "RegulatedAndUnregulatedOperatingRevenue",
        "ElectricUtilityRevenue",
    ], "USD"),
    ("DepreciationAmortization", [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "Depreciation",
    ], "USD"),
]

_BS_WATERFALL = [
    ("AccountsReceivableGross", [
        "AccountsReceivableGrossCurrent",
    ], "USD"),
    ("AccruedCompensation", [
        "EmployeeRelatedLiabilitiesCurrent",
        "AccruedSalariesCurrent",
    ], "USD"),
    ("AccruedIncomeTaxes", [
        "AccruedIncomeTaxesCurrent",
        "TaxesPayableCurrent",
    ], "USD"),
    ("AccumulatedAmortizationIntangibles", [
        "FiniteLivedIntangibleAssetsAccumulatedAmortization",
    ], "USD"),
    ("AccumulatedDepreciation", [
        "AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
    ], "USD"),
    ("AccumulatedOtherComprehensiveIncome", [
        "AccumulatedOtherComprehensiveIncomeLossNetOfTax",
        "OtherReserves",
        "AccumulatedOtherComprehensiveIncome",
        "ReserveOfExchangeDifferencesOnTranslation",
        "AccumulatedOtherComprehensiveIncomeLossForeignCurrencyTranslationAdjustmentNetOfTax",
        "AccumulatedOtherComprehensiveIncomeLossAvailableForSaleSecuritiesAdjustmentNetOfTax",
        "AccumulatedOtherComprehensiveIncomeLossDefinedBenefitPensionAndOtherPostretirementPlansNetOfTax",
        "AccumulatedOtherComprehensiveIncomeLossCumulativeChangesInNetGainLossFromCashFlowHedgesEffectNetOfTax",
    ], "USD"),
    ("AdditionalPaidInCapital", [
        "AdditionalPaidInCapital",
        "AdditionalPaidInCapitalCommonStock",
        "SharePremium",
        "AdditionalPaidinCapital",
    ], "USD"),
    ("AllEquityBalance", [
        "StockholdersEquity",
        "EquityAttributableToOwnersOfParent",
    ], "USD"),
    ("AllEquityBalanceIncludingMinorityInterest", [
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "Equity",
        "LimitedLiabilityCompanyLlcMembersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "AociIncludingPortionAttributableToNoncontrollingInterestTax",
    ], "USD"),
    ("AllowanceForDoubtfulAccounts", [
        "AllowanceForDoubtfulAccountsReceivableCurrent",
    ], "USD"),
    ("AssetRetirementObligations", [
        "AssetRetirementObligationsNoncurrent",
    ], "USD"),
    ("Assets", [
        "Assets",
        "AssetsNet",
    ], "USD"),
    ("AssetsHeldForSale", [
        "AssetsOfDisposalGroupIncludingDiscontinuedOperationCurrent",
        "AssetsHeldForSaleNotPartOfDisposalGroupCurrent",
    ], "USD"),
    ("CashAndMarketableSecurities", [
        "CashAndCashEquivalentsAtCarryingValue",
        "Cash",
        "AvailableForSaleSecuritiesDebtSecurities",
        "Investments",
        "CashAndDueFromBanks",
        "InterestBearingDepositsInBanks",
        "DebtSecuritiesAvailableForSaleExcludingAccruedInterest",
        "EquitySecuritiesFvNi",
        "HeldToMaturitySecuritiesFairValue",
        "AvailableForSaleDebtSecuritiesAmortizedCostBasis",
        "CashEquivalentsAtCarryingValue",
        "MarketableSecurities",
        "HeldToMaturitySecurities",
        "OtherShortTermInvestments",
        "EquitySecuritiesFvNiCurrentAndNoncurrent",
        "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLoss",
        "TradingSecuritiesDebt",
        "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
        "TradingSecurities",
        "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations",
    ], "USD"),
    ("CommonEquity", [
        "CommonStockValue",
        "TreasuryStockCommonValue",
        "IssuedCapital",
        "CommonStockValueOutstanding",
        "PartnersCapital",
        "MembersEquity",
        "CommonStocksIncludingAdditionalPaidInCapital",
        "StockholdersEquityBeforeTreasuryStock",
        "CommonStockShareSubscribedButUnissuedSubscriptionsReceivable",
        "UnearnedESOPShares",
        "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
        "RetainedEarningsUnappropriated",
        "DeferredCompensationEquity",
        "RetainedEarningsAppropriated",
        "CommonStockOtherSharesOutstanding",
        "OtherAdditionalCapital",
        "ReceivableFromShareholdersOrAffiliatesForIssuanceOfCapitalStock",
        "CommonStockOtherValueOutstanding",
        "AociLossCashFlowHedgeCumulativeGainLossAfterTax",
        "ReclassificationFromAociCurrentPeriodNetOfTaxAttributableToParent",
    ], "USD"),
    ("ContractAssets", [
        "CurrentContractAssets",
        "ContractWithCustomerAssetNet",
        "NoncurrentContractAssets",
        "ContractAssets",
    ], "USD"),
    ("ContractLiabilities", [
        "ContractWithCustomerLiabilityNoncurrent",
        "ContractWithCustomerLiability",
        "ContractLiabilities",
    ], "USD"),
    ("ConvertibleDebtNonCurrent", [
        "ConvertibleDebtNoncurrent",
    ], "USD"),
    ("CurrentAssetsTotal", [
        "AssetsCurrent",
        "CurrentAssets",
    ], "USD"),
    ("CurrentLiabilitiesTotal", [
        "LiabilitiesCurrent",
        "CurrentLiabilities",
    ], "USD"),
    ("CurrentPortionOfLongTermDebt", [
        "LongTermDebtCurrent",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
    ], "USD"),
    ("CustomerAdvances", [
        "ContractWithCustomerRefundLiabilityCurrent",
    ], "USD"),
    ("DeferredCompensationNonCurrent", [
        "DeferredCompensationLiabilityClassifiedNoncurrent",
    ], "USD"),
    ("DeferredPolicyAcquisitionCosts", [
        "DeferredPolicyAcquisitionCosts",
        "DeferredPolicyAcquisitionCostAmortizationExpense",
    ], "USD"),
    ("DeferredRevenueCurrent", [
        "DeferredRevenueCurrent",
        "CurrentContractLiabilities",
        "DeferredIncomeIncludingContractLiabilities",
        "DeferredIncomeOtherThanContractLiabilities",
    ], "USD"),
    ("DeferredRevenueNonCurrent", [
        "DeferredRevenueNoncurrent",
        "NoncurrentContractLiabilities",
    ], "USD"),
    ("DeferredTaxCurrentAssets", [
        "DeferredTaxAssetsDeferredIncome",
        "DeferredTaxAssetsGross",
        "DeferredTaxAssetsNet",
        "DeferredIncomeTaxesAndOtherAssetsCurrent",
        "DeferredTaxAssetsOther",
        "DeferredIncomeTaxesAndOtherTaxReceivableCurrent",
        "DeferredTaxAssetsTaxCreditCarryforwards",
        "DeferredTaxAssetsInventory",
        "DeferredTaxAssetsOperatingLossCarryforwards",
        "DeferredTaxAssetsPropertyPlantAndEquipment",
        "DeferredTaxAssetsTaxDeferredExpenseReservesAndAccruals",
    ], "USD"),
    ("DeferredTaxCurrentLiabilities", [
        "DeferredIncomeTaxLiabilitiesNet",
        "DeferredTaxLiabilities",
        "DeferredIncomeTaxLiabilities",
        "DeferredTaxLiabilitiesOther",
        "DeferredTaxLiabilitiesDerivatives",
        "DeferredTaxLiabilitiesTaxDeferredIncome",
        "DeferredTaxLiabilitiesDeferredExpense",
        "DeferredTaxLiabilitiesCurrent",
        "DeferredTaxLiabilitiesDeferredExpenseCapitalizedPatentCosts",
        "DeferredTaxLiabilitiesGoodwillAndIntangibleAssets",
        "DeferredTaxLiabilitiesGoodwillAndIntangibleAssetsIntangibleAssets",
        "DeferredTaxLiabilitiesPrepaidExpenses",
    ], "USD"),
    ("DeferredTaxNonCurrentLiabilities", [
        "DeferredIncomeTaxLiabilitiesNet",
        "DeferredTaxLiabilities",
        "AccruedIncomeTaxesNoncurrent",
        "AccruedIncomeTaxes",
        "TaxesPayableCurrentAndNoncurrent",
        "LiabilityForUncertainTaxPositionsNoncurrent",
        "DeferredIncomeTaxLiabilities",
        "DeferredTaxAndOtherLiabilitiesNoncurrent",
        "DeferredIncomeTaxesAndOtherTaxLiabilitiesNoncurrent",
        "AccumulatedDeferredInvestmentTaxCredit",
        "DeferredIncomeTaxesAndOtherLiabilitiesNoncurrent",
        "DeferredTaxLiabilitiesNoncurrent",
        "DeferredTaxLiabilitiesOther",
        "DeferredTaxLiabilitiesDerivatives",
        "DeferredTaxLiabilitiesTaxDeferredIncome",
        "DeferredTaxLiabilitiesDeferredExpense",
        "DeferredTaxAssetsLiabilitiesNetNoncurrent",
        "DeferredTaxAssetsLiabilitiesNet",
        "TaxCutsAndJobsActOf2017TransitionTaxForAccumulatedForeignEarningsLiabilityNoncurrent",
        "DeferredTaxLiabilitiesDeferredExpenseCapitalizedPatentCosts",
    ], "USD"),
    ("DeferredTaxNoncurrentAssets", [
        "DeferredIncomeTaxAssetsNet",
        "DeferredTaxAssets",
        "IncomeTaxesReceivableNoncurrent",
        "DeferredIncomeTaxesAndOtherAssetsNoncurrent",
        "DeferredTaxAssetsDeferredIncome",
        "DeferredTaxAssetsGross",
        "DeferredTaxAssetsNet",
        "DeferredTaxAssetsNetNoncurrent",
        "DeferredTaxAssetsOther",
        "DeferredTaxAssetsLiabilitiesNetNoncurrent",
        "DeferredTaxAssetsLiabilitiesNet",
        "DeferredTaxAssetsTaxCreditCarryforwards",
        "DeferredTaxAssetsInventory",
        "DeferredTaxAssetsCapitalLossCarryforwards",
        "DeferredTaxAssetsGrossNoncurrent",
        "DeferredTaxAssetsOperatingLossCarryforwards",
        "DeferredTaxAssetsPropertyPlantAndEquipment",
        "DeferredTaxAssetsTaxDeferredExpenseReservesAndAccruals",
    ], "USD"),
    ("DefinedBenefitPlanAssets", [
        "DefinedBenefitPlanFairValueOfPlanAssets",
    ], "USD"),
    ("DefinedBenefitPlanObligations", [
        "DefinedBenefitPlanBenefitObligation",
    ], "USD"),
    ("DefiniteLivedOperatingProvisions(DecommissioningEtc)", [
        "RegulatoryLiabilityNoncurrent",
        "LitigationReserveNoncurrent",
        "MineReclamationAndClosingLiabilityNoncurrent",
        "AccruedCappingClosurePostClosureAndEnvironmentalCostsNoncurrent",
        "AccruedCappingClosurePostClosureAndEnvironmentalCosts",
        "OilAndGasReclamationLiabilityNoncurrent",
        "DecommissioningLiabilityNoncurrent",
        "SpentNuclearFuelObligationNoncurrent",
    ], "USD"),
    ("DividendsPayable", [
        "DividendsPayableCurrent",
        "DividendsPayableCurrentAndNoncurrent",
    ], "USD"),
    ("Goodwill", [
        "Goodwill",
        "IndefiniteLivedLicenseAgreements",
        "IndefiniteLivedTrademarks",
        "IndefiniteLivedTradeNames",
        "GoodwillGross",
        "IndefiniteLivedFranchiseRights",
        "OtherIndefiniteLivedIntangibleAssets",
        "IndefiniteLivedContractualRights",
        "GoodwillImpairedAccumulatedImpairmentLoss",
    ], "USD"),
    ("GoodwillAndIntangiblesNet", [
        "IntangibleAssetsNetIncludingGoodwill",
        "GoodwillAndIntangibleAssetsNet",
    ], "USD"),
    ("GrossPropertyPlantEquipment", [
        "PropertyPlantAndEquipmentGross",
    ], "USD"),
    ("IncomeTaxReceivable", [
        "IncomeTaxesReceivable",
        "IncomeTaxReceivable",
    ], "USD"),
    ("IntangibleAssets", [
        "IntangibleAssetsNetExcludingGoodwill",
        "FiniteLivedIntangibleAssetsNet",
        "IntangibleAssetsOtherThanGoodwill",
        "OtherIntangibleAssetsNet",
        "IndefiniteLivedIntangibleAssetsExcludingGoodwill",
        "IntangibleAssetsCurrent",
        "FiniteLivedPatentsGross",
        "BusinessCombinationRecognizedIdentifiableAssetsAcquiredAndLiabilitiesAssumedIntangibles",
        "OtherFiniteLivedIntangibleAssetsGross",
        "FiniteLivedTrademarksGross",
        "FiniteLivedCustomerRelationshipsGross",
        "FiniteLivedIntangibleAssetOffMarketLeaseFavorableGross",
        "FiniteLivedCustomerListsGross",
        "FiniteLivedNoncompeteAgreementsGross",
        "FiniteLivedTradeNamesGross",
    ], "USD"),
    ("IntangibleAssetsGross", [
        "FiniteLivedIntangibleAssetsGross",
        "IntangibleAssetsGrossExcludingGoodwill",
    ], "USD"),
    ("Inventories", [
        "InventoryNet",
        "Inventories",
        "InventoryGross",
        "InventoryFinishedGoodsNetOfReserves",
        "InventoryValuationReserves",
        "InventoryRawMaterialsAndSupplies",
        "PropertySubjectToOrAvailableForOperatingLeaseNet",
        "InventoryFinishedGoods",
        "InventoryWorkInProcess",
        "InventoryWorkInProcessNetOfReserves",
        "OtherInventorySupplies",
        "InventoryRawMaterialsNetOfReserves",
        "InventoryRawMaterialsAndSuppliesNetOfReserves",
        "EnergyRelatedInventory",
        "EnergyRelatedInventoryNaturalGasInStorage",
        "InventoryRawMaterials",
        "FIFOInventoryAmount",
        "InventoryNetOfAllowancesCustomerAdvancesAndProgressBillings",
        "RetailRelatedInventoryMerchandise",
        "PropertySubjectToOrAvailableForOperatingLeaseAccumulatedDepreciation",
    ], "USD"),
    ("InvestmentsEquityMethod", [
        "EquityMethodInvestments",
        "InvestmentsInAffiliatesSubsidiariesAssociatesAndJointVentures",
        "InvestmentAccountedForUsingEquityMethod",
        "InvestmentsInAssociatesAccountedForUsingEquityMethod",
        "InvestmentsInJointVenturesAccountedForUsingEquityMethod",
    ], "USD"),
    ("Liabilities", [
        "Liabilities",
    ], "USD"),
    ("LiabilitiesAndEquity", [
        "LiabilitiesAndStockholdersEquity",
        "CommitmentsAndContingencies",
    ], "USD"),
    ("LoanLossReserve", [
        "FinancingReceivableAllowanceForCreditLosses",
        "AllowanceForLoanAndLeaseLosses",
    ], "USD"),
    ("LongTermDebt", [
        "LongTermDebtNoncurrent",
        "FinanceLeaseLiabilityNoncurrent",
        "LongTermNotesPayable",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongtermBorrowings",
        "ConvertibleLongTermNotesPayable",
        "LineOfCredit",
        "NotesPayable",
        "LongTermLoansPayable",
        "LongTermLineOfCredit",
        "NotesPayableRelatedPartiesNoncurrent",
        "OtherLongTermDebtNoncurrent",
        "SecuredLongTermDebt",
        "ConvertibleNotesPayable",
        "LongTermLoansFromBank",
        "UnsecuredDebt",
        "SeniorNotes",
        "DebtInstrumentUnamortizedDiscount",
        "OtherLongTermDebt",
    ], "USD"),
    ("LongtermInvestments", [
        "LongTermInvestments",
        "AvailableForSaleSecuritiesDebtSecurities",
        "Investments",
        "EquitySecuritiesFvNi",
        "HeldToMaturitySecuritiesFairValue",
        "LoansReceivableHeldForSaleNetNotPartOfDisposalGroup",
        "HeldToMaturitySecurities",
        "OtherInvestments",
        "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
        "OtherLongTermInvestments",
        "EquitySecuritiesFvNiCurrentAndNoncurrent",
        "InvestmentProperty",
        "RealEstateInvestments",
        "RestrictedInvestments",
        "EquitySecuritiesFVNINoncurrent",
        "EquitySecuritiesWithoutReadilyDeterminableFairValueAmount",
        "TradingSecurities",
        "LongTermInvestmentsAndReceivablesNet",
        "RestrictedInvestmentsNoncurrent",
        "PremiumsAndOtherReceivablesNet",
    ], "USD"),
    ("MinorityInterestBalance", [
        "MinorityInterest",
        "NoncontrollingInterests",
        "PartnersCapitalAttributableToNoncontrollingInterest",
        "MinorityInterestInOperatingPartnerships",
        "MembersEquityAttributableToNoncontrollingInterest",
        "NoncontrollingInterestInVariableInterestEntity",
        "NonredeemableNoncontrollingInterest",
        "MinorityInterestInJointVentures",
        "OtherMinorityInterests",
    ], "USD"),
    ("NetLoansAndLeases", [
        "LoansAndLeasesReceivableNetReportedAmount",
        "FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLoss",
        "LoansAndLeasesReceivableNetOfDeferredIncome",
    ], "USD"),
    ("NonCurrentAssetsTotal", [
        "AssetsNoncurrent",
        "NoncurrentAssets",
    ], "USD"),
    ("NonCurrentLiabilitiesTotal", [
        "LiabilitiesNoncurrent",
        "NoncurrentLiabilities",
    ], "USD"),
    ("NotesReceivableNonCurrent", [
        "NotesAndLoansReceivableNetNoncurrent",
    ], "USD"),
    ("OngoingOperatingProvisions(WarrantiesEtc)", [
        "WarrantsAndRightsOutstanding",
        "DeferredRevenue",
        "NoncurrentProvisions",
        "DeferredIncomeNoncurrent",
        "Provisions",
        "ProductWarrantyAccrualNoncurrent",
        "CustomerAdvancesAndDeposits",
        "ProductWarrantyAccrual",
        "ContractWithCustomerRefundLiability",
        "ContractWithCustomerRefundLiabilityNoncurrent",
        "DeferredRevenueAndCreditsNoncurrent",
        "StandardProductWarrantyAccrualNoncurrent",
        "CustomerRefundLiabilityNoncurrent",
        "CustomerAdvancesOrDepositsNoncurrent",
        "ExtendedProductWarrantyAccrual",
        "ExtendedProductWarrantyAccrualNoncurrent",
        "CustomerAdvancesNoncurrent",
        "CustomerDepositsNoncurrent",
        "DeferredRevenueAndCredits",
        "CustomerAdvancesForConstruction",
    ], "USD"),
    ("OperatingLeaseCurrentDebtEquivalent", [
        "OperatingLeaseLiabilityCurrent",
        "CurrentLeaseLiabilities",
        "OperatingLeaseLiability",
    ], "USD"),
    ("OperatingLeaseNonCurrentDebtEquivalent", [
        "OperatingLeaseLiabilityNoncurrent",
        "NoncurrentLeaseLiabilities",
        "OperatingLeaseLiability",
        "OperatingLeaseLiabilityStatementOfFinancialPositionExtensibleList",
    ], "USD"),
    ("OperatingLeaseRightOfUseAsset", [
        "OperatingLeaseRightOfUseAsset",
        "RightofuseAssets",
    ], "USD"),
    ("OtherNonOperatingCurrentAssets", [
        "PrepaidExpenseAndOtherAssetsCurrent",
        "OtherAssetsCurrent",
        "OtherAssets",
        "OtherReceivablesNetCurrent",
        "DueFromRelatedPartiesCurrent",
        "InterestReceivable",
        "OtherReceivables",
        "NotesReceivableNet",
        "PrepaidExpenseAndOtherAssets",
        "DerivativeAssetsCurrent",
        "LoansReceivableHeldForSaleNetNotPartOfDisposalGroup",
        "AccountsReceivableRelatedPartiesCurrent",
        "AssetsOfDisposalGroupIncludingDiscontinuedOperation",
        "PrepaidTaxes",
        "DeferredFinanceCostsNet",
        "DerivativeAssets",
        "NotesReceivableGross",
        "DueFromRelatedParties",
        "OtherPrepaidExpenseCurrent",
        "OtherCurrentFinancialAssets",
    ], "USD"),
    ("OtherNonOperatingCurrentLiabilities", [
        "OtherLiabilitiesCurrent",
        "DueToRelatedPartiesCurrent",
        "OtherLiabilities",
        "LiabilitiesOfDisposalGroupIncludingDiscontinuedOperationCurrent",
        "DerivativeLiabilitiesCurrent",
        "InterestPayableCurrent",
        "OtherAccruedLiabilitiesCurrent",
        "InterestPayableCurrentAndNoncurrent",
        "DerivativeLiabilities",
        "BusinessCombinationContingentConsiderationLiabilityCurrent",
        "LiabilitiesOfDisposalGroupIncludingDiscontinuedOperation",
        "OtherAccountsPayableAndAccruedLiabilities",
        "DueToAffiliateCurrent",
        "DueToAffiliateCurrentAndNoncurrent",
        "DueToOtherRelatedPartiesClassifiedCurrent",
        "AssetRetirementObligationCurrent",
        "RegulatoryLiabilityCurrent",
        "AccountsPayableOtherCurrentAndNoncurrent",
        "BusinessCombinationContingentConsiderationLiability",
        "LitigationReserveCurrent",
    ], "USD"),
    ("OtherNonOperatingNonCurrentAssets", [
        "OtherAssetsNoncurrent",
        "OtherAssets",
        "AssetsHeldInTrustNoncurrent",
        "DisposalGroupIncludingDiscontinuedOperationAssetsNoncurrent",
        "InterestReceivable",
        "FinanceLeaseRightOfUseAsset",
        "OtherReceivables",
        "PrepaidExpenseNoncurrent",
        "NotesReceivableNet",
        "PrepaidExpenseAndOtherAssets",
        "MarketableSecuritiesNoncurrent",
        "DebtSecuritiesAvailableForSaleExcludingAccruedInterest",
        "MarketableSecurities",
        "AssetsOfDisposalGroupIncludingDiscontinuedOperation",
        "PrepaidTaxes",
        "DeferredFinanceCostsNet",
        "DerivativeAssets",
        "DerivativeAssetsNoncurrent",
        "OtherNoncurrentFinancialAssets",
        "NotesReceivableGross",
    ], "USD"),
    ("OtherNonOperatingNonCurrentLiabilities", [
        "OtherLiabilitiesNoncurrent",
        "LiabilitiesNoncurrent",
        "OtherLiabilities",
        "DerivativeLiabilitiesNoncurrent",
        "LiabilitiesOfDisposalGroupIncludingDiscontinuedOperationNoncurrent",
        "InterestPayableCurrentAndNoncurrent",
        "DerivativeLiabilities",
        "BusinessCombinationContingentConsiderationLiabilityNoncurrent",
        "DividendsPayableCurrentAndNoncurrent",
        "LiabilitiesOfDisposalGroupIncludingDiscontinuedOperation",
        "DueToRelatedPartiesNoncurrent",
        "DueToAffiliateCurrentAndNoncurrent",
        "LiabilitiesOtherThanLongtermDebtNoncurrent",
        "AccountsPayableOtherCurrentAndNoncurrent",
        "BusinessCombinationContingentConsiderationLiability",
        "AccruedProfessionalFeesCurrentAndNoncurrent",
        "OtherAccruedLiabilitiesNoncurrent",
        "AccruedEnvironmentalLossContingenciesNoncurrent",
        "SharesSubjectToMandatoryRedemptionSettlementTermsAmountNoncurrent",
        "OffMarketLeaseUnfavorable",
    ], "USD"),
    ("OtherOperatingCurrentAssets", [
        "RestrictedCashAndCashEquivalents",
        "ContractWithCustomerAssetNetCurrent",
        "DeferredOfferingCosts",
        "DepositsAssetsCurrent",
        "OtherCurrentAssets",
        "DeferredCostsCurrent",
        "CapitalizedContractCostNetCurrent",
        "AdvancesOnInventoryPurchases",
        "RestrictedInvestments",
        "RestrictedCashAndInvestmentsCurrent",
        "DeferredCostsCurrentAndNoncurrent",
        "DeferredCostsAndOtherAssets",
        "RestrictedInvestmentsCurrent",
        "CapitalizedContractCostNet",
        "SettlementAssetsCurrent",
        "DebtSecuritiesAvailableForSaleRestricted",
        "ContractWithCustomerAssetGrossCurrent",
        "FundsHeldForClients",
        "ContractWithCustomerAssetAccumulatedAllowanceForCreditLossCurrent",
        "OtherDeferredCostsNet",
    ], "USD"),
    ("OtherOperatingCurrentLiabilities", [
        "AccruedLiabilitiesCurrent",
        "ContractWithCustomerLiabilityCurrent",
        "AccruedLiabilitiesAndOtherLiabilities",
        "DeferredRevenue",
        "OtherCurrentLiabilities",
        "AccruedLiabilitiesCurrentAndNoncurrent",
        "LiabilityForClaimsAndClaimsAdjustmentExpense",
        "DeferredIncomeCurrent",
        "AccruedEmployeeBenefitsCurrent",
        "ProductWarrantyAccrualClassifiedCurrent",
        "AccruedPayrollTaxesCurrent",
        "DepositLiabilityCurrent",
        "CustomerAdvancesCurrent",
        "CustomerDepositsCurrent",
        "SettlementLiabilitiesCurrent",
        "CustomerRefundLiabilityCurrent",
        "OtherAccruedLiabilitiesCurrentAndNoncurrent",
        "DeferredRentCreditCurrent",
        "AccruedBonusesCurrent",
        "PayablesToCustomers",
    ], "USD"),
    ("OtherOperatingNonCurrentAssets", [
        "DeferredCosts",
        "DepositsAssetsNoncurrent",
        "AccountsReceivableNet",
        "OtherNoncurrentAssets",
        "AllowanceForDoubtfulAccountsReceivable",
        "RestrictedCashAndCashEquivalentsNoncurrent",
        "CapitalizedComputerSoftwareNet",
        "CapitalizedContractCostNetNoncurrent",
        "InventoryNoncurrent",
        "AccountsReceivableNetNoncurrent",
        "ContractWithCustomerAssetNetNoncurrent",
        "AdvancesOnInventoryPurchases",
        "DeferredCostsCurrentAndNoncurrent",
        "DeferredCostsAndOtherAssets",
        "UnbilledContractsReceivable",
        "CapitalizedContractCostNet",
        "AllowanceForDoubtfulAccountsReceivableNoncurrent",
        "FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLossNoncurrent",
        "LandAvailableForDevelopment",
        "OtherDeferredCostsNet",
    ], "USD"),
    ("OtherOperatingNonCurrentLiabilities", [
        "AccountsPayableAndAccruedLiabilitiesCurrentAndNoncurrent",
        "OtherNoncurrentLiabilities",
        "AccountsPayableCurrentAndNoncurrent",
        "AccountsPayableAndOtherAccruedLiabilities",
        "AccruedLiabilitiesCurrentAndNoncurrent",
        "DeferredRentCreditNoncurrent",
        "AccountsPayableAndAccruedLiabilitiesNoncurrent",
        "OtherAccruedLiabilitiesCurrentAndNoncurrent",
        "WorkersCompensationLiabilityNoncurrent",
        "AccountsPayableTradeCurrentAndNoncurrent",
        "AccountsPayableRelatedPartiesNoncurrent",
        "ProgramRightsObligationsNoncurrent",
        "SalesAndExciseTaxPayableCurrentAndNoncurrent",
        "AccruedSalesCommissionCurrentAndNoncurrent",
        "AccruedSalariesCurrentAndNoncurrent",
        "AccruedRoyaltiesCurrentAndNoncurrent",
        "ConstructionPayableCurrentAndNoncurrent",
        "OtherLiabilitiesAndDeferredRevenueNoncurrent",
        "AccruedRentNoncurrent",
        "AccruedRentCurrentAndNoncurrent",
    ], "USD"),
    ("PensionObligations", [
        "PensionAndOtherPostretirementDefinedBenefitPlansLiabilitiesNoncurrent",
        "DefinedBenefitPensionPlanLiabilitiesNoncurrent",
        "PensionAndOtherPostretirementAndPostemploymentBenefitPlansLiabilitiesNoncurrent",
    ], "USD"),
    ("PlantPropertyEquipmentNet", [
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipment",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
        "Land",
        "ConstructionInProgressGross",
        "MachineryAndEquipmentGross",
        "BuildingsAndImprovementsGross",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAccumulatedDepreciationAndAmortization",
        "PropertyPlantAndEquipmentOther",
        "RealEstateHeldforsale",
        "FurnitureAndFixturesGross",
        "PropertyPlantAndEquipmentOtherNet",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetBeforeAccumulatedDepreciationAndAmortization",
        "LeaseholdImprovementsGross",
        "LandAndLandImprovements",
        "RealEstateInvestmentsUnconsolidatedRealEstateAndOtherJointVentures",
        "OilAndGasPropertySuccessfulEffortMethodNet",
        "FixturesAndEquipmentGross",
        "OilAndGasPropertyFullCostMethodNet",
        "OilAndGasPropertySuccessfulEffortMethodAccumulatedDepreciationDepletionAndAmortization",
    ], "USD"),
    ("PreferredStock", [
        "PreferredStockValue",
        "PreferredStockValueOutstanding",
        "PreferredStockRedemptionAmount",
        "AdditionalPaidInCapitalPreferredStock",
        "TreasuryStockPreferredValue",
        "PreferredStockSharesSubscribedButUnissuedSubscriptionsReceivable",
    ], "USD"),
    ("PrepaidExpenses", [
        "PrepaidExpenseCurrent",
        "CurrentPrepaidExpenses",
        "Prepayments",
    ], "USD"),
    ("RealEstateInvestments", [
        "RealEstateInvestmentPropertyNet",
        "RealEstateInvestmentPropertyAtCost",
    ], "USD"),
    ("RegulatedAssets", [
        "RegulatoryAssetsNoncurrent",
        "RegulatoryAssets",
    ], "USD"),
    ("RegulatedLiabilities", [
        "RegulatoryLiabilities",
        "RegulatoryLiabilitiesNoncurrent",
    ], "USD"),
    ("RestrictedCashCurrent", [
        "RestrictedCashCurrent",
        "RestrictedCash",
        "RestrictedCashAndCashEquivalentsAtCarryingValue",
        "CurrentRestrictedCashAndCashEquivalents",
    ], "USD"),
    ("RestrictedCashNonCurrent", [
        "RestrictedCashNoncurrent",
        "NoncurrentRestrictedCashAndCashEquivalents",
        "RestrictedCashAndInvestmentsNoncurrent",
    ], "USD"),
    ("RestructuringProvisions", [
        "RestructuringReserve",
        "RestructuringReserveNoncurrent",
    ], "USD"),
    ("RetainedEarnings", [
        "RetainedEarningsAccumulatedDeficit",
        "RetainedEarnings",
    ], "USD"),
    ("RetirementRelatedCurrentLiabilities", [
        "DeferredCompensationLiabilityCurrent",
        "EmployeeRelatedLiabilitiesCurrentAndNoncurrent",
        "DeferredCompensationShareBasedArrangementsLiabilityCurrent",
        "DeferredCompensationLiabilityCurrentAndNoncurrent",
        "PensionAndOtherPostretirementDefinedBenefitPlansCurrentLiabilities",
        "WorkersCompensationLiabilityCurrent",
        "PensionAndOtherPostretirementDefinedBenefitPlansLiabilitiesCurrentAndNoncurrent",
        "PensionAndOtherPostretirementAndPostemploymentBenefitPlansLiabilitiesCurrent",
        "PensionAndOtherPostretirementAndPostemploymentBenefitPlansLiabilitiesCurrentAndNoncurrent",
        "DeferredCompensationCashBasedArrangementsLiabilityCurrent",
        "DefinedBenefitPensionPlanCurrentAndNoncurrentLiabilities",
        "DefinedBenefitPensionPlanLiabilitiesCurrent",
        "PostemploymentBenefitsLiabilityCurrent",
        "OtherDeferredCompensationArrangementsLiabilityCurrent",
        "OtherEmployeeRelatedLiabilitiesCurrentAndNoncurrent",
        "OtherPostretirementDefinedBenefitPlanLiabilitiesCurrentAndNoncurrent",
    ], "USD"),
    ("RetirementRelatedNonCurrentAssets", [
        "DefinedBenefitPlanAssetsForPlanBenefitsNoncurrent",
        "DeferredCompensationPlanAssets",
        "DefinedBenefitPlanAmountsRecognizedInBalanceSheet",
    ], "USD"),
    ("RetirementRelatedNonCurrentLiabilities", [
        "OtherPostretirementDefinedBenefitPlanLiabilitiesNoncurrent",
        "EmployeeRelatedLiabilitiesCurrentAndNoncurrent",
        "PostemploymentBenefitsLiabilityNoncurrent",
        "DeferredCompensationLiabilityCurrentAndNoncurrent",
        "AssetRetirementObligation",
        "PensionAndOtherPostretirementDefinedBenefitPlansLiabilitiesCurrentAndNoncurrent",
        "PensionAndOtherPostretirementAndPostemploymentBenefitPlansLiabilitiesCurrentAndNoncurrent",
        "SupplementalUnemploymentBenefitsSeveranceBenefits",
        "DefinedBenefitPensionPlanCurrentAndNoncurrentLiabilities",
        "OtherPostretirementBenefitsPayableNoncurrent",
        "OtherEmployeeRelatedLiabilitiesCurrentAndNoncurrent",
        "OtherPostretirementDefinedBenefitPlanLiabilitiesCurrentAndNoncurrent",
    ], "USD"),
    ("SecurityDepositsAsset", [
        "SecurityDeposit",
    ], "USD"),
    ("SelfInsuranceReserve", [
        "SelfInsuranceReserveCurrent",
        "SelfInsuranceReserve",
    ], "USD"),
    ("ShortTermDebt", [
        "NotesPayableCurrent",
        "ShortTermBorrowings",
        "FinanceLeaseLiabilityCurrent",
        "NotesPayableRelatedPartiesClassifiedCurrent",
        "ConvertibleNotesPayableCurrent",
        "LoansPayableCurrent",
        "LinesOfCreditCurrent",
        "ConvertibleDebtCurrent",
        "DebtCurrent",
        "LineOfCredit",
        "ShorttermBorrowings",
        "OtherNotesPayableCurrent",
        "ShortTermBankLoansAndNotesPayable",
        "LoansPayable",
        "ConvertibleNotesPayable",
        "OtherShortTermBorrowings",
        "LoansPayableToBankCurrent",
        "SecuredDebtCurrent",
        "OtherLongTermDebtCurrent",
        "BankOverdrafts",
    ], "USD"),
    ("ShortTermInvestments", [
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "CurrentInvestments",
        "ShorttermInvestmentsClassifiedAsCashEquivalents",
    ], "USD"),
    ("TaxesPayable", [
        "SalesAndExciseTaxPayableCurrent",
        "CurrentTaxLiabilities",
        "AccrualForTaxesOtherThanIncomeTaxesCurrent",
        "AccruedIncomeTaxes",
        "TaxesPayableCurrentAndNoncurrent",
        "LiabilityForUncertainTaxPositionsCurrent",
    ], "USD"),
    ("TemporaryAndMezzanineFinancing", [
        "TemporaryEquityCarryingAmountAttributableToParent",
        "RedeemableNoncontrollingInterestEquityCarryingAmount",
        "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",
        "TemporaryEquityValueExcludingAdditionalPaidInCapital",
        "RedeemableNoncontrollingInterestEquityPreferredCarryingAmount",
        "RedeemableNoncontrollingInterestEquityCommonCarryingAmount",
        "RedeemableNoncontrollingInterestEquityOtherCarryingAmount",
        "RedeemableNoncontrollingInterestEquityFairValue",
        "RedeemableNoncontrollingInterestEquityRedemptionValue",
        "RedeemableNoncontrollingInterestEquityOtherFairValue",
        "RedeemableNoncontrollingInterestEquityCommonFairValue",
    ], "USD"),
    ("TotalDeposits", [
        "Deposits",
        "DepositsFairValueDisclosure",
    ], "USD"),
    ("TradePayables", [
        "AccountsPayableCurrent",
        "AccountsPayableAndAccruedLiabilitiesCurrent",
        "AccountsPayableAndOtherAccruedLiabilitiesCurrent",
        "TradeAndOtherCurrentPayables",
        "AccountsPayableAndAccruedLiabilitiesCurrentAndNoncurrent",
        "AccountsPayableRelatedPartiesCurrent",
        "AccountsPayableTradeCurrent",
        "AccountsPayableCurrentAndNoncurrent",
        "AccountsPayableAndOtherAccruedLiabilities",
        "AccountsPayableOtherCurrent",
        "ReinsurancePayable",
        "AccruedRoyaltiesCurrent",
        "CommissionsPayableToBrokerDealersAndClearingOrganizations",
        "AccountsPayableUnderwritersPromotersAndEmployeesOtherThanSalariesAndWagesCurrent",
        "AccountsPayableTradeCurrentAndNoncurrent",
        "ContractualObligation",
        "ProgramRightsObligationsCurrent",
        "AccruedRoyaltiesCurrentAndNoncurrent",
        "BusinessCombinationRecognizedIdentifiableAssetsAcquiredAndLiabilitiesAssumedCurrentLiabilitiesAccountsPayable",
        "OilAndGasSalesPayableCurrent",
    ], "USD"),
    ("TradeReceivables", [
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
        "TradeAndOtherCurrentReceivables",
        "AccountsAndOtherReceivablesNetCurrent",
        "AccountsReceivableNet",
        "NotesAndLoansReceivableNetCurrent",
        "AllowanceForDoubtfulAccountsReceivable",
        "LoansReceivableHeldForSaleAmount",
        "UnbilledReceivablesCurrent",
        "AccountsNotesAndLoansReceivableNetCurrent",
        "AccountsAndNotesReceivableNet",
        "AllowanceForNotesAndLoansReceivableCurrent",
        "PremiumsReceivableAtCarryingValue",
        "ReceivablesFromCustomers",
        "TradeReceivables",
        "UnbilledContractsReceivable",
        "FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLossCurrent",
        "ReceivablesLongTermContractsOrPrograms",
        "OilAndGasJointInterestBillingReceivablesCurrent",
        "ContractWithCustomerAssetAccumulatedAllowanceForCreditLoss",
    ], "USD"),
    ("TreasuryShares", [
        "TreasuryStockValue",
        "TreasuryStockCommonShares",
        "TreasuryStockShares",
        "TreasuryShares",
    ], "USD"),
    ("UnearnedRevenue", [
        "UnearnedRevenue",
    ], "USD"),
    ("CashAndCashEquivalents", [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ], "USD"),
    ("CommonSharesOutstanding", [
        "CommonStockSharesOutstanding",
        "CommonStockSharesIssued",
    ], "shares"),
    ("CommonEquityTierOneCapitalRatio", [
        "CommonEquityTierOneCapitalRatioToRiskWeightedAssets",
    ], "pure"),
    ("TierOneCapitalRatio", [
        "TierOneRiskBasedCapitalRatio",
    ], "pure"),
    ("TotalCapitalRatioRiskBased", [
        "TotalRiskBasedCapitalRatio",
    ], "pure"),
    ("TierOneLeverageRatio", [
        "TierOneLeverageCapitalRatioToAverageAssets",
    ], "pure"),
]

_CF_WATERFALL = [
    ("AcquisitionsNet", [
        "PaymentsToAcquireBusinessesNetOfCashAcquired",
        "PaymentsToAcquireBusinessesGross",
        "PaymentsToAcquireBusinessesAndInterestInAffiliates",
        "AcquisitionOfSubsidiariesNetOfCashAcquired",
    ], "USD"),
    ("CapitalExpenses", [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "PaymentsToAcquireOtherProductiveAssets",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        # E&P-specific: edgartools' industry_extensions/energy.json tags this
        # "Capital expenditures" (occurrence_rate 0.24) -- not in the general
        # gaap_mappings.json, only the energy industry extension.
        "PaymentsToExploreAndDevelopOilAndGasProperties",
        "PaymentsForProceedsFromDevelopmentOfRealEstate",
        # REIT-specific: verified live against PSA and SPG XBRL (2026-08-27).
        # Neither filer tags the general PropertyPlantAndEquipment concepts
        # above at all -- confirmed by capital_expenditures resolving to $0
        # for PSA before this fix, which silently inflated FCF (= CFO - 0)
        # and every FCF-derived valuation metric (EV/FCF, FCF/EV, AFFO).
        "PaymentsToAcquireRealEstate",                       # PSA, O, VTR
        "PaymentsToDevelopRealEstateAssets",                  # PSA, VTR
        "PaymentsToAcquireRealEstateAndRealEstateJointVentures",  # SPG
        # Verified live against O and VTR XBRL (2026-08-27) while testing
        # this fix -- AMT needed no addition here, it already tags the
        # general PaymentsToAcquirePropertyPlantAndEquipment/
        # PaymentsToAcquireProductiveAssets candidates above (towers are
        # general PP&E in AMT's own taxonomy use, not "real estate").
        "PaymentsToAcquireCommercialRealEstate",              # O, VTR
        "PaymentsToAcquireAndDevelopRealEstate",              # O
        "PaymentsForDepositsOnRealEstateAcquisitions",        # O
        # Unverified -- not found live for PSA/SPG/AMT/O/VTR, kept only in
        # case a different REIT filer uses one of these; harmless no-ops
        # otherwise since an unmatched candidate is simply skipped.
        "PaymentsToAcquireRealEstateAndRealEstateJointVentureInterests",
        "PaymentsForRealEstate",
        "CashPaidForRealEstateAcquisitions",
    ], "USD"),
    ("CapitalLeasePaymentsCF", [
        "RepaymentsOfLongTermCapitalLeaseObligations",
    ], "USD"),
    # ── REIT FFO/AFFO components (NAREIT definitions) ──────────────────────
    # Grouped together (not alphabetical) for traceability. Verified against
    # live SEC XBRL companyfacts for PSA and SPG (2026-08-27):
    #   - Neither filer tags a REIT-specific D&A concept separate from the
    #     general "DepreciationAndAmortization" -- for a pure-play REIT,
    #     essentially all D&A *is* real-estate D&A, so the generic tags are
    #     the reliable candidates; the specific ones below are kept first in
    #     case a rarer filer does split them out, but are unverified/rare.
    #   - "GainsLossesOnSalesOfInvestmentRealEstate" (PSA) and
    #     "GainLossOnSaleOfPropertiesBeforeApplicableIncomeTaxes" (SPG) are
    #     each filer's real gain-on-sale tag -- neither matches the other,
    #     confirming this needs a broad candidate list, not one "the" tag.
    #   - No filer-level maintenance/recurring CapEx tag exists for either
    #     filer -- MaintenanceCapex is expected to fall back to total CapEx
    #     for most REITs (data_layer.py's maintenance_capex property).
    ("RealEstateDA", [
        "DepreciationDepletionAndAmortizationRealEstate",
        "DepreciationAndAmortizationRealEstate",
        "RealEstateDepreciationAndAmortization",
        "DepreciationAndAmortization",
        "DepreciationDepletionAndAmortization",
    ], "USD"),
    ("GainOnSaleRealEstate", [
        "GainsLossesOnSalesOfInvestmentRealEstate",          # confirmed: PSA
        "GainLossOnSaleOfPropertiesBeforeApplicableIncomeTaxes",  # confirmed: SPG
        "GainLossOnDispositionOfProperty",                   # confirmed: PSA
        "GainLossOnDispositionOfAssets",                     # confirmed: PSA (broader)
        "GainLossOnSaleOfProperties",
        "GainLossOnSaleOfRealEstate",
        "GainOnSaleOfRealEstateHeldForInvestment",
        "RealEstateInvestmentPropertyGainLossOnDisposal",
    ], "USD"),
    ("ImpairmentRealEstate", [
        "ImpairmentOfRealEstate",             # confirmed: PSA, SPG
        "ImpairmentLossesRelatedToRealEstate",
        "AssetImpairmentChargesRealEstate",
        "AssetImpairmentCharges",             # confirmed: PSA (general fallback)
    ], "USD"),
    ("StraightLineRentAdj", [
        "StraightLineRentAdjustments",
        "StraightLineRent",                   # confirmed: SPG
        "StraightLineRentAdjustment",
    ], "USD"),
    ("AboveBelowMarketLeaseAmort", [
        "AmortizationOfAboveAndBelowMarketLeases",
        "AmortizationOfAboveMarketLeaseIntangibles",
        "AboveAndBelowMarketLeaseAmortization",
    ], "USD"),
    ("MaintenanceCapex", [
        "PaymentsForCapitalImprovementsRealEstate",
        "MaintenanceCapitalExpenditures",
        "RecurringCapitalExpenditures",
        "PaymentsForImprovements",
        "PaymentsForCapitalImprovements",
    ], "USD"),
    ("NonCashStockComp", [
        "ShareBasedCompensation",                   # confirmed: PSA
        "AllocatedShareBasedCompensationExpense",   # confirmed: PSA
    ], "USD"),
    ("NonCashInterest", [
        "AmortizationOfFinancingCosts",              # confirmed: PSA, SPG
        "AmortizationOfDebtDiscountPremium",         # confirmed: PSA, SPG
        "AmortizationOfFinancingCostsAndDiscounts",
    ], "USD"),
    ("CashAndCashEquivalents", [
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations",
        "CashAndCashEquivalents",
        "CashAndBankBalancesAtCentralBanks",
    ], "USD"),
    ("ChangeInAccruedLiabilities", [
        "IncreaseDecreaseInAccruedLiabilities",
        "IncreaseDecreaseInEmployeeRelatedLiabilities",
    ], "USD"),
    ("ChangeInDeferredRevenue", [
        "IncreaseDecreaseInContractWithCustomerLiability",
        "IncreaseDecreaseInDeferredRevenue",
    ], "USD"),
    ("ChangeInInventory", [
        "IncreaseDecreaseInInventories",
        "AdjustmentsForDecreaseIncreaseInInventories",
    ], "USD"),
    ("ChangeInOtherWorkingCapital", [
        "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",
        "IncreaseDecreaseInOtherOperatingAssets",
        "IncreaseDecreaseInOtherOperatingLiabilities",
        "IncreaseDecreaseInOtherCurrentAssets",
        "IncreaseDecreaseInOtherOperatingCapitalNet",
        "IncreaseDecreaseInOtherCurrentLiabilities",
        "IncreaseDecreaseInOperatingCapital",
    ], "USD"),
    ("ChangeInPayables", [
        "IncreaseDecreaseInAccountsPayable",
        "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities",
        "AdjustmentsForIncreaseDecreaseInTradeAccountPayable",
    ], "USD"),
    ("ChangeInReceivables", [
        "IncreaseDecreaseInAccountsReceivable",
        "IncreaseDecreaseInReceivables",
        "AdjustmentsForDecreaseIncreaseInTradeAccountReceivable",
        "IncreaseDecreaseInAccountsAndNotesReceivable",
    ], "USD"),
    ("CommonDividendsPaid", [
        "DividendsCommonStockCash",
        "DividendsCommonStock",
        "Dividends",
        "DividendsPaid",
        "DividendsPaidClassifiedAsFinancingActivities",
        "DividendsCash",
    ], "USD"),
    ("DebtProceeds", [
        "ProceedsFromIssuanceOfLongTermDebt",
        "ProceedsFromNotesPayable",
        "ProceedsFromConvertibleDebt",
        "ProceedsFromIssuanceOfDebt",
        "ProceedsFromShortTermDebt",
        "ProceedsFromLongTermLinesOfCredit",
        "ProceedsFromBorrowingsClassifiedAsFinancingActivities",
        "ProceedsFromIssuanceOfSeniorLongTermDebt",
        "ProceedsFromBankDebt",
        "ProceedsFromIssuanceOfUnsecuredDebt",
        "ProceedsFromNoncurrentBorrowings",
        "ProceedsFromIssuanceOfMediumTermNotes",
    ], "USD"),
    ("DebtRepayments", [
        "RepaymentsOfLongTermDebt",
        "RepaymentsOfNotesPayable",
        "RepaymentsOfDebt",
        "RepaymentsOfLongTermLinesOfCredit",
        "RepaymentsOfShortTermDebt",
        "RepaymentsOfBorrowingsClassifiedAsFinancingActivities",
        "RepaymentsOfSeniorDebt",
        "RepaymentsOfNoncurrentBorrowings",
        "RepaymentsOfCurrentBorrowings",
    ], "USD"),
    ("DeferredIncomeTaxCF", [
        "DeferredIncomeTaxesAndTaxCredits",
    ], "USD"),
    ("DepreciationAmortizationCF", [
        # CF non-cash add-back: these appear as operating activity adjustments
        # in the cash flow statement filed under 10-K (not IS depreciation)
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "Depreciation",
        "OtherDepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationNonproduction",
        "DepreciationAndAmortizationExcludingAssetRetirementObligation",
    ], "USD"),
    ("DistributionsToMinorityInterests", [
        "PaymentsOfDividends",
        "PaymentsToMinorityShareholders",
        "PaymentsOfDividendsMinorityInterest",
    ], "USD"),
    ("DivestitureProceeds", [
        "ProceedsFromDivestitureOfBusinesses",
        "ProceedsFromDivestitureOfBusinessesNetOfCashDivested",
        "ProceedsFromDivestitureOfInterestInConsolidatedSubsidiaries",
    ], "USD"),
    ("EquityExpenseIncome(BuybackIssued)", [
        "ProceedsFromIssuanceOfCommonStock",
        "PaymentsForRepurchaseOfCommonStock",
        "ProceedsFromSaleOfTreasuryStock",
    ], "USD"),
    ("FinanceLeasePayments", [
        "FinanceLeasePrincipalPayments",
    ], "USD"),
    ("ForeignExchangeEffectOnCash", [
        "EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations",
        "EffectOfExchangeRateOnCashAndCashEquivalents",
    ], "USD"),
    ("GainLossOnAssetSalesCF", [
        "GainLossOnDispositionOfAssets",
        "AdjustmentsForLossesGainsOnDisposalOfNoncurrentAssets",
        "GainLossOnSaleOfAssets",
    ], "USD"),
    ("ImpairmentChargesCF", [
        "ImpairmentOfLongLivedAssetsHeldForUse",
        "ImpairmentOfLongLivedAssetsToBeDisposedOf",
        "AdjustmentsForImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss",
        "ImpairmentOfInvestments",
    ], "USD"),
    ("InvestmentProceeds", [
        "ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities",
        "ProceedsFromSaleOfAvailableForSaleSecuritiesDebt",
        "ProceedsFromSaleAndMaturityOfMarketableSecurities",
        "ProceedsFromSaleAndMaturityOfAvailableForSaleSecurities",
        "ProceedsFromSaleOfShortTermInvestments",
        "ProceedsFromMaturitiesPrepaymentsAndCallsOfHeldToMaturitySecurities",
        "ProceedsFromSaleOfAvailableForSaleSecurities",
    ], "USD"),
    ("InvestmentPurchases", [
        "PaymentsToAcquireAvailableForSaleSecuritiesDebt",
        "PaymentsToAcquireInvestments",
        "PaymentsToAcquireMarketableSecurities",
        "PaymentsToAcquireShortTermInvestments",
        "PaymentsToAcquireOtherInvestments",
        "PaymentsToAcquireHeldToMaturitySecurities",
    ], "USD"),
    ("NetCashFromFinancingActivities", [
        "NetCashProvidedByUsedInFinancingActivities",
        "CashFlowsFromUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    ], "USD"),
    ("NetCashFromInvestingActivities", [
        "NetCashProvidedByUsedInInvestingActivities",
        "CashFlowsFromUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    ], "USD"),
    ("NetCashFromOperatingActivities", [
        "NetCashProvidedByUsedInOperatingActivities",
        "CashFlowsFromUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ], "USD"),
    ("NetChangeInCash", [
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
        "EffectOfExchangeRateChangesOnCashAndCashEquivalents",
        "IncreaseDecreaseInCashAndCashEquivalents",
        "CashAndCashEquivalentsPeriodIncreaseDecrease",
    ], "USD"),
    ("OperatingLeasePayments", [
        "OperatingLeasePayments",
    ], "USD"),
    ("OtherNonCashItemsCF", [
        "OtherNoncashIncomeExpense",
        "OtherNoncashIncome",
    ], "USD"),
    ("PaymentsOfDebtIssuanceCosts", [
        "PaymentsOfDebtIssuanceCosts",
        "PaymentsOfFinancingCosts",
    ], "USD"),
    ("ProceedsFromMaturitiesOfInvestments", [
        "ProceedsFromMaturitiesPrepaymentsAndCallsOfShorttermInvestments",
        "ProceedsFromMaturitiesOfInvestments",
    ], "USD"),
    ("ProceedsFromSaleOfPPE", [
        "ProceedsFromSaleOfPropertyPlantAndEquipment",
        "ProceedsFromSaleOfProductiveAssets",
    ], "USD"),
    ("ProvisionForDoubtfulAccountsCF", [
        "ProvisionForDoubtfulAccounts",
    ], "USD"),
    ("PurchaseOfIntangibleAssets", [
        "PaymentsToAcquireIntangibleAssets",
    ], "USD"),
    ("StockBasedCompensationCF", [
        "AdjustmentsForSharebasedPayments",
        "EmployeeServiceShareBasedCompensationAllocationOfRecognizedPeriodCostsCapitalizedAmount",
    ], "USD"),
    ("StockIssuanceProceeds", [
        "ProceedsFromStockOptionsExercised",
        "ProceedsFromIssuanceOfPreferredStockAndPreferenceStock",
        "ProceedsFromStockPlans",
        "ProceedsFromIssuingShares",
        "ProceedsFromIssuanceOrSaleOfEquity",
        "ProceedsFromIssuanceOfShares",
    ], "USD"),
    ("StockRepurchasePayments", [
        "PaymentsToAcquireOrRedeemEntitysShares",
        "PaymentsForRepurchaseOfEquity",
        "PaymentsForRepurchaseOfPreferredStockAndPreferenceStock",
        "PurchaseOfTreasuryShares",
        "PaymentsForRepurchaseOfOtherEquity",
    ], "USD"),
    ("DepreciationExpense", [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
    ], "USD"),
    ("DepreciationAndAmortization", [
        "DepreciationAndAmortization",
        "DepreciationDepletionAndAmortization",
    ], "USD"),
    ("PropertyPlantAndEquipmentAdditions", [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        # PaymentsForCapitalImprovements removed: edgartools' own
        # gaap_mappings.json maps it to standard_tags=['NetCashFromInvestingActivities']
        # at confidence 0.311, not to CapitalExpenses/capex -- a low-confidence,
        # wrong-bucket mapping, not a genuine capex concept.
    ], "USD"),
    ("InterestPaidCF", [
        "InterestPaidNet",
        "InterestPaid",
    ], "USD"),
    ("DividendsPaid", [
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfDividends",
        "PaymentsOfOrdinaryDividends",
    ], "USD"),
]



# -----------------------------------------------------------------------------
# Low-confidence (0.30-0.49) but high-count (>500 companies) CF additions
# These are correctly-mapped CF concepts penalised by edgartools' conservative
# confidence model.  Appended AFTER the high-conf entries so they never
# displace a better-matched concept.
# -----------------------------------------------------------------------------
_CF_LOW_CONF_ADDITIONS = [
    # NetCashFromOperatingActivities supplemental line items (non-cash add-backs)
    ("_cf_noncash_lease",     ["IncreaseDecreaseInOperatingLeaseLiability",
                                "RightOfUseAssetObtainedInExchangeForOperatingLeaseLiability",
                                "OperatingLeaseRightOfUseAssetAmortizationExpense"], "USD"),
    # ChangeInOtherWorkingCapital additions
    ("_cf_wc_prepaid",        ["IncreaseDecreaseInPrepaidExpense",
                                "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets"], "USD"),
    ("_cf_wc_accrued",        ["IncreaseDecreaseInAccruedLiabilitiesAndOtherOperatingLiabilities",
                                "IncreaseDecreaseInAccruedIncomeTaxesPayable"], "USD"),
    # DebtProceeds additions
    ("_cf_debt_proceeds",     ["ProceedsFromRelatedPartyDebt",
                                "ProceedsFromLinesOfCredit",
                                "ProceedsFromIssuanceOfPrivatePlacement",
                                "ProceedsFromIssuanceInitialPublicOffering"], "USD"),
    # DebtRepayments additions
    ("_cf_debt_repay",        ["RepaymentsOfRelatedPartyDebt",
                                "RepaymentsOfLinesOfCredit",
                                "RepaymentsOfNotesPayable"], "USD"),
    # DividendsPaid additions (PaymentsOfDividendsCommonStock is low-conf but correct)
    ("_cf_dividends",         ["PaymentsOfDividendsCommonStock",
                                "PaymentsOfDividends",
                                "PaymentsOfOrdinaryDividends",
                                "MinorityInterestDecreaseFromDistributionsToNoncontrollingInterestHolders"], "USD"),
    # NetCashFromFinancingActivities misc additions
    ("_cf_fin_misc",          ["PaymentsRelatedToTaxWithholdingForShareBasedCompensation",
                                "PaymentsOfStockIssuanceCosts",
                                "ProceedsFromPaymentsForOtherFinancingActivities",
                                "PaymentsForProceedsFromOtherInvestingActivities"], "USD"),
    # IncomeTaxes CF paid (IncomeTaxesPaid is correctly IncomeTaxes in CF context)
    ("IncomeTaxesPaidCF",     ["IncomeTaxesPaid",
                                "IncomeTaxesPaidNet"], "USD"),
]

# Sectors where _SECTOR_OVERRIDES should PREPEND (fire before base waterfall).
# Used for cases where a universal concept EXISTS but picks a sub-component,
# so the sector-specific concept must be tried first.
# Real Estate: OperatingLeaseLeaseIncome must beat RevenueFromContractWithCustomer
# Financial Services: InterestIncomeExpenseNet must beat ASC 606 sub-components
_SECTOR_PREPEND_OVERRIDE = {"Real Estate", "Financial Services"}

# -----------------------------------------------------------------------------
# Period discovery
# -----------------------------------------------------------------------------

_ANCHOR_CONCEPTS = [
    ("Revenues", "USD", False),
    ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD", False),
    ("SalesRevenueNet", "USD", False),
    ("NetIncomeLoss", "USD", False),
    ("Assets", "USD", True),
]


def _discover_periods(us_gaap: dict, max_years: int) -> list[str]:
    """
    Find all annual period-end dates from anchor concepts.
    Returns sorted newest-first, capped at max_years.
    """
    all_ends: set[str] = set()
    for concept, unit, instant in _ANCHOR_CONCEPTS:
        vals = _extract_annual(us_gaap, concept, unit, is_instant=instant)
        all_ends.update(vals.keys())

    periods = sorted(all_ends, reverse=True)

    if max_years and len(periods) > max_years:
        periods = periods[:max_years]

    return periods


def _has_valid_values(vals: dict, periods: list) -> bool:
    """
    True if `vals` (a {period_end_date: value} dict, e.g. from
    _extract_annual) has at least one entry whose period-end date is
    actually within the target `periods` window.

    _extract_annual returns every annual value a concept has ever
    reported for a filer, with no awareness of which periods the caller
    actually needs -- a concept the filer abandoned years ago (e.g.
    AMZN/NVDA's PaymentsToAcquirePropertyPlantAndEquipment, last used
    ~2012-2016) still returns a non-empty dict, just for dates entirely
    outside the current 5-year window. Used by _resolve_waterfall so its
    "first candidate that resolves" loop commits to a concept only when
    it actually covers a period being asked for, rather than merely
    existing somewhere in the filer's history -- otherwise the next
    (correct, current) candidate in the list never gets tried.
    """
    if not vals:
        return False
    return any(p in vals for p in periods)


# -----------------------------------------------------------------------------
# Waterfall resolution -> DataFrame
# -----------------------------------------------------------------------------

def _resolve_waterfall(us_gaap: dict, waterfall: list, periods: list,
                       is_instant: bool = False,
                       ticker: str = "",
                       sector: str = "",
                       fine_industry: str = "") -> tuple[pd.DataFrame, dict]:
    """
    Resolve a waterfall into a DataFrame compatible with StatementProfile.

    Phase 1 additions:
    - ticker: if provided, prepends company-specific concept mappings
              (from edgartools company_mappings/*.json) to each waterfall entry
    - sector: if provided, splices sector-specific concept priorities
              (from gaap_mappings industry_overrides) after the base concepts

    Phase 2 addition (hybrid SIC-based fine-grained industry routing):
    - fine_industry: if provided (SIC-derived, see _sic_to_industry), prepends
              fine-grained industry-specific concepts (_INDUSTRY_CONCEPT_OVERRIDES)
              ahead of the broad sector override -- catches genuinely
              industry-specific tags (e.g. OilAndGasRevenue for E&P) that
              gaap_mappings' industry_overrides don't carry.

    Priority order per waterfall entry:
      1. Company-specific patch (highest)
      2. Fine-grained industry override (NEW)
      3. Broad sector override (existing _SECTOR_OVERRIDES)
      4. Base waterfall concepts (universal, sorted by company_count)

    Returns
    -------
    df : DataFrame with columns [standard_concept, concept, <period_dates...>]
    resolution_log : {edgartools_label: resolved_raw_concept_or_None}
    """
    rows = []
    log  = {}

    # Per-ticker concept patches (e.g. tsla:, msft: extension tags)
    company_patch: dict[str, list[str]] = _COMPANY_MAPPINGS.get(ticker.upper(), {})

    # Fine-grained SIC-derived industry override (NEW)
    industry_override: dict[str, list[str]] = (
        _INDUSTRY_CONCEPT_OVERRIDES.get(fine_industry, {}) if fine_industry else {}
    )

    # Per-sector concept priority overrides
    sector_override: dict[str, list[str]] = _SECTOR_OVERRIDES.get(sector, {})

    for label, concepts, unit in waterfall:
        # Build augmented concept list:
        #   1. Company-specific concepts (extension tags, highest priority)
        #   2. Fine-grained industry concepts (NEW -- SIC-derived)
        #   3. Sector-specific PREPEND concepts (override base for sector)
        #   4. Base waterfall concepts (universal, sorted by company_count)
        #   5. Sector-specific APPEND concepts (fallback additions)
        #
        # Sectors in _SECTOR_PREPEND_OVERRIDE get their concepts tried BEFORE
        # the base waterfall -- critical for REITs where the universal concept
        # (RevenueFromContractWithCustomer) exists but picks a sub-component.
        # All other sectors get their overrides appended as fallbacks.
        company_concepts  = company_patch.get(label, [])
        industry_concepts = [c for c in industry_override.get(label, [])
                             if c not in company_concepts]
        sector_ov         = sector_override.get(label, [])

        if sector in _SECTOR_PREPEND_OVERRIDE and sector_ov:
            # Prepend sector concepts before base (REITs, Insurance, Banks for Revenue)
            prepend = [c for c in sector_ov
                      if c not in company_concepts and c not in industry_concepts]
            append  = []
            base    = [c for c in concepts
                      if c not in company_concepts and c not in industry_concepts
                      and c not in prepend]
            augmented = company_concepts + industry_concepts + prepend + base + append
        else:
            # Default: sector concepts as fallback after base
            append  = [c for c in sector_ov if c not in concepts
                      and c not in company_concepts and c not in industry_concepts]
            augmented = company_concepts + industry_concepts + concepts + append

        resolved_concept = None
        period_vals = {}
        # First non-empty candidate seen, regardless of period coverage --
        # last-resort fallback only if NO candidate covers the target
        # periods at all (see below), so a filer whose period-discovery
        # anchors landed slightly differently than this specific line
        # item's own history still gets *something* rather than nothing.
        fallback_concept = None
        fallback_vals = {}

        for concept in augmented:
            use_max = concept in _MAX_MAGNITUDE_CONCEPTS
            vals = _extract_annual(us_gaap, concept, unit, is_instant=is_instant,
                                   use_max_magnitude=use_max)
            if not vals:
                continue
            if fallback_concept is None:
                fallback_concept, fallback_vals = concept, vals
            # Only commit to this candidate if it actually has a value for
            # at least one of the periods being asked for -- not merely
            # non-empty somewhere in the filer's history. Without this, a
            # concept a filer stopped using years ago (first candidate,
            # non-empty) permanently blocks a later candidate that's
            # actually current (e.g. AMZN/NVDA capex: the legacy
            # PaymentsToAcquirePropertyPlantAndEquipment tag stops
            # resolving around 2012-2016, so it used to win here and
            # PaymentsToAcquireProductiveAssets -- what they file now --
            # was never tried).
            if _has_valid_values(vals, periods):
                resolved_concept = concept
                period_vals = vals
                break

        if resolved_concept is None and fallback_concept is not None:
            resolved_concept = fallback_concept
            period_vals = fallback_vals

        row = {
            "standard_concept": label,
            "concept":          resolved_concept or "",
        }
        for p in periods:
            row[p] = period_vals.get(p)

        rows.append(row)
        log[label] = resolved_concept

    df = pd.DataFrame(rows)
    return df, log


# -----------------------------------------------------------------------------
# Reconciliation passes
# -----------------------------------------------------------------------------

def _reconcile(is_df: pd.DataFrame, bs_df: pd.DataFrame,
               cf_df: pd.DataFrame, periods: list,
               ticker: str, sector: str = "") -> list[str]:
    """
    Post-hoc reconciliation and quality checks.
    Returns a list of warning strings (empty = clean).
    """
    flags = []

    if periods:
        p0 = periods[0]  # most recent

        # -- Revenue: sector-specific sum-of-components + cross-checks ----------

        rev_resolved  = _cell(is_df, "Revenue",  p0)
        rev_alt       = _cell(is_df, "Revenues", p0)
        nii           = _cell(is_df, "NetInterestIncome", p0)
        nonint        = _cell(is_df, "NonInterestIncome", p0)
        premiums      = _cell(is_df, "NetInterestIncome", p0)   # reused below

        # -- 1. Banks: Revenue = InterestIncomeExpenseNet + NoninterestIncome --
        # InterestIncomeExpenseNet alone = NII (~52-70% of total revenue).
        # NoninterestIncome (~31 tickers) provides the remaining fee/trading revenue.
        # Also guards against SFNC-style overstatement where InterestIncomeExpenseNet
        # is GROSS interest (larger than actual net revenue).
        if sector in ("Financial Services",):
            # FinancialServicesRevenue = InterestIncomeExpenseNet (true NII)
            # NOT NetInterestIncome which resolves to gross sub-components
            fsr_val    = _cell(is_df, "FinancialServicesRevenue", p0)
            nonint_val = _cell(is_df, "NonInterestIncome",        p0)

            # Detect insurer vs bank: check which raw concept FSR resolved to.
            fsr_concept = ""
            _fsr_mask = is_df["standard_concept"] == "FinancialServicesRevenue"
            if _fsr_mask.any():
                fsr_concept = is_df.loc[_fsr_mask].iloc[0].get("concept", "")
            is_insurer = fsr_concept == "PremiumsEarnedNet"

            # -- 1a. Bank sum: NII + NoninterestIncome --
            # Guard: NonInt must be >5% of FSR (avoids FISV/V/MA false positives)
            if (not is_insurer
                    and fsr_val and fsr_val > 1e8
                    and nonint_val
                    and abs(nonint_val) > 1e7
                    and abs(nonint_val) > fsr_val * 0.05):
                bank_total = fsr_val + abs(nonint_val)
                # If Revenues exists and is larger, use it instead
                # (catches IBKR where NII+NonInt undershoots total revenue,
                # and small banks where NII+NonInt overshoots)
                if rev_alt and rev_alt > 1e8 and rev_alt > fsr_val:
                    if bank_total > rev_alt * 1.10:
                        # Over-reporting: cap at Revenues
                        if rev_alt > (rev_resolved or 0):
                            _fill_row(is_df, "Revenue",
                                      {p: _cell(is_df, "Revenues", p)
                                       for p in periods})
                            rev_resolved = rev_alt
                            flags.append(
                                f"Bank revenue: sum({bank_total/1e9:.1f}B) > "
                                f"Revenues({rev_alt/1e9:.1f}B) -- capped"
                            )
                    elif rev_alt > bank_total * 1.10:
                        # Under-reporting: Revenues has more (IBKR pattern)
                        _fill_row(is_df, "Revenue",
                                  {p: _cell(is_df, "Revenues", p)
                                   for p in periods})
                        rev_resolved = rev_alt
                        flags.append(
                            f"Bank revenue: Revenues({rev_alt/1e9:.1f}B) > "
                            f"sum({bank_total/1e9:.1f}B) -- using Revenues"
                        )
                    elif bank_total > (rev_resolved or 0):
                        # Normal bank sum
                        _fill_row(is_df, "Revenue",
                                  {p: (_cell(is_df, "FinancialServicesRevenue", p) or 0) +
                                      abs(_cell(is_df, "NonInterestIncome", p) or 0)
                                   for p in periods})
                        rev_resolved = bank_total
                        flags.append(
                            f"Bank revenue: NII({fsr_val/1e9:.1f}B) + "
                            f"NonInt({nonint_val/1e9:.1f}B) = {bank_total/1e9:.1f}B"
                        )
                elif bank_total > (rev_resolved or 0):
                    # No Revenues tag available, use bank sum as-is
                    _fill_row(is_df, "Revenue",
                              {p: (_cell(is_df, "FinancialServicesRevenue", p) or 0) +
                                  abs(_cell(is_df, "NonInterestIncome", p) or 0)
                               for p in periods})
                    rev_resolved = bank_total
                    flags.append(
                        f"Bank revenue: NII({fsr_val/1e9:.1f}B) + "
                        f"NonInt({nonint_val/1e9:.1f}B) = {bank_total/1e9:.1f}B"
                    )

            # -- 1b. Insurer: PremiumsEarned + InvestmentIncome (or Revenues) --
            elif is_insurer and fsr_val and fsr_val > 1e8:
                ni_inv = _cell(is_df, "InterestAndDividendIncome", p0)
                if ni_inv and ni_inv > 1e7:
                    ins_total = fsr_val + ni_inv
                    if ins_total > (rev_resolved or 0):
                        _fill_row(is_df, "Revenue",
                                  {p: (_cell(is_df, "FinancialServicesRevenue", p) or 0) +
                                      (_cell(is_df, "InterestAndDividendIncome", p) or 0)
                                   for p in periods})
                        rev_resolved = ins_total
                        flags.append(
                            f"Insurance revenue: Premiums({fsr_val/1e9:.1f}B) + "
                            f"InvIncome({ni_inv/1e9:.1f}B) = {ins_total/1e9:.1f}B"
                        )
                elif rev_alt and rev_alt > fsr_val * 1.05 and rev_alt > 1e8:
                    _fill_row(is_df, "Revenue",
                              {p: _cell(is_df, "Revenues", p) for p in periods})
                    rev_resolved = rev_alt
                    flags.append(
                        f"Insurance revenue: Revenues({rev_alt/1e9:.1f}B) > "
                        f"PremiumsEarned({fsr_val/1e9:.1f}B) -- using Revenues"
                    )

            # -- 1c. FS fallback: Revenues cross-check --
            # For brokers/investment banks where neither bank sum nor insurer
            # path fires (JEF pattern: FSR=ManagementFeesRevenue, NonInt=None)
            elif (rev_alt and rev_resolved
                    and rev_alt > rev_resolved * 1.10 and rev_alt > 1e8):
                _fill_row(is_df, "Revenue",
                          {p: _cell(is_df, "Revenues", p) for p in periods})
                flags.append(
                    f"FS revenue cross-check: Revenues({rev_alt/1e9:.1f}B) > "
                    f"Revenue({rev_resolved/1e9:.1f}B) -- substituting"
                )
                rev_resolved = rev_alt

        # -- 2. All sectors: Revenues > resolved Revenue -> substitute ----------
        # Handles BG/ADM commodity traders (ASC606 picks net, Revenues=gross)
        # and EXR/COLD storage REITs (OperatingLeaseLeaseIncome is a sub-component)
        if (rev_resolved is not None and rev_alt is not None
                and rev_alt > rev_resolved * 1.5 and rev_alt > 1e8):
            _fill_row(is_df, "Revenue",
                      {p: _cell(is_df, "Revenues", p) for p in periods})
            flags.append(
                f"Revenue cross-check: Revenues ({rev_alt/1e9:.1f}B) >> "
                f"resolved Revenue ({rev_resolved/1e9:.1f}B) -- substituting"
            )

        # -- 1. Revenue sanity: if Revenue resolved to 0, check if
        #    it's a REIT/utility/bank that uses a sector-specific tag
        rev_val = _cell(is_df, "Revenue", p0)
        if rev_val is None or rev_val == 0:
            flags.append(
                f"Revenue resolved to 0 for {p0} -- likely a sector-specific "
                f"tag not in the waterfall (REIT, utility, or bank)"
            )

        # -- 2. Equity: if AllEquityBalance is 0 but AllEquityBalanceIncludingMinorityInterest
        #    has a value, copy it.  Some filers only tag the inclusive version.
        eq  = _cell(bs_df, "AllEquityBalance", p0)
        eqi = _cell(bs_df, "AllEquityBalanceIncludingMinorityInterest", p0)
        if (eq is None or eq == 0) and eqi and eqi != 0:
            _fill_row(bs_df, "AllEquityBalance", {p: _cell(bs_df, "AllEquityBalanceIncludingMinorityInterest", p)
                                                   for p in periods})
            flags.append("Equity: using inclusive-of-minority-interest figure")

        # -- 2c. COGS plausibility guard: some filers tag
        #    CostOfGoodsAndServicesSold to a minor segment/component
        #    subtotal rather than consolidated COGS (e.g. CAT, DE resolve
        #    to a ~$50-80M sub-line against $50B+ of total operating
        #    costs), and labor-intensive multi-line-expense filers
        #    (airlines, railroads, freight brokers -- AAL, LUV, NSC, UPS,
        #    CSX, UNP, CHRW, EXPD, ...) have no CostOfGoodsAndServicesSold
        #    tag at all, so the waterfall falls back to LaborAndRelatedExpense
        #    -- one line out of a multi-line operating expense schedule, a
        #    small fraction of true cost of service. Both patterns produce
        #    a COGS figure that is an implausibly small sliver of total
        #    operating costs, which (via the Revenue-COGS override below)
        #    inflates gross margin toward ~100%. When CostsSubtotal (total
        #    operating costs) is available and COGS is under 15% of it,
        #    treat COGS as unresolved so the override is skipped -- the
        #    general branch in core.agents then shows gross margin as
        #    "N/A (not reported separately)" instead of a nonsense
        #    near-100% figure. (Sector-specific branches such as
        #    freight_broker read CostsSubtotal directly via
        #    inc.total_operating_costs rather than depending on this
        #    row, so they still produce a numeric estimate.)
        # Managed-care insurers (see core.agents._MANAGED_CARE_TICKERS) are
        # exempt: their CostOfGoodsAndServicesSold tag legitimately resolves
        # to a small product/pharmacy-cost figure that sits ALONGSIDE (not
        # instead of) medical claims costs -- core.agents combines the two
        # directly. Nulling it here would silently drop the product-cost
        # component from that combination.
        _managed_care_exempt = {"CNC", "UNH", "ELV", "CI", "CVS", "MOH", "HUM"}
        cogs_check   = _cell(is_df, "CostOfGoodsAndServicesSold", p0)
        costs_subtot = _cell(is_df, "CostsSubtotal", p0)
        _cogs_mask = is_df["standard_concept"] == "CostOfGoodsAndServicesSold"
        _cogs_raw_concept = (is_df.loc[_cogs_mask].iloc[0].get("concept", "")
                             if _cogs_mask.any() else "")
        # LaborAndRelatedExpense is a waterfall fallback candidate for
        # CostOfGoodsAndServicesSold, but for multi-line-operating-expense
        # filers (airlines, railroads, freight -- AAL, LUV, NSC, UPS, CSX,
        # UNP, CHRW, EXPD, ...) it resolves to just the payroll line, one
        # component out of many (fuel, purchased transportation,
        # maintenance, depreciation, ...), not a cost-of-revenue figure --
        # regardless of its magnitude relative to total costs (unlike the
        # CAT/DE segment-subtotal pattern below, labor cost is often itself
        # a large fraction of total costs, so a magnitude threshold alone
        # doesn't catch it).
        _cogs_bad_concept = _cogs_raw_concept == "LaborAndRelatedExpense"
        _cogs_bad_magnitude = bool(
            cogs_check and costs_subtot and costs_subtot > 0
            and cogs_check < costs_subtot * 0.15
        )
        if ticker.upper() not in _managed_care_exempt and (_cogs_bad_concept or _cogs_bad_magnitude):
            reason = ("tagged via LaborAndRelatedExpense (payroll only, not cost of revenue)"
                      if _cogs_bad_concept else
                      f"implausibly small (${cogs_check/1e9:.2f}B) vs total operating "
                      f"costs (${costs_subtot/1e9:.2f}B) -- likely a segment/component subtotal")
            flags.append(
                f"CostOfGoodsAndServicesSold {reason}, not consolidated COGS. "
                f"Suppressing so gross margin falls back to N/A rather than a "
                f"misleading derived figure."
            )
            if _cogs_mask.any():
                _cogs_idx = is_df.index[_cogs_mask][0]
                for p in periods:
                    if p in is_df.columns:
                        is_df.at[_cogs_idx, p] = np.nan

        # -- 3. Gross profit: prefer Revenue - COGS over the XBRL GrossProfit
        #    tag whenever both are resolved. This also guards against
        #    GrossProfit resolving to a segment subtotal (e.g. DIS) that
        #    would otherwise silently understate gross margin without
        #    tripping the agents.py overflow guard.
        gp   = _cell(is_df, "GrossProfit", p0)
        rev  = _cell(is_df, "Revenue", p0)
        cogs = _cell(is_df, "CostOfGoodsAndServicesSold", p0)
        if rev and cogs:
            computed = {p: (_cell(is_df, "Revenue", p) or 0) - (_cell(is_df, "CostOfGoodsAndServicesSold", p) or 0)
                        for p in periods}
            if gp and gp != 0:
                print(f"[{ticker}] GrossProfit override: XBRL={gp} → "
                      f"derived={computed.get(p0)} (Revenue - COGS)")
            _fill_row(is_df, "GrossProfit", computed)

        # -- 4. Net debt sanity: LT debt should be > 0 for companies
        #    with interest expense > 0
        ie_val = _cell(is_df, "InterestExpense", p0)
        lt_val = _cell(bs_df, "LongTermDebt", p0)
        if ie_val and abs(ie_val) > 1e6 and (lt_val is None or lt_val == 0):
            flags.append(
                f"Interest expense ${ie_val/1e6:.0f}M but LongTermDebt=0 -- "
                f"debt tag may use a non-standard XBRL concept"
            )

        # -- 5. CF InterestPaid: copy to CF InterestExpense label for
        #    compatibility with existing profile's interest_paid property
        ie_cf = _cell(cf_df, "InterestPaidCF", p0)
        if ie_cf and ie_cf != 0:
            # The existing CashFlowProfile.interest_paid looks for
            # standard_concept == 'InterestExpense' in the CF dataframe.
            # We mapped CF interest to 'InterestPaidCF' to avoid collision
            # with the IS InterestExpense.  Add an alias row.
            alias_vals = {p: _cell(cf_df, "InterestPaidCF", p) for p in periods}
            _add_row(cf_df, "InterestExpense", "InterestPaidNet", alias_vals)

    return flags


def _cell(df: pd.DataFrame, label: str, period: str):
    """Extract a single value from a resolved DataFrame."""
    if df is None or df.empty:
        return None
    mask = df["standard_concept"] == label
    if not mask.any():
        return None
    row = df.loc[mask].iloc[0]
    val = row.get(period)
    if pd.isna(val):
        return None
    return float(val)


def _fill_row(df: pd.DataFrame, label: str, values: dict) -> None:
    """Overwrite values in an existing row."""
    mask = df["standard_concept"] == label
    if not mask.any():
        return
    idx = df.index[mask][0]
    for col, val in values.items():
        if col in df.columns and val is not None:
            df.at[idx, col] = val


def _add_row(df: pd.DataFrame, label: str, concept: str, values: dict) -> None:
    """Append a new row to a DataFrame."""
    row = {"standard_concept": label, "concept": concept}
    row.update(values)
    new = pd.DataFrame([row])
    # Append in-place via concat
    combined = pd.concat([df, new], ignore_index=True)
    df.drop(df.index, inplace=True)
    for col in combined.columns:
        df[col] = combined[col].values


# -----------------------------------------------------------------------------
# FactsDataProcessor -- drop-in replacement for RobustDataProcessor
# -----------------------------------------------------------------------------

class FactsDataProcessor:
    """
    Loads financial data from the SEC company facts API.

    Interface matches RobustDataProcessor:
        .load_data()  -> bool
        .financials   -> dict with keys: income_statement, balance_sheet, cash_flow
        .market_cap   -> float
        .ticker       -> str
        .sector       -> str

    Additional diagnostics:
        .periods          -> list[str]  all discovered annual periods
        .resolution_log   -> dict       {statement: {label: resolved_concept}}
        .data_flags       -> list[str]  reconciliation warnings
    """

    def __init__(self, ticker: str, sector: str | None = None,
                 cache=None):
        self.ticker  = ticker.upper()
        # A caller-supplied sector that is neither missing nor the "General"
        # placeholder is treated as an explicit override and always wins over
        # SIC-based inference (see load_data()).
        self._sector_explicit = bool(sector) and sector != "General"
        self.sector  = sector or "General"
        self._cache  = cache   # unused for now; reserved for future caching

        self.financials:     dict = {}
        self.market_cap:     float = 0.0
        self.periods:        list[str] = []
        self.resolution_log: dict = {}
        self.data_flags:     list[str] = []
        self.degraded_reason: str | None = None   # set when standard ingestion failed
        self._sic:            int | None = None
        self._fine_industry:  str | None = None

    def _get_sic(self, ticker: str, cik: str) -> int | None:
        """Fetch the filer's SIC code via edgartools, for fine-grained industry routing."""
        try:
            from edgar import Company
            company = Company(ticker)
            sic = company.sic
            return int(sic) if sic else None
        except Exception:
            return None

    def load_data(self, max_years: int = 10) -> bool:
        """
        Fetch and resolve all financial data.

        Parameters
        ----------
        max_years : Maximum number of annual periods to return.
                    Newest first.  Set to 0 or None for all available.

        Returns True on success, False if no usable data found.
        """
        print(f"[{self.ticker}] FactsDataProcessor: loading from company facts API...")

        # -- Resolve CIK --------------------------------------------------
        cik = _get_cik(self.ticker)
        print(f"[DEBUG] {self.ticker}: CIK = {cik}")
        if not cik:
            print(f"[{self.ticker}] CIK not found.")
            return False

        # -- Fine-grained SIC-based industry routing ----------------------
        self._sic = self._get_sic(self.ticker, cik)
        self._fine_industry = _sic_to_industry(self._sic)
        print(f"[{self.ticker}] SIC={self._sic} → industry={self._fine_industry}")

        # -- SIC-based sector inference (unless caller passed an explicit
        #    override) -- must run before any of the sector-gated logic
        #    below (Financial Services revenue derivation, REIT overrides,
        #    etc.) so it sees the resolved sector. ----------------------
        if not self._sector_explicit:
            _inferred_sector = _infer_sector_from_sic(self._sic, self._fine_industry)
            if _inferred_sector:
                print(f"[{self.ticker}] sector inferred from SIC {self._sic}: {_inferred_sector}")
                self.sector = _inferred_sector

        # -- Fetch company facts blob -------------------------------------
        facts = _get_facts(cik)
        print(f"[DEBUG] {self.ticker}: raw company facts response (first 500 chars): {str(facts)[:500]}")
        if not facts:
            print(f"[{self.ticker}] Company facts not available.")
            return False

        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        print(f"[DEBUG] {self.ticker}: us-gaap concept count = {len(us_gaap)}; "
              f"first 20 keys = {list(us_gaap.keys())[:20]}")

        # -- Discover periods ---------------------------------------------
        print(f"[DEBUG] {self.ticker}: calling _discover_periods(us_gaap, max_years={max_years or 0}) "
              f"to resolve annual periods")
        periods = _discover_periods(us_gaap, max_years or 0) if us_gaap else []
        print(f"[DEBUG] {self.ticker}: periods returned by _discover_periods = {periods}")

        if not us_gaap or not periods:
            if not us_gaap:
                print(f"[{self.ticker}] No us-gaap facts found.")
            else:
                print(f"[{self.ticker}] No annual periods found.")

            # -- Edge-case ingestion fallback chain ------------------------
            if self._load_via_fallback_chain():
                return True   # self.financials/.periods/.market_cap already set
            return False

        self.periods = periods
        print(f"[{self.ticker}] Found {len(periods)} annual periods: "
              f"{periods[0]} -> {periods[-1]}")

        # -- Resolve waterfalls -------------------------------------------
        # Pass ticker + sector so company patches and sector overrides apply
        is_df, is_log = _resolve_waterfall(us_gaap, _IS_WATERFALL, periods,
                                           is_instant=False,
                                           ticker=self.ticker, sector=self.sector,
                                           fine_industry=self._fine_industry or "")
        bs_df, bs_log = _resolve_waterfall(us_gaap, _BS_WATERFALL, periods,
                                           is_instant=True,
                                           ticker=self.ticker, sector=self.sector,
                                           fine_industry=self._fine_industry or "")
        cf_df, cf_log = _resolve_waterfall(us_gaap, _CF_WATERFALL, periods,
                                           is_instant=False,
                                           ticker=self.ticker, sector=self.sector,
                                           fine_industry=self._fine_industry or "")

        self.resolution_log = {
            "income_statement": is_log,
            "balance_sheet":    bs_log,
            "cash_flow":        cf_log,
        }

        # Log unresolved items
        for stmt, log in self.resolution_log.items():
            unresolved = [k for k, v in log.items() if v is None]
            if unresolved:
                logger.info("facts_processor: %s %s -- unresolved: %s",
                            self.ticker, stmt, ", ".join(unresolved))

        # -- Reconciliation -----------------------------------------------
        self.data_flags = _reconcile(is_df, bs_df, cf_df, periods, self.ticker, self.sector)
        if self.data_flags:
            for f in self.data_flags:
                print(f"[{self.ticker}] DATA FLAG: {f}")

        # -- Package output -----------------------------------------------
        # -- Sector revenue adjustments (inline, before storing) ---------
        # These run inside load_data so they apply regardless of which
        # calling script (orchestrator, validate_pipeline, etc.) uses the data.
        if periods:
            p0 = periods[0]

            def _cell_ld(df, label):
                mask = df["standard_concept"] == label
                if not mask.any():
                    return None
                try:
                    v = float(df.loc[mask].iloc[0].get(p0))
                    return v if v == v else None  # NaN check
                except Exception:
                    return None

            def _fill_ld(df, label, val):
                mask = df["standard_concept"] == label
                if not mask.any() or val is None:
                    return
                idx = df.index[mask][0]
                for p in periods:
                    # Scale proportionally from p0 base
                    base = _cell_ld(df, "FinancialServicesRevenue") or 1
                    p_fsr = None
                    try:
                        m = df["standard_concept"] == "FinancialServicesRevenue"
                        if m.any():
                            p_fsr = float(df.loc[m].iloc[0].get(p))
                    except Exception:
                        pass
                    p_ni = None
                    try:
                        m2 = df["standard_concept"] == "NonInterestIncome"
                        if m2.any():
                            p_ni = float(df.loc[m2].iloc[0].get(p))
                    except Exception:
                        pass
                    if p_fsr and p_ni:
                        df.at[idx, p] = p_fsr + abs(p_ni)

            # Bank sum: FinancialServicesRevenue (=InterestIncomeExpenseNet) + NonInterestIncome
            if self.sector == "Financial Services":
                fsr = _cell_ld(is_df, "FinancialServicesRevenue")
                noi = _cell_ld(is_df, "NonInterestIncome")
                rev = _cell_ld(is_df, "Revenue")
                if fsr and fsr > 1e8 and noi and abs(noi) > 1e7:
                    bank_total = fsr + abs(noi)
                    if bank_total > (rev or 0):
                        _fill_ld(is_df, "Revenue", bank_total)
                        # Also set p0 directly
                        m = is_df["standard_concept"] == "Revenue"
                        if m.any():
                            is_df.at[is_df.index[m][0], p0] = bank_total
                        logger.info("[%s] Bank revenue: NII(%.1fB)+NonInt(%.1fB)=%.1fB",
                                    self.ticker, fsr/1e9, noi/1e9, bank_total/1e9)

            # Cross-check: if Revenues > Revenue*1.5 use Revenues (commodity traders)
            rev2 = _cell_ld(is_df, "Revenue")
            revs = _cell_ld(is_df, "Revenues")
            if revs and rev2 and revs > rev2 * 1.5 and revs > 1e8:
                m = is_df["standard_concept"] == "Revenue"
                if m.any():
                    idx = is_df.index[m][0]
                    for p in periods:
                        try:
                            m2 = is_df["standard_concept"] == "Revenues"
                            if m2.any():
                                v = is_df.loc[m2].iloc[0].get(p)
                                if v is not None:
                                    is_df.at[idx, p] = float(v)
                        except Exception:
                            pass
                logger.info("[%s] Revenue cross-check: Revenues(%.1fB) >> Revenue(%.1fB)",
                            self.ticker, revs/1e9, rev2/1e9)

        self.financials = {
            "income_statement": is_df,
            "balance_sheet":    bs_df,
            "cash_flow":        cf_df,
        }

        # -- Market cap (live) --------------------------------------------
        try:
            import yfinance as yf
            self.market_cap = yf.Ticker(self.ticker).info.get("marketCap", 0.0)
        except Exception:
            self.market_cap = 0.0

        # -- Summary ------------------------------------------------------
        resolved_is = sum(1 for v in is_log.values() if v)
        resolved_bs = sum(1 for v in bs_log.values() if v)
        resolved_cf = sum(1 for v in cf_log.values() if v)
        print(f"[{self.ticker}] Resolved: IS={resolved_is}/{len(is_log)}  "
              f"BS={resolved_bs}/{len(bs_log)}  CF={resolved_cf}/{len(cf_log)}  "
              f"periods={len(periods)}")

        return True

    def _load_via_fallback_chain(self) -> bool:
        """
        Called when standard company-facts ingestion yields 0 us-gaap
        concepts or 0 annual periods. Tries progressively more targeted
        recovery paths depending on whether the ticker is a known edge
        case, before giving up.

        On success, self.financials / self.periods / self.market_cap are
        populated directly and this returns True. Returns False if every
        path fails (self.degraded_reason records why).
        """
        ticker = self.ticker

        if ticker in KNOWN_20F_FILERS:
            print(f"[{ticker}] is a 20-F filer — attempting 20-F ingestion")
            if self._try_single_filing_ingestion():
                return True
            print(f"[{ticker}] 20-F ingestion did not yield usable financial data — "
                  f"degrading to market-data-only sections.")
            self.degraded_reason = "20F_NO_DATA"
            return False

        if ticker in KNOWN_DELISTINGS:
            print(f"[{ticker}] — {KNOWN_DELISTINGS[ticker]} — attempting last available 10-K")
            if self._try_single_filing_ingestion():
                return True
            print(f"[{ticker}] no usable filings found — degrading to market-data-only "
                  f"sections. Fundamental/risk/valuation sections will show "
                  f"N/A (company delisted — no current filings).")
            self.degraded_reason = "DELISTED_NO_DATA"
            return False

        # General fallback: any other ticker with 0 us-gaap concepts or 0
        # annual periods -- try the most recently available qualifying
        # filing before giving up entirely.
        print(f"[{ticker}] No annual periods found via standard XBRL facts — "
              f"trying the most recent available filings before giving up...")
        if self._try_single_filing_ingestion():
            return True

        self.degraded_reason = "NO_DATA"
        return False

    def _try_single_filing_ingestion(self) -> bool:
        """
        Best-effort fallback: parse the most recent qualifying annual
        filing directly via RobustDataProcessor (reused, not duplicated),
        which already tries company.latest("10-K") -> company.latest("20-F")
        -> a scan of recent filings for the first clean, non-amended one
        (form=["10-K", "20-F"]). Its .financials output is the same
        {income_statement, balance_sheet, cash_flow} DataFrame shape this
        class produces, so it can be adopted directly.

        Returns True and sets self.financials/.periods/.market_cap on
        success; returns False (leaving this processor's state untouched)
        if no usable data was found.
        """
        try:
            from core.data_layer import RobustDataProcessor, _META_COLS
            rdp = RobustDataProcessor(self.ticker, sector=self.sector, cache=self._cache)
            if not rdp.load_data():
                return False

            is_df = rdp.financials.get("income_statement")
            if is_df is None or is_df.empty:
                return False

            date_cols = [c for c in is_df.columns if c not in _META_COLS]
            if not date_cols:
                return False

            self.financials = rdp.financials
            self.periods    = date_cols
            self.market_cap = rdp.market_cap or self.market_cap
            print(f"[{self.ticker}] Fallback ingestion succeeded: "
                  f"{len(date_cols)} period(s) from single-filing XBRL parse")
            return True
        except Exception as e:
            logger.debug("facts_processor: single-filing fallback failed for %s: %s",
                        self.ticker, e)
            return False

    def print_resolution_log(self) -> None:
        """Print which raw XBRL concept resolved for each line item."""
        for stmt, log in self.resolution_log.items():
            print(f"\n{'='*60}")
            print(f"  {stmt.upper()}")
            print(f"{'='*60}")
            for label, concept in log.items():
                status = concept if concept else "** UNRESOLVED **"
                print(f"  {label:<45s} -> {status}")
