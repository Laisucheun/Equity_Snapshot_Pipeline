"""
ticker_universe_20f.py — Test universe for 20-F foreign filers

Purpose: Validate whether the waterfall can accurately resolve financial
line items for foreign private issuers (20-F filers), even though the
full report pipeline (orchestrator) excludes them due to missing 10-Q/8-K
coverage.

Categories tested:
  - European / developed market ADRs (NVO, ASML, AZN, etc.)
  - China ADRs via Cayman structure (BIDU, BABA, NIO, etc.)
  - Other Asia / LatAm ADRs (TM, HDB, VALE, etc.)

Usage:
  1. Copy this file to your project directory
  2. Temporarily rename it to ticker_universe.py
  3. Run: python validate_pipeline.py --identity "sc sc@gmail.com" --delay 0.5
  4. Outputs go to phase2_output_20f/ (rename OUTPUT_DIR in validate_pipeline.py
     or just move outputs after the run)
  5. Restore your real ticker_universe.py when done
"""

from dataclasses import dataclass
from typing import List


@dataclass
class TickerEntry:
    ticker: str
    sector: str
    tier: int


UNIVERSE: List[TickerEntry] = [
    # ── European / Developed Market ADRs ─────────────────────────────────────
    TickerEntry("NVO",  "Healthcare",          5),  # Novo Nordisk (Denmark)
    TickerEntry("ASML", "Technology",          5),  # ASML (Netherlands)
    TickerEntry("SAP",  "Technology",          5),  # SAP (Germany)
    TickerEntry("AZN",  "Healthcare",          5),  # AstraZeneca (UK)
    TickerEntry("BP",   "Energy",              5),  # BP (UK)
    TickerEntry("RIO",  "Materials",           5),  # Rio Tinto (UK/Australia)
    TickerEntry("BHP",  "Materials",           5),  # BHP (Australia)
    TickerEntry("UL",   "Consumer Staples",    5),  # Unilever (UK)
    TickerEntry("DEO",  "Consumer Staples",    5),  # Diageo (UK)
    TickerEntry("BTI",  "Consumer Staples",    5),  # British American Tobacco
    TickerEntry("GSK",  "Healthcare",          5),  # GSK (UK)
    TickerEntry("SNY",  "Healthcare",          5),  # Sanofi (France)
    TickerEntry("TTE",  "Energy",              5),  # TotalEnergies (France)
    TickerEntry("SHEL", "Energy",              5),  # Shell (UK/Netherlands)
    TickerEntry("NGG",  "Utilities",           5),  # National Grid (UK)
    TickerEntry("SAN",  "Financial Services",  5),  # Banco Santander (Spain)
    TickerEntry("BBVA", "Financial Services",  5),  # BBVA (Spain)
    TickerEntry("ING",  "Financial Services",  5),  # ING Group (Netherlands)

    # ── China ADRs (Cayman/VIE structure, file 20-F) ─────────────────────────
    TickerEntry("BIDU", "Technology",          5),  # Baidu
    TickerEntry("BABA", "Consumer Discretionary", 5),  # Alibaba
    TickerEntry("JD",   "Consumer Discretionary", 5),  # JD.com
    TickerEntry("PDD",  "Consumer Discretionary", 5),  # PDD Holdings (Temu)
    TickerEntry("TCOM", "Consumer Discretionary", 5),  # Trip.com
    TickerEntry("NIO",  "Industrials",         5),  # NIO
    TickerEntry("XPEV", "Industrials",         5),  # XPeng
    TickerEntry("LI",   "Industrials",         5),  # Li Auto
    TickerEntry("VIPS", "Consumer Discretionary", 5),  # Vipshop
    TickerEntry("BILI", "Communication Services", 5),  # Bilibili
    TickerEntry("NTES", "Technology",          5),  # NetEase
    TickerEntry("FUTU", "Financial Services",  5),  # Futu Holdings
    TickerEntry("HUYA", "Technology",          5),  # Huya (gaming streams)
    TickerEntry("IQ",   "Communication Services", 5),  # iQIYI
    TickerEntry("MOMO", "Technology",          5),  # Hello Group
    TickerEntry("ZTO",  "Industrials",         5),  # ZTO Express
    TickerEntry("NOAH", "Financial Services",  5),  # Noah Holdings
    TickerEntry("RLX",  "Consumer Staples",    5),  # RLX Technology (e-cig)
    TickerEntry("QFIN", "Financial Services",  5),  # 360 DigiTech
    TickerEntry("FINV", "Financial Services",  5),  # FinVolution
    TickerEntry("ATHM", "Technology",          5),  # Autohome
    TickerEntry("HTHT", "Consumer Discretionary", 5),  # H World Group (hotels)
    TickerEntry("BEKE", "Real Estate",         5),  # KE Holdings (Beike)

    # ── Other Asia / LatAm ADRs ──────────────────────────────────────────────
    TickerEntry("TM",   "Industrials",         5),  # Toyota (Japan)
    TickerEntry("HDB",  "Financial Services",  5),  # HDFC Bank (India)
    TickerEntry("KB",   "Financial Services",  5),  # KB Financial (Korea)
    TickerEntry("WF",   "Financial Services",  5),  # Woori Financial (Korea)
    TickerEntry("SHG",  "Financial Services",  5),  # Shanghai Commercial Bank
    TickerEntry("NMR",  "Financial Services",  5),  # Nomura (Japan)
    TickerEntry("IX",   "Financial Services",  5),  # ORIX (Japan)
    TickerEntry("VALE", "Materials",           5),  # Vale (Brazil)
    TickerEntry("SID",  "Materials",           5),  # CSN (Brazil)
    TickerEntry("GFI",  "Materials",           5),  # Gold Fields (South Africa)
    TickerEntry("FRO",  "Industrials",         5),  # Frontline (Norway/tankers)
    TickerEntry("BUR",  "Financial Services",  5),  # Burford Capital (UK)
]


def get_by_ticker(ticker: str) -> TickerEntry | None:
    for e in UNIVERSE:
        if e.ticker == ticker:
            return e
    return None


def get_by_sector(sector: str) -> List[TickerEntry]:
    return [e for e in UNIVERSE if e.sector == sector]


def get_by_tier(tier: int) -> List[TickerEntry]:
    return [e for e in UNIVERSE if e.tier == tier]


def summary():
    from collections import Counter
    print(f"Total tickers: {len(UNIVERSE)}")
    print("\nBy sector:")
    for s, c in Counter(e.sector for e in UNIVERSE).most_common():
        print(f"  {s:30} {c}")
