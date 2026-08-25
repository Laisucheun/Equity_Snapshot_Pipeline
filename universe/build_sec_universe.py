"""
build_sec_universe.py -- Build full SEC EDGAR ticker universe with SIC sector mapping

Fetches company_tickers_exchange.json from SEC, filters to US-exchange 10-K filers,
maps SIC codes to pipeline sector taxonomy, and writes ticker_universe.py with all
tickers properly classified.

Usage:
    python build_sec_universe.py --identity "Your Name email@domain.com"
    python build_sec_universe.py --identity "Your Name email@domain.com" --dry-run

After running, ticker_universe.py will contain ~8,000 tickers.
Then run validation with --resume to only process new tickers:
    python validate_pipeline.py --identity "Your Name email@domain.com" --delay 0.5 --resume
"""

import argparse
import json
import logging
import os
import re
import requests
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

# SEC EDGAR endpoints
_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# SIC code to pipeline sector mapping
# Source: SEC SIC code list (https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&SIC=)
# Mapped to match the sector strings in agents.py / facts_processor.py

_SIC_TO_SECTOR = {
    # Technology (SIC 3570-3579, 3660-3679, 3812, 3825, 7370-7379)
    range(3570, 3580): "Technology",      # Computer hardware
    range(3660, 3680): "Technology",      # Communications equipment
    range(3810, 3830): "Technology",      # Search/navigation/measuring instruments
    range(3670, 3680): "Technology",      # Electronic components
    range(3674, 3675): "Technology",      # Semiconductors
    range(7370, 7380): "Technology",      # Computer programming, data processing
    range(3559, 3560): "Technology",      # Special industry machinery (semicon equip)
    range(3825, 3826): "Technology",      # Instruments for measuring
    range(3669, 3670): "Technology",      # Communications equip NEC
    range(3612, 3614): "Technology",      # Power transformers
    range(3620, 3630): "Technology",      # Electrical industrial apparatus
    range(3640, 3650): "Technology",      # Lighting equipment
    range(3690, 3700): "Technology",      # Electronic components NEC
    range(5045, 5046): "Technology",      # Computers and peripherals wholesale
    range(5734, 5735): "Technology",      # Computer and software stores
    range(5961, 5962): "Technology",      # Catalog and mail-order houses (ecommerce)

    # Healthcare (SIC 2830-2836, 2860-2869, 3841-3851, 5912, 8000-8099)
    range(2830, 2837): "Healthcare",      # Drugs
    range(2835, 2837): "Healthcare",      # In vitro diagnostics
    range(2860, 2870): "Healthcare",      # Industrial chemicals (pharma)
    range(3841, 3852): "Healthcare",      # Medical instruments
    range(5912, 5913): "Healthcare",      # Drug stores
    range(8000, 8100): "Healthcare",      # Health services
    range(2833, 2834): "Healthcare",      # Pharmaceutical preparations
    range(3826, 3828): "Healthcare",      # Lab analytical instruments
    range(3829, 3830): "Healthcare",      # Measuring instruments

    # Financial Services (SIC 6000-6799)
    range(6000, 6100): "Financial Services",  # Depository institutions (banks)
    range(6100, 6200): "Financial Services",  # Non-depository credit
    range(6200, 6300): "Financial Services",  # Security brokers/dealers
    range(6300, 6400): "Financial Services",  # Insurance carriers
    range(6400, 6500): "Financial Services",  # Insurance agents
    range(6500, 6553): "Financial Services",  # Real estate (non-REIT)
    range(6553, 6700): "Financial Services",  # Mortgage bankers, holding companies (gap fill)
    range(6700, 6800): "Financial Services",  # Holding/investment offices

    # Real Estate (SIC 6500-6553, 6798)
    # REITs are SIC 6798; other real estate is 6500-6553
    # We override 6798 specifically for REITs
    range(6798, 6799): "Real Estate",

    # Industrials (SIC 3400-3569, 3580-3612, 3700-3799, 4011-4231, 8711-8713)
    range(3400, 3570): "Industrials",     # Metal/machinery/fab
    range(3580, 3613): "Industrials",     # General industrial machinery
    range(3700, 3720): "Industrials",     # Transportation equipment
    range(3720, 3729): "Industrials",     # Aircraft
    range(3730, 3732): "Industrials",     # Ship building
    range(3740, 3744): "Industrials",     # Railroad equipment
    range(3760, 3770): "Industrials",     # Guided missiles/space
    range(3790, 3800): "Industrials",     # Misc transportation equip
    range(4011, 4014): "Industrials",     # Railroads
    range(4100, 4232): "Industrials",     # Transit/trucking/warehousing
    range(4400, 4500): "Industrials",     # Water transportation
    range(4500, 4600): "Industrials",     # Air transportation
    range(4700, 4790): "Industrials",     # Transportation services
    range(7380, 7390): "Industrials",     # Miscellaneous business services
    range(8711, 8744): "Industrials",     # Engineering, accounting, management consulting
    range(3559, 3560): "Industrials",     # Special industry machinery
    range(3540, 3550): "Industrials",     # Metalworking machinery
    range(3550, 3560): "Industrials",     # Special industry machinery
    range(3825, 3826): "Industrials",     # (overlap with Tech - let Tech win)

    # Consumer Discretionary (SIC 2500-2599, 3711-3716, 5000-5199, 5300-5961, 7000-7369, 7800-7999)
    range(2500, 2600): "Consumer Discretionary",  # Furniture
    range(2300, 2400): "Consumer Discretionary",  # Apparel
    range(3140, 3150): "Consumer Discretionary",  # Footwear
    range(3711, 3716): "Consumer Discretionary",  # Motor vehicles
    range(3944, 3950): "Consumer Discretionary",  # Games/toys
    range(5000, 5200): "Consumer Discretionary",  # Durable goods wholesale
    range(5300, 5400): "Consumer Discretionary",  # General merchandise stores
    range(5400, 5500): "Consumer Discretionary",  # Food stores (some)
    range(5500, 5600): "Consumer Discretionary",  # Auto dealers
    range(5600, 5700): "Consumer Discretionary",  # Apparel stores
    range(5700, 5734): "Consumer Discretionary",  # Home furnishing stores
    range(5735, 5800): "Consumer Discretionary",  # Record/music stores
    range(5800, 5900): "Consumer Discretionary",  # Eating/drinking places
    range(5900, 5970): "Consumer Discretionary",  # Retail NEC (extended to cover 5950-5969)
    range(7000, 7100): "Consumer Discretionary",  # Hotels
    range(7200, 7300): "Consumer Discretionary",  # Personal services
    range(7800, 8000): "Consumer Discretionary",  # Amusement/recreation
    range(7300, 7370): "Consumer Discretionary",  # Business services (misc)

    # Consumer Staples (SIC 2000-2111, 2800-2830, 5140-5159, 5400-5499)
    range(2000, 2112): "Consumer Staples",  # Food and kindred products
    range(2100, 2200): "Consumer Staples",  # Tobacco
    range(2800, 2830): "Consumer Staples",  # Chemicals (household/personal)
    range(5140, 5160): "Consumer Staples",  # Groceries wholesale
    range(5411, 5412): "Consumer Staples",  # Grocery stores

    # Energy (SIC 1300-1389, 2911, 4900-4991, 5171-5172)
    range(1300, 1390): "Energy",            # Oil/gas extraction
    range(2911, 2912): "Energy",            # Petroleum refining
    range(5171, 5173): "Energy",            # Petroleum wholesale
    range(1381, 1382): "Energy",            # Drilling oil/gas wells
    range(1382, 1390): "Energy",            # Oil/gas field services

    # Utilities (SIC 4900-4991)
    range(4900, 4992): "Utilities",

    # Materials (SIC 1000-1299, 2200-2299, 2400-2499, 2600-2799, 3200-3399)
    range(1000, 1300): "Materials",         # Mining
    range(2200, 2300): "Materials",         # Textile mill products
    range(2400, 2500): "Materials",         # Lumber/wood
    range(2600, 2800): "Materials",         # Paper/chemicals
    range(3200, 3400): "Materials",         # Stone/clay/glass/metals
    range(3310, 3400): "Materials",         # Primary metals
    range(2810, 2830): "Materials",         # Industrial chemicals
    range(2890, 2900): "Materials",         # Industrial chemicals NEC

    # Communication Services (SIC 4800-4899, 2710-2741, 4810-4841)
    range(4800, 4900): "Communication Services",
    range(2710, 2742): "Communication Services",  # Publishing
    range(4810, 4842): "Communication Services",  # Telephone/telegraph
}


def _sic_to_sector(sic_code: int) -> str:
    """Map a numeric SIC code to the pipeline sector string."""
    # Check specific overrides first
    if sic_code == 6798:
        return "Real Estate"
    # REITs often use 6512, 6500 range too
    if sic_code in (6500, 6510, 6512, 6552, 6553):
        return "Real Estate"

    for sic_range, sector in _SIC_TO_SECTOR.items():
        if sic_code in sic_range:
            return sector
    return "General"


# SIC codes that should be excluded from the validation universe
# 6726 = Investment Offices NEC (crypto trusts, commodity ETFs, closed-end funds)
# 6770 = Blank Checks (SPACs)
# 6221 = Commodity contracts dealers/brokers — commodity ETFs (DBA, USO, UNG, PALL etc.)
# 6199 used by some shell companies but also real lenders -- do NOT exclude broadly
_EXCLUDE_SIC = {
    6726,  # Investment offices NEC — crypto trusts, closed-end funds, ETFs
    6770,  # Blank checks — SPACs
    6221,  # Commodity contracts dealers/brokers — commodity ETFs
}

# ETF/ETP issuer name patterns — catches products that slip through SIC filters
# (SIC varies: 6726, 6221, 6199, 6311 depending on trust structure)
_ETF_ISSUER_RE = re.compile(
    r'^(iShares|Invesco|ProShares|VanEck|WisdomTree|Direxion|'
    r'SPDR|Grayscale|Bitwise|ARK |Fidelity Wise Origin|'
    r'2x |1\.5x |-1x |-2x )',
    re.IGNORECASE
)

# Ticker suffix patterns that identify non-operating securities.
# Matched against the FULL ticker string after uppercasing.
# W  alone is Weight Watchers (WW), not a warrant -- we match trailing W only when
# the base has >= 2 letters before it, e.g. SEATW, CSHRW, PMTW.
_WARRANT_UNIT_RE = re.compile(
    r'^[A-Z]{2,}'    # at least 2-letter base
    r'(?:W|WS|WW|WT|WU|R|U|RT|RW)$'  # warrant/unit/right suffix
)

# Form types that identify foreign private issuers (not 10-K filers)
_FOREIGN_FORMS = {"20-F", "40-F", "20-F/A", "40-F/A"}


def _should_exclude(ticker: str, sic: int, recent_form: str,
                    name: str = "") -> tuple[bool, str]:
    """
    Return (True, reason) if this ticker should be excluded from the universe.
    Checks in order: SIC-based, ETF issuer name, warrant/unit suffix, foreign filer.
    """
    # Crypto trusts, commodity ETFs, SPACs
    if sic in _EXCLUDE_SIC:
        return True, f"SIC {sic} (crypto/ETF/SPAC)"

    # ETF/ETP issuer name patterns — catches products with non-standard SICs
    if name and _ETF_ISSUER_RE.match(name.strip()):
        return True, f"ETF/ETP issuer ({name[:40]})"

    # Warrant, unit, right suffixes
    if _WARRANT_UNIT_RE.match(ticker):
        return True, "warrant/unit/right suffix"

    # Foreign private issuers (20-F / 40-F annual report)
    if recent_form in _FOREIGN_FORMS:
        return True, f"foreign filer ({recent_form})"

    return False, ""


def fetch_sec_tickers(identity: str) -> list[dict]:
    """Fetch all SEC-registered tickers with exchange info."""
    headers = {"User-Agent": identity}

    # Try exchange-aware endpoint first
    try:
        r = requests.get(_TICKERS_EXCHANGE_URL, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        # Format: {"fields": [...], "data": [[cik, name, ticker, exchange], ...]}
        fields = data.get("fields", [])
        rows = data.get("data", [])
        logger.info("Fetched %d tickers from company_tickers_exchange.json", len(rows))

        results = []
        for row in rows:
            entry = dict(zip(fields, row))
            results.append({
                "cik": str(entry.get("cik", "")).zfill(10),
                "ticker": (entry.get("ticker") or "").upper(),
                "name": entry.get("name", ""),
                "exchange": (entry.get("exchange") or "").upper(),
            })
        return results
    except Exception as e:
        logger.warning("Exchange endpoint failed (%s), falling back to basic", e)

    # Fallback: basic ticker list (no exchange info)
    r = requests.get(_TICKERS_URL, headers=headers, timeout=30)
    r.raise_for_status()
    results = []
    for entry in r.json().values():
        results.append({
            "cik": str(entry.get("cik_str", "")).zfill(10),
            "ticker": (entry.get("ticker") or "").upper(),
            "name": entry.get("title", ""),
            "exchange": "",
        })
    logger.info("Fetched %d tickers from company_tickers.json", len(results))
    return results


def fetch_sic_codes(identity: str, cik_list: list[str]) -> dict[str, dict]:
    """
    Batch-fetch SIC codes and most-recent annual filing form type from SEC submissions.
    Returns {cik: {"sic": int, "form": str}} mapping.

    The form field identifies foreign private issuers (20-F, 40-F) so they can be
    excluded from the universe without a separate API call.

    One HTTP call per CIK; ~8 req/sec respects SEC rate limits.
    At ~8000 CIKs this takes roughly 17 minutes.
    """
    import time
    headers = {"User-Agent": identity}
    result = {}
    total = len(cik_list)

    for i, cik in enumerate(cik_list):
        if i % 500 == 0:
            logger.info("Fetching SIC codes: %d / %d", i, total)
        try:
            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                time.sleep(0.12)
                continue
            data = r.json()
            sic = data.get("sic", "")
            if not sic:
                time.sleep(0.12)
                continue

            # Find the most recent annual filing form type
            recent_form = ""
            filings = data.get("filings", {}).get("recent", {})
            forms = filings.get("form", [])
            annual_forms = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
            for form in forms:  # already sorted newest-first by SEC
                if form in annual_forms:
                    recent_form = form
                    break

            result[cik] = {"sic": int(sic), "form": recent_form}
            time.sleep(0.12)
        except Exception:
            pass

    logger.info("Fetched SIC codes for %d / %d CIKs", len(result), total)
    return result


def build_universe(identity: str,
                   existing_file: str = "ticker_universe.py",
                   output_file: str = "ticker_universe_sec.py") -> None:
    """
    Build the full SEC universe and merge with existing tier 1/2/3 entries.
    Existing entries keep their manually-assigned sector and tier.
    New entries get SIC-based sector and tier=4 (SEC universe).
    Writes to ticker_universe_sec.py -- does NOT touch ticker_universe.py.
    """

    # Step 1: Load existing universe to preserve manual classifications
    existing = {}
    if os.path.exists(existing_file):
        # Import the existing module
        import importlib.util
        spec = importlib.util.spec_from_file_location("existing_tu", existing_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for entry in mod.UNIVERSE:
            existing[entry.ticker] = (entry.sector, entry.tier, getattr(entry, "note", ""))
        logger.info("Loaded %d existing entries from %s", len(existing), existing_file)

    # Step 2: Fetch all SEC tickers
    all_tickers = fetch_sec_tickers(identity)

    # Step 3: Filter to US exchanges
    us_exchanges = {"NYSE", "NASDAQ", "CBOE", "BATS", "NYSEAMERICAN", "NYSEARCA"}
    us_tickers = [
        t for t in all_tickers
        if t["exchange"] in us_exchanges
    ]
    # Also keep tickers without exchange info if they look like US tickers
    no_exchange = [t for t in all_tickers if not t["exchange"]]
    logger.info("US exchange tickers: %d, no exchange: %d", len(us_tickers), len(no_exchange))

    # Step 4: Filter out obvious non-operating entities
    # Skip tickers with special characters (warrants, units, preferred)
    valid_ticker_re = re.compile(r'^[A-Z]{1,5}$')
    candidates = {}
    for t in us_tickers:
        ticker = t["ticker"]
        if valid_ticker_re.match(ticker) and ticker not in existing:
            candidates[ticker] = t

    logger.info("New candidates after filtering: %d", len(candidates))

    # Step 5: Fetch SIC codes for new candidates
    cik_list = [t["cik"] for t in candidates.values()]
    cik_to_ticker = {t["cik"]: t["ticker"] for t in candidates.values()}
    cik_to_name   = {t["cik"]: t.get("name", "") for t in candidates.values()}
    sic_map = fetch_sic_codes(identity, cik_list)

    # Step 6: Map SIC to sector, filter exclusions
    new_entries = {}
    sector_counts = {}
    excluded_counts = {}
    for cik, info in sic_map.items():
        ticker = cik_to_ticker.get(cik)
        if not ticker:
            continue
        sic  = info["sic"]
        form = info["form"]
        name = cik_to_name.get(cik, "")
        exclude, reason = _should_exclude(ticker, sic, form, name)
        if exclude:
            excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
            logger.debug("Excluded %s: %s", ticker, reason)
            continue
        sector = _sic_to_sector(sic)
        new_entries[ticker] = sector
        sector_counts[sector] = sector_counts.get(sector, 0) + 1

    logger.info("Excluded %d tickers:", sum(excluded_counts.values()))
    for reason, count in sorted(excluded_counts.items(), key=lambda x: -x[1]):
        logger.info("  %-40s %d", reason, count)
    logger.info("Sector distribution for new entries:")
    for s, c in sorted(sector_counts.items(), key=lambda x: -x[1]):
        logger.info("  %-30s %d", s, c)

    # Step 7: Write ticker_universe_sec.py (does NOT touch ticker_universe.py)
    _write_universe_file(output_file, existing, new_entries)
    logger.info("Wrote %d total entries to %s",
                len(existing) + len(new_entries), output_file)


def _write_universe_file(path: str, existing: dict, new_entries: dict) -> None:
    """Write the full ticker_universe.py with existing + new entries."""

    lines = []
    lines.append('"""')
    lines.append("ticker_universe.py -- Full SEC EDGAR validation universe")
    lines.append("")
    lines.append("Coverage:")
    lines.append("    Tier 1 -- S&P 500 core")
    lines.append("    Tier 2 -- Russell 1000 additions")
    lines.append("    Tier 3 -- Edge cases")
    lines.append("    Tier 4 -- SEC EDGAR active filers (SIC-classified)")
    lines.append('"""')
    lines.append("")
    lines.append("from dataclasses import dataclass, field")
    lines.append("")
    lines.append("")
    lines.append("@dataclass")
    lines.append("class TickerEntry:")
    lines.append("    ticker: str")
    lines.append("    sector: str")
    lines.append("    tier:   int")
    lines.append('    note:   str = ""')
    lines.append("")
    lines.append("")
    lines.append("# Existing tiers 1-3 (manually classified)")
    lines.append("_EXISTING = [")

    # Write existing entries sorted by tier then ticker
    sorted_existing = sorted(existing.items(), key=lambda x: (x[1][1], x[0]))
    for ticker, (sector, tier, note) in sorted_existing:
        if note:
            lines.append(f'    TickerEntry("{ticker}", "{sector}", {tier}, "{note}"),')
        else:
            lines.append(f'    TickerEntry("{ticker}", "{sector}", {tier}),')
    lines.append("]")
    lines.append("")
    lines.append("")

    # Write new SEC universe entries grouped by sector
    lines.append("# SEC EDGAR active filers (tier 4, SIC-classified)")
    lines.append("_SEC_UNIVERSE = [")

    from collections import defaultdict
    by_sector = defaultdict(list)
    for ticker, sector in sorted(new_entries.items()):
        by_sector[sector].append(ticker)

    for sector in sorted(by_sector.keys()):
        tickers = by_sector[sector]
        lines.append(f"    # {sector} ({len(tickers)})")
        # Write 4 per line
        for i in range(0, len(tickers), 4):
            chunk = tickers[i:i+4]
            entries = ", ".join(f'("{t}",{" " * max(1, 5-len(t))}"{sector}")' for t in chunk)
            lines.append(f"    {entries},")
        lines.append("")
    lines.append("]")
    lines.append("")
    lines.append("")

    # Write build_universe function
    lines.append("def build_universe() -> list[TickerEntry]:")
    lines.append('    """Return full universe. Existing entries override SEC tier."""')
    lines.append("    seen: dict[str, TickerEntry] = {}")
    lines.append("")
    lines.append("    # Tier 4 first (lowest priority)")
    lines.append("    for ticker, sector in _SEC_UNIVERSE:")
    lines.append("        t = ticker.upper()")
    lines.append("        if t not in seen:")
    lines.append("            seen[t] = TickerEntry(t, sector, 4)")
    lines.append("")
    lines.append("    # Existing entries override (higher priority)")
    lines.append("    for entry in _EXISTING:")
    lines.append("        seen[entry.ticker.upper()] = entry")
    lines.append("")
    lines.append("    return sorted(seen.values(), key=lambda e: (e.tier, e.ticker))")
    lines.append("")
    lines.append("")
    lines.append("UNIVERSE = build_universe()")
    lines.append("")
    lines.append("")
    lines.append("def get_by_sector(sector: str) -> list[TickerEntry]:")
    lines.append("    return [e for e in UNIVERSE if e.sector == sector]")
    lines.append("")
    lines.append("")
    lines.append("def get_by_tier(tier: int) -> list[TickerEntry]:")
    lines.append("    return [e for e in UNIVERSE if e.tier == tier]")
    lines.append("")
    lines.append("")
    lines.append("def summary() -> None:")
    lines.append("    from collections import Counter")
    lines.append("    tier_counts   = Counter(e.tier   for e in UNIVERSE)")
    lines.append("    sector_counts = Counter(e.sector for e in UNIVERSE)")
    lines.append('    print(f"Total tickers: {len(UNIVERSE)}")')
    lines.append('    print("\\nBy tier:")')
    lines.append("    for tier in sorted(tier_counts):")
    lines.append('        label = {1: "S&P 500", 2: "Russell 1000", 3: "Edge cases", 4: "SEC universe"}.get(tier, f"Tier {tier}")')
    lines.append('        print(f"  Tier {tier} ({label}): {tier_counts[tier]}")')
    lines.append('    print("\\nBy sector:")')
    lines.append("    for sector, count in sector_counts.most_common():")
    lines.append('        print(f"  {sector:<30} {count:>5}")')
    lines.append("")
    lines.append("")
    lines.append('if __name__ == "__main__":')
    lines.append("    summary()")
    lines.append("")

    with open(path, "w", encoding="ascii", errors="replace") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build full SEC ticker universe")
    parser.add_argument("--identity", required=True, help="SEC User-Agent identity")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report, don't write")
    args = parser.parse_args()

    build_universe(args.identity)
