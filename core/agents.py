"""
agents.py — Four analyst agents for the equity brief pipeline.

Each agent receives a CompanyFinancialProfile plus optional enrichment data,
and returns a structured dict consumed by the PDF renderer.

Sector routing is handled inside each agent — no new classes, just conditional
logic. The sector string is read from profile.sector and mapped to a
SECTOR_GROUP which drives which metrics are computed vs suppressed.

Agents:
    FundamentalAgent     — margins, CAGR, ROE, ROA, ROIC proxy, op. leverage
    RiskAgent            — liquidity, solvency, D/E trend, Altman Z-Score
    ValuationAgent       — P/E and P/B (historical FY-end price + current price)
    TrendCommentaryAgent — deterministic narrative + MD&A guidance from 10-K
"""

import re
import csv
import json
import logging
import pathlib

logger = logging.getLogger(__name__)
import numpy as np


# ─────────────────────────────────────────────
# Sector grouping
# ─────────────────────────────────────────────

# Known financial-sector tickers — fallback when sector string not provided
_FINANCIAL_TICKERS = {
    "JPM","BAC","WFC","GS","MS","C","BNY","BK","USB","PNC","TFC","COF",
    "SCHW","AXP","BLK","STT","NTRS","MTB","RF","CFG","HBAN","KEY","FITB",
    "ZION","CMA","SNV","WAL","SIVB","FRC","PACW","WBS","UMBF","BOH",
    # Insurance
    "MET","PRU","AIG","LNC","UNM","AFL","CNO","GL","AIZ","RNR","MKL",
    "ALL","CB","HIG","PGR","TRV",                    # P&C insurers
    # Asset managers / private equity
    "BEN","IVZ","AMG","WDR","TROW","APAM","LAZ","EVR","MC","HLI",
    "AMP","APO","BX",                                # wealth mgmt / alt managers
    # Specialty finance / payments
    "DFS","SYF",                                     # consumer finance
}

# Managed care / health insurers — medical claims costs (PolicyBenefitsAndClaims)
# are the dominant cost of service and belong in the gross-margin cost base
# alongside any tagged CostOfGoodsAndServicesSold (e.g. pharmacy/product costs).
# Left in the "general" COGS branch, these companies' derived gross margin is
# based only on the (often minor) product-cost tag, understating true cost of
# service by 40-75pp vs. consensus. Ticker-based (not sector-string-based)
# because "Healthcare" sector also contains device makers, pharma, etc. that
# should NOT get this treatment.
_MANAGED_CARE_TICKERS = {"CNC", "UNH", "ELV", "CI", "CVS", "MOH", "HUM"}

# Freight brokers — non-asset-based logistics companies whose revenue is
# largely a pass-through to purchased/contracted carriers. Their XBRL
# filings don't break out "purchased transportation" as a standard us-gaap
# concept (custom extension tags aren't exposed via the companyfacts API),
# so there is no reliable way to derive a true "net revenue" COGS figure.
# Economically there is little daylight between gross and operating margin
# for a pure broker (almost all cost sits above operating income), so
# operating margin is used as the best available proxy.
_FREIGHT_BROKER_TICKERS = {"CHRW", "EXPD"}


def _sector_group(sector: str, ticker: str = "") -> str:
    """
    Map a sector string to a routing group.
    Returns: 'financials' | 'energy' | 'utilities' | 'real_estate' |
             'managed_care' | 'freight_broker' | 'general'

    'utilities' is separate from 'energy' because:
      - Regulated utilities should not show Operating Cost Ratio (an E&P metric)
      - Altman Z warrants its own suppression note (model calibrated for manufacturers,
        not regulated utilities with structurally low working capital and high leverage)
      - Interest coverage and D/E are meaningful and should be computed normally

    'real_estate' is intentionally NOT in the financials group — REITs have different
    characteristics from banks/insurers and should receive the full general ratio set
    (D/E, Current Ratio, Altman Z). They will score low on Z-score due to structural
    leverage, which is expected rather than a bug. Gross margin specifically IS
    suppressed (see FundamentalAgent) — REITs have no COGS line and consensus
    providers report NOI margin, not a comparable gross margin.
    """
    s = (sector or "").lower()
    ticker_u = ticker.upper()
    # Ticker-based fallbacks — catch companies whose sector string alone
    # wouldn't route them correctly (business-model exceptions within a
    # broader sector, or banks run without an explicit sector).
    if ticker_u in _FINANCIAL_TICKERS:
        return "financials"
    if ticker_u in _MANAGED_CARE_TICKERS:
        return "managed_care"
    if ticker_u in _FREIGHT_BROKER_TICKERS:
        return "freight_broker"

    # Fintech and payments companies share sector labels containing "financ" but
    # are NOT banks — they should receive the full general ratio set.
    # This exclusion must run before the financials check.
    if any(k in s for k in ["fintech", "payments", "financial technology",
                             "financial services technology"]):
        return "general"
    if any(k in s for k in ["financ", "bank", "insurance"]):
        return "financials"
    if any(k in s for k in ["utilities", "utility"]):
        return "utilities"
    if "real estate" in s:
        return "real_estate"
    if any(k in s for k in ["energy", "oil", "gas", "mining"]):
        return "energy"
    return "general"


# ─────────────────────────────────────────────
# Sector-appropriate metric suppression (Section 1)
# ─────────────────────────────────────────────
#
# Display-only: nothing here changes what FundamentalAgent computes. Every
# metric below is still calculated (and remains available for internal
# cross-checks, e.g. risk flags) -- this map only tells the renderer which
# rows are not meaningful to *show* for a given industry, plus the reason,
# rendered as a "Basis of Omission" footnote in Section 1.
#
# Keyed by fine-grained SIC industry (data.facts_processor._sic_to_industry,
# e.g. "realestate", "banking") so industries that share a sector_group
# routing bucket but aren't equally suppressible (e.g. banking vs. insurance
# both route to sg == "financials", but only banking's Operating Margin is
# meaningfully replaced by Efficiency Ratio) get distinct treatment.
_METRIC_SUPPRESSION = {

    "realestate": {
        "suppress": [
            "Gross Margin", "COGS / Revenue", "Inventory Turnover",
            "Days Sales of Inventory", "FCF Margin",
        ],
        "footnotes": {
            "Gross Margin": "Not meaningful for REITs — revenue is "
                "primarily rental income with no cost of goods sold in "
                "the traditional sense. Use FFO/AFFO margin instead.",
            "FCF Margin": "Replaced by FFO and AFFO margin for REITs "
                "(NAREIT standard).",
            "Inventory Turnover": "Not applicable — REITs do not carry "
                "inventory.",
            "Days Sales of Inventory": "Not applicable — REITs do not "
                "carry inventory.",
        },
    },

    "banking": {
        "suppress": [
            "Gross Margin", "COGS / Revenue", "Inventory Turnover",
            "Days Sales of Inventory", "FCF Margin", "Operating Margin",
            "ROIC",
        ],
        "footnotes": {
            "Gross Margin": "Not meaningful for banks — revenue is net "
                "interest income and fees, not product sales. Use Net "
                "Interest Margin and Efficiency Ratio instead.",
            "Operating Margin": "Replaced by Efficiency Ratio for "
                "financial institutions.",
            "Inventory Turnover": "Not applicable — banks do not carry "
                "inventory.",
            "Days Sales of Inventory": "Not applicable — banks do not "
                "carry inventory.",
            "FCF Margin": "Not a standard metric for banks — use Return "
                "on Equity and Tier 1 Capital Ratio.",
            "ROIC": "Not a standard metric for banks — regulatory "
                "capital ratios are the relevant capital efficiency "
                "measure.",
        },
    },

    "insurance": {
        "suppress": [
            "Gross Margin", "COGS / Revenue", "Inventory Turnover",
            "Days Sales of Inventory", "ROIC",
        ],
        "footnotes": {
            "Gross Margin": "Not meaningful for insurers — premiums "
                "earned less claims is captured by the combined ratio, "
                "not gross margin.",
            "Inventory Turnover": "Not applicable — insurers do not "
                "carry inventory.",
            "Days Sales of Inventory": "Not applicable — insurers do not "
                "carry inventory.",
            "ROIC": "Not a standard metric for insurers — use Return on "
                "Equity and combined ratio.",
        },
    },

    "energy": {
        "suppress": ["Inventory Turnover", "Days Sales of Inventory"],
        "footnotes": {
            "Inventory Turnover": "Less meaningful for E&P companies — "
                "commodity inventory dynamics differ from manufacturing.",
            "Days Sales of Inventory": "Less meaningful for E&P "
                "companies.",
        },
    },

    "utilities": {
        "suppress": [
            "Gross Margin", "Inventory Turnover", "Days Sales of Inventory",
        ],
        "footnotes": {
            "Gross Margin": "Not standard for regulated utilities — cost "
                "recovery is set by regulators. Use operating margin and "
                "EBITDA margin instead.",
            "Inventory Turnover": "Not applicable — utilities do not "
                "carry significant product inventory.",
            "Days Sales of Inventory": "Not applicable — utilities do "
                "not carry significant product inventory.",
        },
    },

    "transportation": {
        "suppress": [
            "Gross Margin", "Inventory Turnover", "Days Sales of Inventory",
        ],
        "footnotes": {
            "Gross Margin": "Not standard for airlines and transportation "
                "companies — cost structure is primarily operating "
                "expenses. Use operating margin instead.",
            "Inventory Turnover": "Not applicable — transportation is a "
                "service business.",
            "Days Sales of Inventory": "Not applicable — transportation "
                "is a service business.",
        },
    },

    "hospitality": {
        "suppress": ["Inventory Turnover", "Days Sales of Inventory"],
        "footnotes": {
            "Inventory Turnover": "Not meaningful for hotels and gaming "
                "— service-based revenue model.",
            "Days Sales of Inventory": "Not meaningful for hotels and "
                "gaming — service-based revenue model.",
        },
    },

    "telecom": {
        "suppress": ["Inventory Turnover", "Days Sales of Inventory"],
        "footnotes": {
            "Inventory Turnover": "Less meaningful for telecom — "
                "primarily a service business.",
            "Days Sales of Inventory": "Not applicable for "
                "service-dominant revenue.",
        },
    },

    "mining": {
        "suppress": ["Days Sales of Inventory"],
        "footnotes": {
            "Days Sales of Inventory": "Less meaningful for mining — "
                "commodity inventory timing varies with production "
                "cycles.",
        },
    },

    "tech":       {"suppress": [], "footnotes": {}},
    "healthcare": {"suppress": [], "footnotes": {}},
}

# sector_group (_sector_group() output) -> _METRIC_SUPPRESSION key, used
# only when fine_industry is unavailable (SIC lookup failed) or doesn't
# match a key above. Coarser than fine_industry -- e.g. it can't tell a
# bank from an insurer -- so fine_industry is always tried first.
_SECTOR_GROUP_SUPPRESSION_FALLBACK = {
    "financials":   "banking",
    "real_estate":  "realestate",
    "energy":       "energy",
    "utilities":    "utilities",
}


def _get_metric_suppression(fine_industry: str | None, sector_group: str) -> dict:
    """Suppression rules for this ticker: fine_industry first, then a
    coarser sector_group fallback, else no suppression."""
    if fine_industry and fine_industry in _METRIC_SUPPRESSION:
        return _METRIC_SUPPRESSION[fine_industry]
    fallback_key = _SECTOR_GROUP_SUPPRESSION_FALLBACK.get(sector_group)
    if fallback_key and fallback_key in _METRIC_SUPPRESSION:
        return _METRIC_SUPPRESSION[fallback_key]
    return {"suppress": [], "footnotes": {}}


# ─────────────────────────────────────────────
# Working-capital industry routing (Section 2B)
# ─────────────────────────────────────────────
#
# Keyed by fine-grained SIC industry, same convention as _METRIC_SUPPRESSION.
#   "full"    — every working-capital and capital-intensity metric
#   "partial" — inventory metrics (DIO, Inv/Revenue, Inv/Assets) omitted;
#               these industries either carry no inventory at all (transport,
#               telecom) or carry commodity inventory whose turnover dynamics
#               don't describe a working-capital cycle (energy, utilities)
#   "skip"    — Section 2B not rendered: banks/insurers have no operating
#               working-capital cycle (their balance sheet IS the business),
#               and REITs hold real property rather than trade working capital
_WC_SUPPRESSION = {
    # Full working capital analysis
    "general":          "full",
    "retail":           "full",
    "tech":             "full",
    "semiconductors":   "full",
    "healthcare":       "full",
    "mining":           "full",
    "hospitality":      "full",

    # Partial — skip inventory metrics
    "energy":           "partial",   # no inventory turnover
    "transportation":   "partial",   # no inventory
    "telecom":          "partial",   # no inventory

    # Skip entirely — not meaningful
    "realestate":       "skip",
    "banking":          "skip",
    "insurance":        "skip",
    "utilities":        "partial",   # AR/AP only
}

# sector_group -> mode, used when fine_industry is unavailable (SIC lookup
# failed) or isn't a key above. Coarser -- it can't tell a bank from an
# insurer -- so fine_industry is always tried first.
_WC_MODE_FALLBACK = {
    "financials":     "skip",
    "real_estate":    "skip",
    "managed_care":   "partial",   # health insurers: no product inventory cycle
    "utilities":      "partial",
    "energy":         "partial",
    "freight_broker": "partial",   # non-asset-based: no inventory
    "general":        "full",
}


def _get_wc_mode(fine_industry: str | None, sector_group: str) -> str:
    """Working-capital display mode for this ticker: fine_industry first,
    then a coarser sector_group fallback, else the full metric set."""
    if fine_industry and fine_industry in _WC_SUPPRESSION:
        return _WC_SUPPRESSION[fine_industry]
    return _WC_MODE_FALLBACK.get(sector_group, "full")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _safe_div(num, den, fallback="N/A"):
    try:
        if den and den != 0:
            return num / den
    except Exception:
        pass
    return fallback


def _pct(val, decimals=2):
    if isinstance(val, (int, float)):
        return f"{val * 100:.{decimals}f}%"
    return "N/A"


def _fmt_x(val, decimals=2):
    if isinstance(val, (int, float)):
        return f"{val:.{decimals}f}x"
    return "N/A"


def _safe_val(arr, i):
    """
    arr[i] as a plain float, or None if out of range or falsy. This data
    layer's np.ndarray properties use 0.0 as the sentinel for an
    unresolved XBRL tag (see _get_vector/_get_max_vector), so a falsy
    value here means "unresolved," not "genuinely zero" -- callers get
    None either way and treat it as N/A.
    """
    try:
        v = arr[i]
        return float(v) if v else None
    except (IndexError, TypeError, ValueError):
        return None


def _yfinance_gross_margin_fallback(ticker: str) -> tuple:
    """
    Last-resort gross margin source for the most recent period, used only
    when SEC XBRL data doesn't resolve a comparable figure at all --
    either the filer doesn't tag a GAAP gross-profit/COGS line (common for
    telecoms, defense contractors, diversified conglomerates -- e.g. T,
    RTX, DIS) or the sector's own metric isn't directly comparable
    (Utilities, REITs). Prefers annual Gross Profit / Total Revenue (same
    fiscal-year basis as the rest of this report) over the TTM
    `grossMargins` field, falling back to TTM only if annual data isn't
    available. Never raises -- returns (None, "") on any failure so the
    caller shows a clean N/A instead.

    Returns (margin_float_or_None, source_label_str).
    """
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        fin = tk.financials
        if fin is not None and not fin.empty and "Gross Profit" in fin.index and "Total Revenue" in fin.index:
            gp  = fin.loc["Gross Profit"].iloc[0]
            rev = fin.loc["Total Revenue"].iloc[0]
            if gp and rev:
                return float(gp) / float(rev), "yfinance annual"
        ttm = tk.info.get("grossMargins")
        if ttm is not None:
            return float(ttm), "yfinance TTM"
    except Exception:
        pass
    return None, ""


def _yfinance_info_cache(ticker: str) -> dict:
    """
    Fetches yfinance's .info dict once per ticker per process, for reuse
    across the several last-resort fallback fields below (operating
    margin, net margin, ROE, ROA). Never raises -- returns {} on failure.
    """
    if ticker in _YF_INFO_CACHE:
        return _YF_INFO_CACHE[ticker]
    info = {}
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception:
        pass
    _YF_INFO_CACHE[ticker] = info
    return info


_YF_INFO_CACHE: dict = {}


def _yfinance_diluted_shares_cache(ticker: str) -> dict:
    """
    Fetches yfinance's annual income statement "Diluted Average Shares" row
    once per ticker per process. yfinance only carries ~4 fiscal years
    (vs. this pipeline's 5 XBRL periods), so the oldest period is typically
    absent -- callers must fall back to XBRL diluted_shares for any period
    not present here. Never raises -- returns {} on failure.

    Returns {fiscal_year_int: diluted_share_count_float}.
    """
    if ticker in _YF_DILUTED_SHARES_CACHE:
        return _YF_DILUTED_SHARES_CACHE[ticker]
    out = {}
    try:
        import yfinance as yf
        ist = yf.Ticker(ticker).income_stmt
        if ist is not None and "Diluted Average Shares" in ist.index:
            row = ist.loc["Diluted Average Shares"]
            for col, val in row.items():
                try:
                    fval = float(val)
                except (TypeError, ValueError):
                    continue
                if fval != fval:  # NaN
                    continue
                out[col.year] = fval
    except Exception:
        pass
    _YF_DILUTED_SHARES_CACHE[ticker] = out
    return out


_YF_DILUTED_SHARES_CACHE: dict = {}


def _yfinance_line_item_fallback(ticker: str, info_key: str) -> tuple:
    """
    Last-resort fallback for a single .info field (operatingMargins,
    profitMargins, returnOnEquity, returnOnAssets), used only when the
    SEC-XBRL-based computation for that line item is N/A. Never raises --
    returns (None, "") on any failure so the caller shows a clean N/A
    instead. Returns (value_float_or_None, source_label_str).
    """
    info = _yfinance_info_cache(ticker)
    val = info.get(info_key)
    if val is None:
        return None, ""
    try:
        return float(val), "yfinance TTM"
    except (TypeError, ValueError):
        return None, ""


def _yfinance_ratio_fallback(ticker: str, info_key: str, divide_by: float = 1.0) -> tuple:
    """
    Same as _yfinance_line_item_fallback, but for raw-multiple fields
    (current ratio, D/E) rather than percentages -- formatted with
    _fmt_x() ("1.23x") by the caller instead of _pct(). yfinance reports
    debtToEquity on a 0-100+ scale (e.g. 78.4 meaning 0.78x), so divide_by
    lets the caller normalise to a raw multiple; currentRatio/quickRatio
    are already raw multiples (divide_by=1.0).
    """
    info = _yfinance_info_cache(ticker)
    val = info.get(info_key)
    if val is None:
        return None, ""
    try:
        return float(val) / divide_by, "yfinance TTM"
    except (TypeError, ValueError, ZeroDivisionError):
        return None, ""


# Cell sentinel for a period whose per-share/EV metrics were suppressed
# because its share count is as-filed (pre-split) while prices are
# split-adjusted. Deliberately NOT one of renderer._is_na_cell's
# "structural" strings, so the row stays visible with the reason shown.
_PRESPLIT_NA = "N/A (pre-split)"


# ─────────────────────────────────────────────
# Red flag post-processing (collapse + ordering)
# ─────────────────────────────────────────────

# Matches the shape every per-period threshold flag is written in:
#   "<metric> <value> (<year>) <below|above ...> — <explanation>"
# e.g. "Current ratio 0.79x (2026) below 1.0x threshold — current
# liabilities exceed current assets"
_FLAG_PERIOD_RE = re.compile(
    r"^(?P<head>.*?)(?P<val>-?[\d,]+(?:\.\d+)?\s*[x%])\s+"
    r"\((?P<yr>[A-Za-z0-9]{2,8})\)\s+(?P<tail>.+)$"
)

# Period labels as they appear in flag text, for ordering flags that aren't
# threshold-shaped. Covers the single form "(2026)" / "(FY26)" and the
# transition form "(2025→2026)" used by the YoY change flags. Deliberately
# anchored to parentheses so a bare four-digit number elsewhere in the text
# (a maturity year, a dollar amount) isn't mistaken for the period label.
_FLAG_YEAR_RE = re.compile(
    r"\(((?:FY)?\d{2,4})(?:\s*(?:→|->|-)\s*((?:FY)?\d{2,4}))?\)"
)

# Substrings that mark a flag as high-severity, used as the secondary sort
# key within each recency bucket.
_FLAG_SEVERITY_KEYWORDS = (
    "net loss", "distress", "data error", "negative", "exceeds operating cash",
    "contraction", "below 0%", "impairment", "combined signal",
    "interest coverage", "refinancing cliff", "pre-split",
)


def _collapse_repeated_flags(flags: list, periods: list) -> list:
    """
    Collapse near-identical per-period threshold flags into one line naming
    the range, so a condition true in every year reads as one structural
    finding instead of five near-duplicate bullets.

    Grouping is on the flag text with the value and year removed, so it is
    metric-agnostic: any flag written as "<metric> <value> (<year>)
    <condition> — <explanation>" collapses, whatever the metric.
    """
    if not flags:
        return flags

    # _fy() label -> position in the period list (0 = most recent)
    order = {_fy(p): i for i, p in enumerate(periods)}
    total = len(periods)

    groups: dict = {}
    passthrough: list = []
    for idx, f in enumerate(flags):
        m = _FLAG_PERIOD_RE.match(f)
        if not m or m.group("yr") not in order:
            passthrough.append((idx, f))
            continue
        key = (m.group("head"), m.group("tail"))
        groups.setdefault(key, []).append((idx, m))

    out: list = []
    for idx, f in passthrough:
        out.append((idx, f))

    for (head, tail), members in groups.items():
        if len(members) < 2:
            out.append((members[0][0], flags[members[0][0]]))
            continue

        members.sort(key=lambda t: order[t[1].group("yr")])   # newest first
        vals = []
        for _, m in members:
            try:
                vals.append((float(m.group("val")[:-1].replace(",", "")),
                             m.group("val")))
            except ValueError:
                vals.append((0.0, m.group("val")))
        lo = min(vals, key=lambda v: v[0])[1]
        hi = max(vals, key=lambda v: v[0])[1]

        cond, _, expl = tail.partition(" — ")
        n = len(members)
        span = f"all {total} years" if n == total else f"{n} of {total} years"

        # Direction of "worse" is read from the condition itself: a
        # below-threshold breach worsens as the value falls, an
        # above-threshold breach as it rises.
        newest_v = vals[0][0]
        oldest_v = vals[-1][0]
        if "below" in cond:
            worsening = newest_v < oldest_v
        elif "above" in cond:
            worsening = newest_v > oldest_v
        else:
            worsening = None
        if worsening is True:
            trend = "; and deteriorating across the window"
        elif worsening is False:
            trend = "; structural, not deteriorating"
        else:
            trend = ""

        collapsed = f"{head}{cond} in {span} ({lo}–{hi})"
        if expl:
            collapsed += f" — {expl}{trend}"
        elif trend:
            collapsed += f" —{trend[1:]}"
        # Sort position: keep it after current-period findings.
        out.append((members[0][0] + len(flags), collapsed))

    out.sort(key=lambda t: t[0])
    return [f for _, f in out]


def _sort_flags_by_recency(flags: list, periods: list) -> list:
    """
    Order flags: current-period findings first, then undated ones (trend
    and data-quality flags), then multi-year/structural and older-period
    findings. Severity keywords break ties within each bucket; original
    order breaks the rest, so the sort is stable and reproducible.
    """
    if not flags or not periods:
        return flags

    current_label = _fy(periods[0])
    older_labels  = {_fy(p) for p in periods[1:]}

    def bucket(f: str) -> int:
        # findall returns a tuple per match (single label, transition label);
        # flatten and drop the empty second group.
        years = {y for pair in _FLAG_YEAR_RE.findall(f) for y in pair if y}
        if " years (" in f:          # collapsed multi-year finding
            return 2
        if current_label in years:
            return 0
        if years & older_labels:
            return 2
        return 1                     # undated: trend / data-quality flags

    def severity(f: str) -> int:
        fl = f.lower()
        return 0 if any(k in fl for k in _FLAG_SEVERITY_KEYWORDS) else 1

    return [f for _, _, _, f in
            sorted(((bucket(f), severity(f), i, f) for i, f in enumerate(flags)))]


def _resolve_diluted_shares(profile, periods: list, ticker: str,
                            current_price: float | None = None,
                            market_cap: float | None = None) -> tuple[dict, dict]:
    """
    Per-period diluted share count plus the source each one came from.

    yfinance annual "Diluted Average Shares" is primary (pre-cleaned, no
    unit-tagging ambiguity) and is split-adjusted for its whole history;
    the SEC XBRL waterfall is the fallback for any period yfinance doesn't
    cover (it only carries ~4 fiscal years vs. this pipeline's 5) and is
    as-filed, i.e. NOT retroactively split-adjusted.

    That asymmetry is what _detect_split_contamination() keys off, so the
    source label must be recorded honestly per period. Extracted from
    ValuationAgent so the split detector, the agent, and the offline
    generality sweep all resolve shares through one code path.

    Returns ({period: shares_or_None}, {period: "yfinance"|"xbrl"|"implied (mkt cap / price)"}).
    """
    shares_rows: dict = {}
    shares_source: dict = {}

    _yf_diluted_by_year = _yfinance_diluted_shares_cache(ticker) if ticker else {}
    inc = profile.income_statement

    for i, p in enumerate(periods):
        # Primary: yfinance annual "Diluted Average Shares", matched by
        # fiscal year.
        _yf_year = int(p[:4]) if p and p[:4].isdigit() else None
        shares = _yf_diluted_by_year.get(_yf_year) if _yf_year is not None else None
        if shares and shares > 0:
            shares_source[p] = "yfinance"
        else:
            shares = _safe_val(inc.diluted_shares, i)
            shares_source[p] = "xbrl"
        # Fallback 1: derive shares from market cap / current price when
        # neither yfinance nor the XBRL filing tag a diluted count (e.g. OXY).
        if (not shares or shares == 0) and current_price and market_cap and current_price > 0:
            shares = market_cap / current_price
            shares_source[p] = "implied (mkt cap / price)"
        # Fallback 2: unit correction — some filers (e.g. MCD) report shares
        # in millions (721.9) rather than actual count (721,900,000).
        # Detected by comparing to implied share count from market data:
        # if XBRL shares is off by ~1,000,000x, scale it up.
        if shares and shares > 0 and current_price and market_cap and current_price > 0:
            implied = market_cap / current_price
            if implied > 0:
                ratio = implied / shares
                if 500_000 <= ratio <= 2_000_000:
                    shares = shares * 1_000_000
        shares_rows[p] = shares

    return shares_rows, shares_source


def _detect_split_contamination(shares_by_period: dict,
                                periods: list,
                                share_sources: dict) -> set:
    """
    Return the set of periods whose share count is likely pre-split
    relative to the most recent period.

    A ratio jump of >3x or <0.33x between adjacent periods indicates a
    split (or reverse split) occurred. Every period older than that
    boundary is contaminated when its share count came from XBRL
    (as-filed, never retroactively adjusted) while the FY-end prices this
    pipeline divides by come from yfinance and ARE split-adjusted.

    Purely ratio-driven: no split calendar, no per-ticker table. A period
    whose share count came from yfinance is never contaminated, because
    that source is split-adjusted on both sides of the ratio.

    The 3x/0.33x bounds are deliberately wide. Real share-count changes
    from buybacks or issuance are single-digit percentages a year; even an
    aggressive equity raise rarely triples a count in one year, so a jump
    this large is a split with very few false positives. The narrowest
    common split (2-for-1) sits below the bound and is missed by design --
    tightening to catch it would start flagging ordinary issuance.
    """
    contaminated = set()
    split_found = False
    for i in range(len(periods) - 1):
        newer = shares_by_period.get(periods[i])
        older = shares_by_period.get(periods[i + 1])
        if not newer or not older or older == 0:
            continue
        ratio = newer / older
        if ratio > 3 or ratio < 0.33:
            split_found = True
        if split_found:
            # This and all older periods are suspect
            contaminated.add(periods[i + 1])

    # Only as-filed (XBRL) counts are actually contaminated -- a period
    # sourced from yfinance is already split-adjusted and consistent with
    # the price series.
    return {p for p in contaminated if share_sources.get(p) == "xbrl"}


# Plausible range for a real diluted share count. Same band ValuationAgent
# uses to reject implausible counts before computing CFO/Share; reused here
# to decide which side of a unit-error boundary is the mis-tagged one.
_SHARES_PLAUSIBLE_LO = 1_000_000
_SHARES_PLAUSIBLE_HI = 500_000_000_000


def _share_unit_scale(ratio: float) -> int | None:
    """
    The power of 1000 a share-count discontinuity sits on, or None.

    Matches both directions: the older period tagged too small (ratio ~
    scale) and too large (ratio ~ 1/scale). Note the second test is
    (1/ratio)/scale, NOT scale/ratio -- the latter is algebraically the
    same condition as the first (scale/ratio in [0.98, 1.02] means ratio
    is within 2% of scale), so it would never catch the inverse direction.
    """
    if not ratio or ratio <= 0:
        return None
    for scale in (1000, 1_000_000):
        if 0.98 <= ratio / scale <= 1.02 or 0.98 <= (1 / ratio) / scale <= 1.02:
            return scale
    return None


def _classify_share_anomaly(ratio: float) -> str:
    """
    Classify a share-count discontinuity between adjacent periods.

    Real splits are small integer ratios (2-for-1 through the 50-for-1 end
    of the range seen in practice). Share-count unit errors -- a filing
    that tagged one year's weighted-average count in thousands or millions
    rather than units -- land on a near-exact power of 1000, because that
    is what the unit change does arithmetically.

    Anything else (a ratio that is neither a plausible split nor a clean
    power of 1000 -- e.g. a unit error compounded with a split) stays
    "unknown" and is suppressed rather than guessed at.

    Returns "unit_error", "likely_split", or "unknown".
    """
    if _share_unit_scale(ratio) is not None:
        return "unit_error"
    if 1.5 <= ratio <= 60 or 1 / 60 <= ratio <= 1 / 1.5:
        return "likely_split"
    return "unknown"


def _resolve_share_anomalies(shares_by_period: dict, periods: list,
                             share_sources: dict, ticker: str = "") -> dict:
    """
    Classify every share-count discontinuity, repair the repairable ones,
    and return what still has to be suppressed.

    A unit error is a data defect with a known correction -- multiply the
    mis-scaled period back by the power of 1000 it was tagged at -- so the
    period's per-share metrics become usable rather than being thrown
    away. A split has no such correction available here (the as-filed
    count is simply on the other side of the split from the price series),
    so those periods are still suppressed, as is anything unclassifiable.

    Walks newest-first so each comparison is against an already-corrected
    anchor, which lets a run of consecutive mis-scaled years be repaired.
    Only XBRL-sourced periods are touched; yfinance counts are already
    split-adjusted and consistent with the price series.

    Returns
    -------
    {
      "shares":       {period: count}   -- corrections applied
      "contaminated": set(periods)      -- still unsafe, suppress these
      "classes":      {period: "unit_error"|"likely_split"|"unknown"}
      "ratios":       {period: float}
      "corrections":  {period: {"scale": int, "raw": float, "corrected": float}}
    }
    """
    shares = dict(shares_by_period)
    classes: dict = {}
    ratios: dict = {}
    corrections: dict = {}

    for i in range(len(periods) - 1):
        newer_p, older_p = periods[i], periods[i + 1]
        newer = shares.get(newer_p)
        older = shares.get(older_p)
        if not newer or not older or older == 0:
            continue
        ratio = newer / older
        if 0.33 <= ratio <= 3:
            continue                      # no discontinuity at this boundary

        kind = _classify_share_anomaly(ratio)
        classes[older_p] = kind
        ratios[older_p]  = ratio

        if kind == "unit_error":
            # Which SIDE of the boundary is mis-scaled is not knowable from
            # the ratio alone -- the older period may be tagged in thousands,
            # or the newer one may be. Decide with the same share-count
            # plausibility band the rest of the pipeline uses: the side that
            # falls outside it is the mis-tagged one. If both or neither look
            # implausible the boundary is left alone and the re-detection
            # pass below suppresses it.
            scale = _share_unit_scale(ratio) or 1000
            older_bad = (not _SHARES_PLAUSIBLE_LO < older < _SHARES_PLAUSIBLE_HI
                         and share_sources.get(older_p) == "xbrl")
            newer_bad = (not _SHARES_PLAUSIBLE_LO < newer < _SHARES_PLAUSIBLE_HI
                         and share_sources.get(newer_p) == "xbrl")

            if older_bad and not newer_bad:
                bad_p, raw, anchor = older_p, older, newer
            elif newer_bad and not older_bad:
                bad_p, raw, anchor = newer_p, newer, older
            else:
                continue        # ambiguous -- fall through to suppression

            corrected = raw * scale if raw < anchor else raw / scale
            shares[bad_p] = corrected
            corrections[bad_p] = {"scale": scale, "raw": raw,
                                  "corrected": corrected}
            if ticker:
                _op = "×" if raw < anchor else "÷"
                print(f"[{ticker}] share unit correction: {bad_p} "
                      f"{raw:,.0f} {_op} {scale:,} = {corrected:,.0f}")
            continue

        # Splits and unclassifiable breaks: only as-filed counts are
        # suspect; a yfinance-sourced period is already on the same basis
        # as the price series.
        if share_sources.get(older_p) != "xbrl":
            continue

        if ticker:
            print(f"[{ticker}] share anomaly ({kind}): {older_p} "
                  f"ratio={ratio:,.4g} — suppressing per-share/EV metrics")

    # Re-run the detector once on the corrected series. Anything still
    # flagged did not clear (a correction that didn't hold, or a genuine
    # split) and falls back to suppression.
    contaminated = _detect_split_contamination(shares, periods, share_sources)
    for p in contaminated:
        classes.setdefault(p, "unknown")
        if p in corrections and ticker:
            print(f"[{ticker}] share unit correction did not clear the "
                  f"anomaly for {p} — falling back to suppression")

    return {"shares": shares, "contaminated": contaminated,
            "classes": classes, "ratios": ratios, "corrections": corrections}


def _split_boundary_info(shares_by_period: dict, periods: list) -> dict:
    """
    Implied split factor and boundary for the footnote, derived from the
    same adjacent-period ratio the detector uses. Returns {} when no
    boundary is present.
    """
    for i in range(len(periods) - 1):
        newer = shares_by_period.get(periods[i])
        older = shares_by_period.get(periods[i + 1])
        if not newer or not older or older == 0:
            continue
        ratio = newer / older
        if ratio > 3:
            return {"factor": round(ratio, 1), "direction": "split",
                    "after": periods[i], "before": periods[i + 1]}
        if ratio < 0.33:
            return {"factor": round(1 / ratio, 1), "direction": "reverse split",
                    "after": periods[i], "before": periods[i + 1]}
    return {}


def _compute_working_capital(profile, periods: list, mode: str) -> dict:
    """
    Per-period working-capital and capital-intensity metrics (Section 2B).

    `mode` comes from _get_wc_mode(): "full" computes everything,
    "partial" leaves the inventory-derived series (DIO, Inv/Revenue,
    Inv/Assets) empty, and "skip" computes nothing at all. Only the
    inventory series are gated on mode -- CCC still nets DSO against DPO
    in partial mode, with the (absent) DIO term treated as zero.

    Every value is a plain float or None; formatting is the renderer's job.
    None means "not computable from filed XBRL", never zero -- _safe_val
    already collapses this data layer's 0.0 unresolved-tag sentinel to None.

    CapEx is read through abs(): filers tag capital expenditures with either
    sign (as an investing-activity outflow or as a positive additions line),
    and the CapEx/Revenue and CapEx/CFO intensity ratios are only meaningful
    on the magnitude. Same convention CashFlowProfile.free_cash_flow uses.
    """
    _KEYS = ("dso", "dpo", "dio", "ccc", "nwc", "nwc_pct_rev",
             "ar_pct_rev", "ap_pct_rev", "inv_pct_rev", "inv_pct_ta",
             "ppe_pct_ta", "capex_pct_rev", "capex_pct_cfo")
    out: dict = {k: {} for k in _KEYS}
    out["mode"] = mode

    if mode == "skip" or not periods:
        return out

    inc = profile.income_statement
    bal = profile.balance_sheet
    cf  = profile.cash_flow

    # Hoist the vectors -- these are properties that re-run the XBRL lookup
    # on every attribute access.
    ar_v    = bal.accounts_receivable
    ap_v    = bal.accounts_payable
    inv_v   = bal.inventory
    ca_v    = bal.current_assets
    cl_v    = bal.current_liabilities
    ppe_v   = bal.ppe_net
    ta_v    = bal.total_assets
    rev_v   = inc.revenue
    cogs_v  = inc.cogs
    capex_v = cf.capital_expenditures
    cfo_v   = cf.operating_cash_flow

    _show_inventory = (mode == "full")

    for i, p in enumerate(periods):
        ar    = _safe_val(ar_v, i)
        ap    = _safe_val(ap_v, i)
        inv   = _safe_val(inv_v, i)
        rev   = _safe_val(rev_v, i)
        cogs  = _safe_val(cogs_v, i)
        ca    = _safe_val(ca_v, i)
        cl    = _safe_val(cl_v, i)
        ppe   = _safe_val(ppe_v, i)
        ta    = _safe_val(ta_v, i)
        capex = _safe_val(capex_v, i)
        cfo   = _safe_val(cfo_v, i)

        capex_abs = abs(capex) if capex is not None else None

        # ── Cycle days ────────────────────────────────────────────────────
        dso = (ar / rev * 365) if (ar and rev and ar > 0 and rev > 0) else None

        # DPO denominator: COGS preferred, revenue as fallback for filers
        # that don't break out a cost-of-sales line (service businesses).
        dpo_den = cogs if (cogs and cogs > 0) else (rev if (rev and rev > 0) else None)
        dpo = (ap / dpo_den * 365) if (ap and ap > 0 and dpo_den) else None

        # DIO: same guards as FundamentalAgent's DSI -- negative net
        # inventory (customer advances exceeding gross inventory) and
        # negative COGS (loss provisions) both make the ratio meaningless.
        if _show_inventory and inv and cogs and inv > 0 and cogs > 0:
            dio = inv / cogs * 365
        else:
            dio = None

        if dio is not None or dso is not None or dpo is not None:
            ccc = (dio or 0) + (dso or 0) - (dpo or 0)
        else:
            ccc = None

        # ── Balance-sheet intensity ───────────────────────────────────────
        nwc = (ca - cl) if (ca and cl) else None

        out["dso"][p]         = dso
        out["dpo"][p]         = dpo
        out["dio"][p]         = dio
        out["ccc"][p]         = ccc
        out["nwc"][p]         = nwc
        out["nwc_pct_rev"][p] = (nwc / rev * 100) if (nwc is not None and rev) else None
        out["ar_pct_rev"][p]  = (ar / rev * 100)  if (ar and rev) else None
        out["ap_pct_rev"][p]  = (ap / rev * 100)  if (ap and rev) else None
        out["inv_pct_rev"][p] = (inv / rev * 100) if (_show_inventory and inv and rev) else None
        out["inv_pct_ta"][p]  = (inv / ta * 100)  if (_show_inventory and inv and ta)  else None
        out["ppe_pct_ta"][p]  = (ppe / ta * 100)  if (ppe and ta) else None
        out["capex_pct_rev"][p] = (capex_abs / rev * 100) if (capex_abs and rev) else None
        out["capex_pct_cfo"][p] = (capex_abs / cfo * 100) if (capex_abs and cfo) else None

    return out


def _compute_shareholder_returns(profile, periods: list, sg: str,
                                 affo_by_period: dict | None = None) -> dict:
    """
    Dividends, buybacks and payout ratios per FY period.

    Only the parts that don't need market data live here. The three yield
    rows (dividend / buyback / total shareholder yield) and the diluted
    share-count change are computed in ValuationAgent instead, because they
    need the FY-end market cap it already derives for fy_ev and must honour
    the same split/unit-anomaly suppression -- recomputing either here
    would duplicate that work and risk the two disagreeing.

    Payout denominator is FCF, except for REITs with AFFO available, where
    AFFO is the sector-standard distribution base (GAAP FCF for a REIT is
    distorted by real-estate D&A -- the same reason CFO metrics are
    suppressed for the sector). `payout_basis` records which was used so
    the renderer can label the row.

    Filers tag financing outflows with either sign; every figure here is
    taken on magnitude and re-signed by meaning (dividends and buybacks are
    always outflows, issuance always an inflow).
    """
    cf  = profile.cash_flow
    inc = profile.income_statement

    div_v   = cf.dividends_paid
    rep_v   = cf.share_repurchases
    iss_v   = cf.share_issuance
    fcf_v   = cf.free_cash_flow
    ni_v    = inc.net_income

    affo_by_period = affo_by_period or {}
    use_affo = (sg == "real_estate"
                and any(isinstance(v, (int, float)) for v in affo_by_period.values()))

    # Payout denominator follows the same sector routing the rest of the
    # report uses. Banks and insurers get earnings: FCF is already
    # suppressed for financials everywhere else (FCF Margin, EV/FCF,
    # FCF/EV) because CapEx is not their capital-allocation lever, so a
    # payout ratio against it is meaningless -- it reads in the hundreds of
    # percent for every healthy bank and would fire the "exceeds FCF" flag
    # universally.
    if use_affo:
        basis = "AFFO"
    elif sg == "financials":
        basis = "Earnings"
    else:
        basis = "FCF"

    out: dict = {
        "dividends_paid": {}, "buybacks": {}, "share_issuance": {},
        "net_buyback": {}, "payout_ratio_earnings": {},
        "payout_ratio_fcf": {}, "total_payout_fcf": {},
        "payout_basis": basis,
        "available": False,
    }

    for i, p in enumerate(periods):
        div = _safe_val(div_v, i)
        rep = _safe_val(rep_v, i)
        iss = _safe_val(iss_v, i)
        ni  = _safe_val(ni_v, i)

        div = abs(div) if div else None
        rep = abs(rep) if rep else None
        iss = abs(iss) if iss else None

        net_bb = None
        if rep is not None or iss is not None:
            net_bb = (rep or 0) - (iss or 0)

        if basis == "AFFO":
            base = affo_by_period.get(p)
            base = base if isinstance(base, (int, float)) else None
        elif basis == "Earnings":
            base = ni
        else:
            base = _safe_val(fcf_v, i)

        out["dividends_paid"][p] = div
        out["buybacks"][p]       = rep
        out["share_issuance"][p] = iss
        out["net_buyback"][p]    = net_bb
        out["payout_ratio_earnings"][p] = (div / ni * 100) if (div and ni and ni > 0) else None
        out["payout_ratio_fcf"][p]      = (div / base * 100) if (div and base and base > 0) else None
        out["total_payout_fcf"][p] = (
            ((div or 0) + (net_bb or 0)) / base * 100
            if (base and base > 0 and (div or net_bb)) else None
        )

    # The whole block is suppressed for companies that neither pay a
    # dividend nor buy back stock in any period (many growth names).
    out["available"] = any(
        out["dividends_paid"].get(p) or out["buybacks"].get(p)
        for p in periods
    )
    return out


def _compute_sbc_metrics(profile, periods: list) -> dict:
    """
    Stock-based compensation and FCF restated to treat it as a cash cost.

    Source: the existing NonCashStockComp CF label (added for the AFFO
    work) via CashFlowProfile.non_cash_stock_comp, falling back to the
    existing StockBasedCompensationExpense IS label via
    IncomeStatementProfile.stock_comp. Both are reused as-is -- no new
    label, no extension. Measured coverage across a 20-ticker sector
    spread: 15/20 resolve on those two labels, with the non-resolvers
    being banks/utilities/energy that file no SBC line at all.

    Deliberately NOT used: the StockBasedCompensationCF label. Its second
    candidate is
    EmployeeServiceShareBasedCompensationAllocationOfRecognizedPeriodCostsCapitalizedAmount
    -- the portion of SBC CAPITALIZED into inventory or software, i.e.
    explicitly the part NOT expensed. For any filer where its first
    candidate misses, that label returns the capitalized amount and would
    understate SBC. Confirmed live: it resolves for A, CMG and PLD
    alongside a much larger true SBC figure. Same class of error as
    InterestCostsCapitalized and PaymentsOfDividendsMinorityInterest.

    SBC is a non-cash add-back and filers tag it with either sign; it is
    normalised to a positive magnitude before being subtracted from FCF.
    """
    out: dict = {"sbc": {}, "sbc_pct_revenue": {}, "sbc_pct_cfo": {},
                 "fcf_after_sbc": {}, "fcf_after_sbc_margin": {},
                 "available": False}

    cf  = profile.cash_flow
    inc = profile.income_statement
    sbc_cf_v = cf.non_cash_stock_comp
    sbc_is_v = inc.stock_comp
    rev_v    = inc.revenue
    cfo_v    = cf.operating_cash_flow
    fcf_v    = cf.free_cash_flow

    for i, p in enumerate(periods):
        sbc = _safe_val(sbc_cf_v, i)
        if sbc is None:
            sbc = _safe_val(sbc_is_v, i)
        sbc = abs(sbc) if sbc else None

        rev = _safe_val(rev_v, i)
        cfo = _safe_val(cfo_v, i)
        fcf = _safe_val(fcf_v, i)

        out["sbc"][p] = sbc
        out["sbc_pct_revenue"][p] = (sbc / rev * 100) if (sbc and rev and rev > 0) else None
        out["sbc_pct_cfo"][p]     = (sbc / cfo * 100) if (sbc and cfo and cfo > 0) else None
        # FCF after SBC needs both; SBC / Revenue above stands on its own
        # when FCF doesn't resolve.
        if sbc is not None and fcf is not None:
            after = fcf - sbc
            out["fcf_after_sbc"][p] = after
            out["fcf_after_sbc_margin"][p] = (after / rev * 100) if (rev and rev > 0) else None
        else:
            out["fcf_after_sbc"][p] = None
            out["fcf_after_sbc_margin"][p] = None

    out["available"] = any(out["sbc"].get(p) for p in periods)
    return out


def _compute_yoy_and_cagr(values: dict, periods: list) -> dict:
    """
    YoY % change and CAGR for one metric series.

    values  : {period: float}  -- non-numeric entries (the diagnostic
              "N/A (...)" strings these dicts also carry) are treated as
              missing, same as None.
    periods : ordered most-recent first.

    YoY  : (current - prior) / abs(prior) * 100
    CAGR : (most_recent / oldest) ** (1 / n_years) - 1

    Guards:
      - skip when the prior value is 0 or missing
      - skip when the two values have different signs (a negative base makes
        a percentage change meaningless)
      - CAGR only when >= 3 periods resolve, and only from a positive base
        to a positive end value
    """
    def _num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    yoy = {}
    for i in range(len(periods) - 1):
        curr_p = periods[i]
        prev_p = periods[i + 1]
        curr = _num(values.get(curr_p))
        prev = _num(values.get(prev_p))
        if (curr is not None and prev is not None
                and prev != 0
                and (curr >= 0) == (prev >= 0)):   # same sign
            yoy[curr_p] = (curr - prev) / abs(prev) * 100
        else:
            yoy[curr_p] = None

    # CAGR from oldest to most recent
    cagr = None
    valid_periods = [p for p in periods if _num(values.get(p)) is not None]
    if len(valid_periods) >= 3:
        newest = _num(values[valid_periods[0]])
        oldest = _num(values[valid_periods[-1]])
        n = len(valid_periods) - 1
        if oldest and oldest > 0 and newest > 0:
            cagr = (newest / oldest) ** (1 / n) - 1

    return {"yoy": yoy, "cagr": cagr, "n_years": max(len(valid_periods) - 1, 0)}


def _cagr(start, end, years):
    try:
        if start and start > 0 and end and years > 0:
            return (end / start) ** (1 / years) - 1
    except Exception:
        pass
    return None


def _fy(period_str: str) -> str:
    """'2025-12-31 (FY)' -> 'FY25'  Used in flag messages for compact period labels."""
    try:
        return f"FY{period_str[2:4]}" if "(FY)" in period_str else period_str[:4]
    except Exception:
        return str(period_str)


# ─────────────────────────────────────────────
# Config loader — pipeline_config.csv
# ─────────────────────────────────────────────
#
# All thresholds, benchmarks, and domain-tunable values live in
# pipeline_config.csv (same directory as this file).  Edit the 'value'
# column to adjust when flags fire without touching any code.
# Unknown keys are ignored; invalid values fall back to the defaults below.

_CONFIG_DEFAULTS: dict[str, float] = {
    # ── General solvency / leverage ───────────────────────────────────────
    "de_ratio":                   3.0,    # D/E above this → high leverage flag
    "gross_margin_drop_bps":    500.0,    # bps YoY GM drop → compression flag
    "current_ratio":              1.0,    # below → liquidity flag
    "interest_coverage":          2.0,    # below → debt service flag
    "revenue_cagr_negative":      0.0,    # below → top-line contraction flag
    # ── Profitability ─────────────────────────────────────────────────────
    "net_margin_negative":        0.0,    # below → net loss flag
    "roe_benchmark_financials":   0.12,   # below → financials underperformance note
    "roe_benchmark_general":      0.20,   # above → 'strong returns' note
    # ── Financials-specific ───────────────────────────────────────────────
    "nim_floor":                  0.01,   # NIM below → yield compression flag
    "efficiency_ratio_high":      0.75,   # above → elevated cost flag
    "efficiency_ratio_peer_low":  0.50,   # peer range lower bound (display only)
    "efficiency_ratio_peer_high": 0.65,   # peer range upper bound (display only)
    # ── Energy-specific ───────────────────────────────────────────────────
    "op_cost_ratio_high":         0.85,   # above → margin pressure flag
    # ── Valuation ─────────────────────────────────────────────────────────
    "ev_ebitda_premium":         30.0,    # above → premium valuation flag
    "pe_current_premium":        45.0,    # above → premium valuation flag
    # ── Operating leverage ────────────────────────────────────────────────
    "op_leverage_distortion":    15.0,    # |ratio| above → flag as non-representative
    # ── Guidance tone ─────────────────────────────────────────────────────
    "tone_positive_threshold":    0.20,   # score above → 'Confident'
    "tone_negative_threshold":   -0.20,   # score below → 'Cautious'
}


def _load_thresholds(config_path: str | None = None) -> dict[str, float]:
    """
    Load pipeline_config.csv from the same directory as agents.py.
    Falls back to _CONFIG_DEFAULTS for any missing or unparseable value.
    Safe to call at import time — never raises.
    """
    thresholds = dict(_CONFIG_DEFAULTS)

    if config_path is None:
        config_path = pathlib.Path(__file__).parent.parent / "pipeline_config.csv"

    try:
        with open(config_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("key") or "").strip()
                val = (row.get("value") or "").strip()
                if key in thresholds and val:
                    try:
                        thresholds[key] = float(val)
                    except ValueError:
                        pass   # leave default intact
        print(f"[config] Thresholds loaded from {config_path}")
    except FileNotFoundError:
        print(f"[config] pipeline_config.csv not found — using built-in defaults")
    except Exception as exc:
        print(f"[config] Warning loading config ({exc}) — using built-in defaults")

    return thresholds


RED_FLAG_THRESHOLDS: dict[str, float] = _load_thresholds()


# ─────────────────────────────────────────────
# Red Flag thresholds
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# 1. FundamentalAgent
# ─────────────────────────────────────────────

class FundamentalAgent:
    """
    Sector-aware profitability and activity metrics.

    Sector routing:
        general    — gross margin, op margin, net margin, ROE, ROA, ROIC, turnover
        financials — NIM proxy, efficiency ratio, net margin, ROE, ROA
                     (gross margin and op margin suppressed — meaningless for banks)
        energy     — operating cost ratio (CostsSubtotal / revenue), net margin,
                     ROE, ROA, ROIC, asset turnover
                     (labelled "Operating Cost Ratio" not "Gross Margin")

    Output keys (consistent across all sectors — suppressed fields = "N/A (sector)"):
        periods, gross_margin (or sector equivalent), gross_margin_label,
        operating_margin, net_margin, roe, roa, roic_proxy,
        revenue_cagr, asset_turnover, inventory_turnover,
        operating_leverage, sector_group, flags
    """

    def analyze(self, profile, fr_y9c_data: dict = None, ownership: dict = None,
               insider_activity: dict = None, short_interest: dict = None) -> dict:
        """
        Parameters
        ----------
        profile          : CompanyFinancialProfile
        fr_y9c_data      : optional dict from FRY9CFetcher.fetch() — Basel III ratios
                           keyed by period string. Only used for financials sector.
        insider_activity : optional dict from InsiderTransactionLoader.fetch()
        short_interest    : optional dict from ShortInterestLoader.fetch()
        """
        inc = profile.income_statement
        bal = profile.balance_sheet
        periods = profile.periods
        ticker = getattr(profile, 'ticker', '')
        sg = _sector_group(profile.sector, ticker)
        fine_industry = getattr(profile, "fine_industry", None)
        _metric_suppression = _get_metric_suppression(fine_industry, sg)
        # Section 2B — working capital & capital intensity. Computed here
        # (rather than in a separate agent) so it travels with the rest of
        # the fundamental output to both the renderer and
        # TrendCommentaryAgent, which raises the working-capital red flags.
        _wc_mode = _get_wc_mode(fine_industry, sg)
        working_capital = _compute_working_capital(profile, periods, _wc_mode)

        gross_margin = {}
        gross_margin_label = "Gross Margin"   # overridden per sector
        op_margin    = {}
        net_margin   = {}
        roe          = {}
        roa          = {}
        roic         = {}
        asset_turn   = {}
        inv_turn     = {}
        fcf_margin   = {}
        ebitda_margin= {}
        dsi          = {}
        efficiency_ratio = {}
        rotce        = {}
        flags        = []
        sga_pct      = {}   # SG&A as % of revenue
        rd_pct       = {}   # R&D as % of revenue
        effective_tax= {}   # effective tax rate
        dso          = {}   # days sales outstanding
        dpo          = {}   # days payable outstanding
        ccc          = {}   # cash conversion cycle
        net_debt     = {}   # net debt (absolute $)
        net_debt_ebitda = {}  # net debt / EBITDA
        ebitda_coverage = {}  # EBITDA interest coverage
        bvps         = {}   # book value per share
        roce         = {}   # return on capital employed
        ffo_margin   = {}   # FFO margin (REITs only)
        ffo_rows     = {}   # FFO dollar value (REITs only)
        affo_rows    = {}   # AFFO dollar value (REITs only)
        affo_margin  = {}   # AFFO margin (REITs only)
        ffo_components  = {}   # per-period FFO component breakdown (REITs only)
        affo_components = {}   # per-period AFFO component breakdown (REITs only)

        for i, p in enumerate(periods):
            rev  = inc.revenue[i]
            gp   = inc.gross_profit[i]
            cogs = inc.cogs[i]
            # For Real Estate (REITs), use net_income_continuing to exclude
            # large discontinued-ops disposal gains/losses (e.g. CCI FY24).
            if sg == "real_estate":
                ni = inc.net_income_continuing[i]
            else:
                ni   = inc.net_income[i]
            oi   = inc.operating_income[i]
            ta   = bal.total_assets[i]
            eq   = bal.equity[i]
            inv  = bal.inventory[i]
            debt = bal.total_debt[i]

            # ── Sector: Financials ────────────────────────────────────────
            if sg == "financials":
                gross_margin_label = "Net Interest Margin"
                # NIM = NII / interest-earning assets (IEA).
                # Large US bank 10-Ks do not tag a consolidated IEA line in XBRL
                # balance sheets — it only appears in the supplemental rate/volume
                # table in the MD&A, which is not machine-readable via standard tags.
                # We collect every IEA component that IS tagged (loans, AFS/HTM
                # securities, CB deposits, Fed funds sold, trading assets), compute
                # NIM on that partial denominator, and flag which components were
                # missing so readers can gauge the understatement.
                # If NO components resolve, NIM is marked N/A — no proxy is applied.
                nii = inc.net_interest_income[i]
                if i == 0:
                    # Only compute component map once per run (same for all periods
                    # since _get_vector pulls the full period vector each call)
                    _iea = bal.iea_components()
                    _iea_totals  = _iea['total']
                    _iea_found   = _iea['found']
                    _iea_missing = _iea['missing']

                iea_val = _iea_totals[i] if i < len(_iea_totals) else 0.0

                if nii and iea_val and iea_val > 0:
                    nim_val = _safe_div(nii, iea_val)
                    if isinstance(nim_val, float):
                        # Annotate with coverage: e.g. "2.31% (loans+AFS)"
                        found_short = '+'.join(
                            f.replace(' securities', '').replace('CB ', '')
                             .replace('Fed funds sold/repo', 'repo')
                             .replace('IEA (direct tag)', 'direct')
                            for f in _iea_found
                        )
                        gross_margin[p] = f"{nim_val * 100:.2f}% ({found_short})"
                        if nim_val < RED_FLAG_THRESHOLDS["nim_floor"]:
                            flags.append(
                                f"NIM {_pct(nim_val)} ({_fy(p)}) below "
                                f"{_pct(RED_FLAG_THRESHOLDS['nim_floor'])} floor — "
                                f"earning asset yield severely compressed"
                            )
                        # Flag missing components so analyst knows denominator is partial
                        if _iea_missing and i == 0:
                            missing_str = ', '.join(_iea_missing)
                            flags.append(
                                f"NIM denominator incomplete — XBRL missing: {missing_str}. "
                                f"NIM is overstated vs. reported; obtain avg IEA from "
                                f"bank's supplemental rate/volume table for exact figure."
                            )
                    else:
                        gross_margin[p] = "N/A"
                else:
                    gross_margin[p] = "N/A (no XBRL tag)"
                    if i == 0:
                        flags.append(
                            "NIM not computed — no interest-earning asset tags resolved "
                            "in XBRL. Obtain avg IEA from bank supplemental disclosures."
                        )

                # Operating margin suppressed for banks
                op_margin[p] = "N/A (financials)"

                # Net margin: use financial_revenue (NII + NonInterestIncome)
                # The Revenue tag for banks captures only one segment, not consolidated
                fin_rev = inc.financial_revenue[i]
                denom = fin_rev if (fin_rev and fin_rev > 0) else rev
                if denom and denom > 0 and ni:
                    net_margin[p] = _pct(_safe_div(ni, denom))
                else:
                    net_margin[p] = "N/A"

                # NIM compression is flagged after the loop, on the most
                # recent YoY transition only -- see the margin-change block
                # below. (The old in-loop version compared against the
                # PREVIOUS iteration, which is the NEWER period given this
                # loop runs newest-first, and so reported the change
                # backwards.)

            # ── Sector: Energy ────────────────────────────────────────────
            elif sg == "energy":
                gross_margin_label = "Operating Cost Ratio"
                # Use CostsSubtotal (total operating costs) as cost line
                # cogs property in data_layer already includes CostsSubtotal as fallback
                if cogs and rev and rev != 0:
                    op_cost_ratio = _safe_div(cogs, rev)
                    gross_margin[p] = _pct(op_cost_ratio)
                    if isinstance(op_cost_ratio, float):
                        if op_cost_ratio > RED_FLAG_THRESHOLDS["op_cost_ratio_high"]:
                            flags.append(
                                f"Operating cost ratio {_pct(op_cost_ratio)} ({_fy(p)}) "
                                f"above {_pct(RED_FLAG_THRESHOLDS['op_cost_ratio_high'])} threshold — "
                                f"costs consuming most of revenue"
                            )
                        # Cost-ratio deterioration is flagged after the loop
                        # on the most recent YoY transition only -- see the
                        # margin-change block below.
                else:
                    gross_margin[p] = "N/A (costs missing)"

                if rev and oi:
                    op_margin[p] = _pct(_safe_div(oi, rev))
                else:
                    op_margin[p] = "N/A"

                net_margin[p] = _pct(_safe_div(ni, rev)) if rev else "N/A"

            # ── Sector: Utilities ────────────────────────────────────────
            elif sg == "utilities":
                # Regulated utilities file no COGS line — the "general" branch's
                # Revenue-COGS derivation lands on whatever partial cost tag
                # happens to resolve (often just fuel, sometimes nothing),
                # producing a meaningless (and usually wildly overstated)
                # margin. Consensus providers use a different convention
                # (Revenue - fuel & purchased power) that XBRL doesn't
                # reliably expose across filers (confirmed: DUK tags none of
                # the fuel/purchased-power concepts AEE/SO do). Flag as a
                # genuine definitional mismatch rather than guess.
                gross_margin_label = "Gross Margin"
                gross_margin[p] = "DEFINITION_MISMATCH (regulated utility — no COGS line)"
                op_margin[p]  = _pct(_safe_div(oi, rev)) if rev else "N/A"
                net_margin[p] = _pct(_safe_div(ni, rev)) if rev else "N/A"

            # ── Sector: Real Estate (REITs) ──────────────────────────────
            elif sg == "real_estate":
                # REITs have no traditional COGS; consensus providers report
                # NOI margin (Revenue - property operating expenses), which
                # is not a comparable "gross margin" and not reliably
                # derivable from XBRL (property opex tagging is inconsistent
                # across filers). Flag rather than show a Revenue-COGS
                # artifact (e.g. negative or >100% margins seen pre-fix).
                gross_margin_label = "Gross Margin"
                gross_margin[p] = "DEFINITION_MISMATCH (REIT — no comparable COGS/NOI basis)"
                op_margin[p]  = _pct(_safe_div(oi, rev)) if rev else "N/A"
                net_margin[p] = _pct(_safe_div(ni, rev)) if rev else "N/A"

            # ── Sector: Managed care / health insurers ───────────────────
            elif sg == "managed_care":
                # Medical claims costs (PolicyBenefitsAndClaims) are the
                # dominant cost of service for a health insurer and belong
                # in the cost base — consensus gross margin nets them out
                # alongside any separately-tagged product/pharmacy COGS.
                # Left out (as in the general branch), gross margin is based
                # only on the minor product-cost tag and overstates true
                # margin by 40-75pp (e.g. UNH 88.7% vs. consensus 19.7%).
                benefits = inc.insurance_benefits[i]
                combined_cogs = (cogs or 0) + (benefits or 0)
                if rev and combined_cogs:
                    gm_val = _safe_div(rev - combined_cogs, rev)
                    if isinstance(gm_val, float) and gm_val > 1.0:
                        gm_val = "N/A (tag overflow)"
                    gross_margin[p] = _pct(gm_val) if isinstance(gm_val, float) else (gm_val or "N/A")
                    # Compression flagged after the loop on the most recent
                    # YoY transition only -- see the margin-change block below.
                else:
                    gross_margin[p] = "N/A (medical costs/COGS missing)"
                op_margin[p]  = _pct(_safe_div(oi, rev)) if rev else "N/A"
                net_margin[p] = _pct(_safe_div(ni, rev)) if rev else "N/A"

            # ── Sector: Freight brokers (non-asset-based logistics) ──────
            elif sg == "freight_broker":
                # Freight brokers don't tag "purchased transportation" (their
                # dominant cost) as a standard us-gaap concept, so a true net-
                # revenue COGS figure isn't derivable. Economically there is
                # little daylight between gross and operating margin for a
                # pure broker — almost all cost sits above operating income —
                # so total operating costs (CostsSubtotal) is used as the
                # best available proxy, same mechanics as the Energy branch.
                gross_margin_label = "Net Revenue Margin (≈ Operating Margin)"
                total_costs = inc.total_operating_costs[i]
                if total_costs and rev and rev != 0:
                    proxy_margin = _safe_div(total_costs, rev)
                    if isinstance(proxy_margin, float):
                        gross_margin[p] = _pct(1 - proxy_margin) if proxy_margin <= 1 else "N/A (tag overflow)"
                    else:
                        gross_margin[p] = "N/A"
                else:
                    gross_margin[p] = "N/A (costs missing)"
                op_margin[p]  = _pct(_safe_div(oi, rev)) if rev else "N/A"
                net_margin[p] = _pct(_safe_div(ni, rev)) if rev else "N/A"

            # ── Sector: General (default) ─────────────────────────────────
            else:
                # Gross profit fallback: revenue - cogs
                if not gp and cogs:
                    gp = rev - cogs if rev else 0

                if rev:
                    # Zero-GP guard: gp/cogs resolving to falsy is indistinguishable
                    # from "genuinely zero" in this data layer (_get_vector returns
                    # 0.0 for both an unresolved tag and a real zero) -- a filer
                    # reporting a literal zero gross profit is effectively never the
                    # correct read, so treat any falsy gp as "no COGS line filed"
                    # (common for REITs, some utilities, and filers where COGS
                    # resolved to an implausible tag and was suppressed upstream --
                    # e.g. DLR, MAA, DE) rather than show a misleading 0.00%.
                    if not gp:
                        gm_val = 'N/A (not reported separately)'
                    else:
                        gm_val = _safe_div(gp, rev)
                    # Collapse guard: if gross margin == operating margin (within float
                    # epsilon), the GrossProfit tag resolved to the same value as
                    # OperatingIncomeLoss — tag collision or no separate gross line filed
                    # (common in aerospace/defense, franchisors). Show N/A rather than
                    # repeating operating margin under a different label.
                    if isinstance(gm_val, float) and oi and rev and round(gm_val, 4) == round(oi / rev, 4):
                        gm_val = "N/A (not reported separately)"
                    # Overflow guard: gross margin > 100% is impossible — the GrossProfit
                    # tag resolved to a line larger than revenue (e.g. DIS segment subtotals,
                    # content licensing credits that appear as negative COGS).
                    elif isinstance(gm_val, float) and gm_val > 1.0:
                        gm_val = "N/A (tag overflow)"
                    gross_margin[p] = _pct(gm_val) if isinstance(gm_val, float) else (gm_val or "N/A (COGS missing)")
                    op_margin[p]    = _pct(_safe_div(oi, rev))
                    net_margin[p]   = _pct(_safe_div(ni, rev))

                    # Compression flagged after the loop on the most recent
                    # YoY transition only -- see the margin-change block below.

                    if isinstance(net_margin.get(p), str) and net_margin[p].startswith("-"):
                        flags.append(
                            f"Net margin {net_margin[p]} ({_fy(p)}) below 0% breakeven — "
                            f"company reporting a net loss"
                        )
                else:
                    gross_margin[p] = op_margin[p] = net_margin[p] = "N/A"

            # ── Common metrics (all sectors) ──────────────────────────────

            # ROE — use average equity (beginning + ending / 2) per CFA convention.
            # Periods are newest-first, so the prior period is index i+1.
            # For the oldest period (last index), no prior is available; fall back
            # to period-end equity which still produces a useful estimate.
            eq_prior = bal.equity[i + 1] if (i + 1) < len(periods) else None
            if eq and eq > 0 and eq_prior and eq_prior > 0:
                avg_eq = (eq + eq_prior) / 2.0
                roe[p] = _pct(_safe_div(ni, avg_eq))
            elif eq and eq > 0:
                roe[p] = _pct(_safe_div(ni, eq))
            elif eq is not None and eq < 0:
                roe[p] = "N/A (neg. equity)"   # genuine negative equity
            else:
                # eq is None or 0 — likely a parse failure, not genuinely zero
                # Flag as parse error when total assets are substantial
                roe[p] = ("N/A (balance sheet parse error — verify XBRL)"
                          if ta and ta > 1e9 else "N/A (neg. equity)")

            # ROA
            roa[p] = _pct(_safe_div(ni, ta)) if ta else "N/A"

            # ── yfinance last-resort fallback (most recent period only) ────
            # When the SEC-XBRL-based pipeline can't resolve a comparable
            # figure at all -- filer doesn't tag a GAAP gross-profit/COGS
            # line (telecoms, defense contractors, diversified
            # conglomerates), or the sector's own metric isn't directly
            # comparable (Utilities, REITs) -- fall back to yfinance rather
            # than leave the report with a bare N/A. Only fires when the
            # XBRL path produced nothing usable; never overrides a real
            # computed value. Every fallback value is labelled with its
            # source inline so it's never mistaken for a GAAP/XBRL figure.
            if i == 0 and ticker:
                # Gross margin fallback only applies where "Gross Margin" is
                # actually the intended concept. Financials (NIM) and Energy
                # (Operating Cost Ratio) show a deliberately different metric
                # under this field -- if THAT metric is unresolved, the fix
                # is to source NIM/cost-ratio data, not silently swap in an
                # unrelated yfinance "gross margin" under the same label.
                if sg not in ("financials", "energy") and isinstance(gross_margin.get(p), str) and (
                        gross_margin[p].startswith("N/A") or gross_margin[p].startswith("DEFINITION_MISMATCH")):
                    val, src = _yfinance_gross_margin_fallback(ticker)
                    if val is not None:
                        gross_margin[p] = f"{_pct(val)} ({src})"
                        flags.append(
                            f"Gross margin ({_fy(p)}) sourced from {src}, not SEC XBRL -- "
                            f"this filer doesn't tag a comparable GAAP gross-profit/COGS line."
                        )
                # A bare "0.00%" is treated the same as "N/A" below: this data
                # layer uses 0.0 as the sentinel for both an unresolved XBRL
                # tag and a genuine zero (see the gross-margin zero-GP guard
                # above), and a company reporting an exact literal zero net
                # margin/ROE/ROA is effectively never the correct read -- e.g.
                # AEE's NetIncome tag doesn't resolve here, silently producing
                # "0.00%" net margin/ROE/ROA instead of the unresolved-data
                # signal a fallback needs to trigger on.
                def _unresolved(v):
                    return isinstance(v, str) and (v.startswith("N/A") or v == "0.00%")

                # Operating margin is deliberately suppressed (not merely
                # unresolved) for financials -- see "N/A (financials)" above
                # -- so it's excluded here too; Energy computes it normally.
                if sg != "financials" and _unresolved(op_margin.get(p)):
                    val, src = _yfinance_line_item_fallback(ticker, "operatingMargins")
                    if val is not None:
                        op_margin[p] = f"{_pct(val)} ({src})"
                if _unresolved(net_margin.get(p)):
                    val, src = _yfinance_line_item_fallback(ticker, "profitMargins")
                    if val is not None:
                        net_margin[p] = f"{_pct(val)} ({src})"
                if _unresolved(roe.get(p)):
                    val, src = _yfinance_line_item_fallback(ticker, "returnOnEquity")
                    if val is not None:
                        roe[p] = f"{_pct(val)} ({src})"
                if _unresolved(roa.get(p)):
                    val, src = _yfinance_line_item_fallback(ticker, "returnOnAssets")
                    if val is not None:
                        roa[p] = f"{_pct(val)} ({src})"

            # ROIC proxy (suppress for financials — invested capital concept invalid)
            if sg == "financials":
                roic[p] = "N/A (financials)"
            else:
                try:
                    if oi and ta:
                        nopat = oi * 0.79
                        invested_capital = (eq + debt) if (eq and eq > 0 and debt) else ta
                        roic[p] = _pct(_safe_div(nopat, invested_capital)) if invested_capital else "N/A"
                    else:
                        roic[p] = "N/A"
                except Exception:
                    roic[p] = "N/A"

            # Asset turnover
            if sg == "financials":
                fin_rev = inc.financial_revenue[i]
                at_rev = fin_rev if (fin_rev and fin_rev > 0) else rev
            else:
                at_rev = rev
            asset_turn[p] = _fmt_x(_safe_div(at_rev, ta)) if (at_rev and ta) else "N/A"

            # Inventory turnover (suppress for financials and energy — not meaningful)
            if sg in ("financials", "energy"):
                inv_turn[p] = "N/A (sector)"
            elif inv and cogs and inv > 0 and cogs > 0:
                inv_turn[p] = _fmt_x(_safe_div(cogs, inv))
            elif inv and inv <= 0:
                # Negative net inventory: customer advance payments exceed gross inventory
                # (common in long-cycle aerospace/defense — e.g. Boeing progress billings)
                inv_turn[p] = "N/A (net inventory negative — advance pmts)"
            elif cogs and cogs <= 0:
                # Negative COGS: contract loss provisions or write-downs exceed normal costs
                inv_turn[p] = "N/A (negative COGS — loss provisions)"
            else:
                inv_turn[p] = "N/A"

            # DSI (days sales of inventory) — general only
            if sg == "general":
                if inv and cogs and inv > 0 and cogs > 0:
                    dsi[p] = f"{(inv / cogs * 365):.1f} days"
                elif inv and inv <= 0:
                    dsi[p] = "N/A (net inventory negative — advance pmts)"
                elif cogs and cogs <= 0:
                    dsi[p] = "N/A (negative COGS — loss provisions)"
                else:
                    dsi[p] = "N/A"
            else:
                dsi[p] = "N/A (sector)"

            # EBITDA Margin — general + energy (not meaningful for financials)
            if sg == "financials":
                ebitda_margin[p] = "N/A (financials)"
            else:
                da = profile.cash_flow.depreciation_amortization[i]
                # CF D&A is zero for some filers (e.g. MSFT) that don't tag it
                # as a separate add-back line. Fall back to IS depreciation.
                if not da:
                    da = inc.depreciation_amortization[i]
                # Balance sheet delta fallback for filers where D&A isn't tagged
                # in either the CF or IS (e.g. MSFT embeds accumulated depreciation
                # in the PP&E net label text rather than as a separate element).
                # PP&E depreciation ≈ change in accumulated depreciation
                # Intangible amortization ≈ decline in net intangible assets
                if not da and i + 1 < len(periods):
                    accum_curr = bal.accumulated_depreciation[i]
                    accum_prev = bal.accumulated_depreciation[i + 1]
                    int_curr   = bal.intangible_assets[i]
                    int_prev   = bal.intangible_assets[i + 1]
                    ppe_dep    = max(0.0, accum_curr - accum_prev) if (accum_curr and accum_prev) else 0.0
                    int_amort  = max(0.0, int_prev - int_curr)     if (int_curr  and int_prev)  else 0.0
                    da = ppe_dep + int_amort
                # If D&A is still zero after all fallbacks, EBITDA cannot be
                # computed — show N/A rather than silently equating it to
                # operating margin (which would be misleading for asset-heavy
                # companies like TSLA with significant depreciation not tagged
                # in any XBRL statement).
                if oi and da:
                    ebitda_margin[p] = _pct(_safe_div(oi + abs(da), rev)) if rev else "N/A"
                elif oi and rev:
                    ebitda_margin[p] = "N/A (D&A unresolvable)"
                else:
                    ebitda_margin[p] = "N/A"

            # FCF Margin — general + energy (not meaningful for financials)
            if sg == "financials":
                fcf_margin[p] = "N/A (financials)"
            else:
                fcf = profile.cash_flow.free_cash_flow[i]
                fcf_margin[p] = _pct(_safe_div(fcf, rev)) if rev else "N/A"

            # Efficiency Ratio = NonInterestExpense / financial_revenue — financials only
            if sg == "financials":
                nie = inc.noninterest_expense[i]
                fin_rev = inc.financial_revenue[i]
                denom = fin_rev if (fin_rev and fin_rev > 0) else rev
                if nie and nie != 0 and denom and denom > 0:
                    eff = _safe_div(abs(nie), denom)
                    efficiency_ratio[p] = _pct(eff)
                    if isinstance(eff, float) and eff > RED_FLAG_THRESHOLDS["efficiency_ratio_high"]:
                        peer_lo = RED_FLAG_THRESHOLDS.get("efficiency_ratio_peer_low",  0.50)
                        peer_hi = RED_FLAG_THRESHOLDS.get("efficiency_ratio_peer_high", 0.65)
                        flags.append(
                            f"Efficiency ratio {_pct(eff)} ({_fy(p)}) above "
                            f"{_pct(RED_FLAG_THRESHOLDS['efficiency_ratio_high'])} threshold — "
                            f"high cost-to-income ratio (peer range {_pct(peer_lo)}–{_pct(peer_hi)})"
                        )
                else:
                    efficiency_ratio[p] = "N/A (NonInterestExpense tag missing)"
            else:
                efficiency_ratio[p] = "N/A (sector)"

            # ROTCE = Net Income / Tangible Common Equity — financials only
            if sg == "financials":
                gi = bal.goodwill_and_intangibles[i]
                pref = bal.preferred_stock[i]
                common_eq = (eq - pref) if (eq and pref) else eq
                # Guard: some large banks (e.g. JPM) do not tag goodwill as a
                # separate XBRL line — it is embedded in "Other assets".
                # When gi == 0, TCE == common equity and ROTCE == ROE, which
                # is misleading.  Surface the data gap rather than show a
                # silently wrong number.
                if not gi or gi == 0:
                    rotce[p] = "N/A (goodwill untagged)"
                else:
                    tce = (common_eq - gi) if (common_eq and gi is not None) else common_eq
                    if tce and tce > 0 and ni:
                        rotce[p] = _pct(_safe_div(ni, tce))
                    else:
                        rotce[p] = "N/A"
            else:
                rotce[p] = "N/A (sector)"

            # ── SG&A and R&D as % of revenue (all sectors) ──────────────
            sga = inc.sga_expense[i]
            rd  = inc.rd_expense[i]
            sga_pct[p] = _pct(_safe_div(abs(sga), rev)) if (sga and rev) else "N/A"
            rd_pct[p]  = _pct(_safe_div(abs(rd),  rev)) if (rd  and rev) else "N/A"

            # ── Effective tax rate ────────────────────────────────────────
            pretax = inc.pretax_income[i]
            tax    = inc.income_tax[i]
            if pretax and pretax != 0 and tax is not None:
                etr = _safe_div(abs(tax), abs(pretax))
                effective_tax[p] = _pct(etr) if isinstance(etr, float) else "N/A"
            else:
                effective_tax[p] = "N/A"

            # ── DSO / DPO / CCC (general + energy; suppress for financials) ─
            if sg == "financials":
                dso[p] = dpo[p] = ccc[p] = "N/A (financials)"
            else:
                ar  = bal.accounts_receivable[i]
                ap  = bal.accounts_payable[i]
                # DSO = AR / Revenue * 365
                if ar and rev and ar > 0:
                    dso[p] = f"{(ar / rev * 365):.1f} days"
                else:
                    dso[p] = "N/A"
                # DPO = AP / COGS * 365
                if ap and cogs and ap > 0 and cogs > 0:
                    dpo[p] = f"{(ap / cogs * 365):.1f} days"
                else:
                    dpo[p] = "N/A"
                # CCC = DSO + DSI - DPO (only when all three resolve)
                dso_v = ar / rev * 365       if (ar and rev and ar > 0) else None
                dsi_v = inv / cogs * 365     if (inv and cogs and inv > 0 and cogs > 0) else None
                dpo_v = ap / cogs * 365      if (ap and cogs and ap > 0 and cogs > 0) else None
                if dso_v is not None and dsi_v is not None and dpo_v is not None:
                    ccc[p] = f"{(dso_v + dsi_v - dpo_v):.1f} days"
                else:
                    ccc[p] = "N/A"

            # ── Net debt ─────────────────────────────────────────────────
            cash_v = bal.cash[i]
            if debt is not None and cash_v is not None:
                nd = debt - cash_v
                net_debt[p] = f"${nd / 1e9:.2f}B" if abs(nd) >= 1e9 else f"${nd / 1e6:.0f}M"
            else:
                net_debt[p] = "N/A"

            # ── Net debt / EBITDA ─────────────────────────────────────────
            # Computed after ebitda_margin is set; use raw EBITDA value
            if sg == "financials":
                net_debt_ebitda[p] = "N/A (financials)"
                ebitda_coverage[p] = "N/A (financials)"
            else:
                da_v = profile.cash_flow.depreciation_amortization[i]
                if not da_v:
                    da_v = inc.depreciation_amortization[i]
                ebitda_v = (oi + abs(da_v)) if (oi and da_v) else None
                cash_v2  = bal.cash[i]
                nd_v     = (debt - cash_v2) if (debt is not None and cash_v2 is not None) else None
                if nd_v is not None and ebitda_v and ebitda_v > 0:
                    net_debt_ebitda[p] = _fmt_x(_safe_div(nd_v, ebitda_v))
                else:
                    net_debt_ebitda[p] = "N/A"
                # EBITDA interest coverage = EBITDA / interest expense
                ie_v = inc.interest_expense[i]
                if not ie_v:
                    ie_v = profile.cash_flow.interest_paid[i]
                if ebitda_v and ie_v and abs(ie_v) > 0:
                    ebitda_coverage[p] = _fmt_x(_safe_div(ebitda_v, abs(ie_v)))
                else:
                    # Same guard as interest_cov: flag data gap when debt is material
                    _debt_mat_ebitda = (
                        debt and ta and debt > 0 and ta > 0
                        and (debt / ta) > 0.10
                    )
                    if _debt_mat_ebitda and ebitda_v:
                        ebitda_coverage[p] = "[DATA ERROR — int. exp. tag missing; verify]"
                    else:
                        ebitda_coverage[p] = "N/A"

            # ── ROCE = EBIT / Capital Employed ────────────────────────────
            # Capital Employed = total assets - current liabilities
            if sg == "financials":
                roce[p] = "N/A (financials)"
            else:
                cl = bal.current_liabilities[i]
                if oi and ta and cl is not None:
                    cap_emp = ta - cl
                    roce[p] = _pct(_safe_div(oi, cap_emp)) if cap_emp and cap_emp > 0 else "N/A"
                else:
                    roce[p] = "N/A"

            # ── Book value per share ──────────────────────────────────────
            shares = inc.diluted_shares[i]
            if eq and eq > 0 and shares and shares > 0:
                bvps[p] = f"${eq / shares:.2f}"
            else:
                bvps[p] = "N/A"

            # ── FFO / AFFO (REITs only, NAREIT definitions) ────────────────
            # FFO  = Net Income + Real Estate D&A - Gains on Sale of RE
            #        + Impairment of RE (add-back)
            # AFFO = FFO - Straight-line Rent Adj - Above/Below-Market Lease
            #        Amort - Maintenance CapEx + Non-cash Stock Comp
            #        + Non-cash Interest/Financing Cost Amort
            # Routed on sg (sector_group), consistent with the CFO/EV
            # suppression in ValuationAgent, rather than a raw sector-string
            # match. Only computed for Real Estate; left blank for all others.
            if sg == "real_estate":
                re_da      = profile.cash_flow.real_estate_da[i]
                gain_sale  = profile.cash_flow.gain_on_sale_real_estate[i]
                impairment = profile.cash_flow.impairment_real_estate[i]

                if ni and re_da:
                    ffo = ni + abs(re_da) - (gain_sale if gain_sale and gain_sale > 0 else 0) \
                          + (abs(impairment) if impairment else 0)

                    # UPREIT structures (e.g. SPG): consolidated NI already
                    # nets out the OP unitholders' (noncontrolling interest)
                    # share, but NAREIT defines FFO as "FFO attributable to
                    # common shareholders AND OP unitholders" -- they're
                    # economically equivalent for FFO purposes, so add the
                    # NCI share back. Omitting this understated SPG's FFO by
                    # ~its full NCI share vs. investor-relations-disclosed FFO.
                    nci = _safe_val(inc.minority_interest_expense, i)
                    if nci:
                        ffo += abs(nci)
                        print(f"[{ticker}] REIT FFO ({p}): NCI addback = {nci/1e6:,.0f}M")

                    ffo_rows[p] = ffo
                    ffo_margin[p] = _pct(_safe_div(ffo, rev)) if rev else "N/A"
                    ffo_components[p] = {
                        "ni": ni, "re_da": re_da,
                        "gain_sale": gain_sale, "impairment": impairment,
                        "nci_addback": abs(nci) if nci else 0,
                    }

                    sl_rent      = profile.cash_flow.straight_line_rent_adj[i]
                    lease_amort  = profile.cash_flow.above_below_market_lease_amort[i]
                    maint_capex  = profile.cash_flow.maintenance_capex[i]
                    stock_comp   = profile.cash_flow.non_cash_stock_comp[i]
                    non_cash_int = profile.cash_flow.non_cash_interest[i]

                    affo = ffo
                    if sl_rent:
                        affo -= sl_rent
                    if lease_amort:
                        affo -= abs(lease_amort)
                    if maint_capex:
                        affo -= abs(maint_capex)
                    if stock_comp:
                        affo += abs(stock_comp)
                    if non_cash_int:
                        affo += abs(non_cash_int)

                    affo_rows[p] = affo
                    affo_margin[p] = _pct(_safe_div(affo, rev)) if rev else "N/A"
                    affo_components[p] = {
                        "sl_rent": sl_rent, "lease_amort": lease_amort,
                        "maint_capex": maint_capex,
                        "capex_is_total_proxy": profile.cash_flow.maintenance_capex_is_proxy,
                        "stock_comp": stock_comp, "non_cash_int": non_cash_int,
                    }
                    print(
                        f"[{ticker}] FFO ({p}): NI={ni:,.0f} + RE_DA={abs(re_da):,.0f} "
                        f"- Gains={gain_sale or 0:,.0f} + Impairment={abs(impairment) if impairment else 0:,.0f} "
                        f"= FFO={ffo:,.0f}  |  AFFO={affo:,.0f}"
                        f"{'  (maint capex = total capex proxy)' if affo_components[p]['capex_is_total_proxy'] else ''}"
                    )
                else:
                    ffo_rows[p] = None
                    ffo_margin[p] = "N/A"
                    affo_rows[p] = None
                    affo_margin[p] = "N/A"
            else:
                ffo_rows[p] = None
                ffo_margin[p] = "N/A (sector)"
                affo_rows[p] = None
                affo_margin[p] = "N/A (sector)"

        # ── Stock-based compensation ────────────────────────────────────────
        sbc_metrics = _compute_sbc_metrics(profile, periods)
        if sbc_metrics["available"] and periods:
            # Most recent period only, consistent with the recency rule
            # applied to the other margin flags.
            _sbc_pct = sbc_metrics["sbc_pct_revenue"].get(periods[0])
            if isinstance(_sbc_pct, (int, float)) and _sbc_pct > 10:
                flags.append(
                    f"SBC at {_sbc_pct:.1f}% of revenue ({_fy(periods[0])}) — "
                    f"material economic cost not reflected in GAAP FCF"
                )

        # ── Shareholder returns (dividends / buybacks / payout) ─────────────
        shareholder_returns = _compute_shareholder_returns(
            profile, periods, sg, affo_rows
        )
        if shareholder_returns["available"] and periods:
            _sr_mr   = periods[0]
            _basis   = shareholder_returns["payout_basis"]
            _pay_fcf = shareholder_returns["payout_ratio_fcf"].get(_sr_mr)
            _tot_fcf = shareholder_returns["total_payout_fcf"].get(_sr_mr)
            if isinstance(_pay_fcf, (int, float)) and _pay_fcf > 100:
                flags.append(
                    f"Dividends exceed {_basis} ({_pay_fcf:.0f}% of {_basis}, "
                    f"{_fy(_sr_mr)}) — distribution funded by balance sheet or debt"
                )
            if isinstance(_tot_fcf, (int, float)) and _tot_fcf > 100:
                flags.append(
                    f"Total shareholder returns exceed {_basis} "
                    f"({_tot_fcf:.0f}% of {_basis}, {_fy(_sr_mr)}) — "
                    f"funded by balance sheet or debt"
                )

        # ── Margin / cost-ratio change flag (most recent YoY only) ───────────
        # Replaces four per-sector in-loop versions that compared each period
        # against the PREVIOUS loop iteration. Because this loop runs
        # newest-first, that "previous" value was the NEWER period, so the
        # comparison ran backwards: an expansion was reported as a
        # compression, arrowed the wrong way, and labelled with the older
        # year. It also fired on every historical transition, surfacing
        # multi-year-old moves as current findings.
        #
        # periods[0] is the current period and periods[1] the prior one, so
        # `prior -> current` is the only correct direction here.
        if len(periods) >= 2:
            _mr_gm, _pr_gm = periods[0], periods[1]
            _gm_cur = (_parse_pct(gross_margin.get(_mr_gm, ""))
                       if "%" in str(gross_margin.get(_mr_gm, "")) else None)
            _gm_prv = (_parse_pct(gross_margin.get(_pr_gm, ""))
                       if "%" in str(gross_margin.get(_pr_gm, "")) else None)
            _gm_thr = RED_FLAG_THRESHOLDS["gross_margin_drop_bps"]
            if _gm_cur is not None and _gm_prv is not None:
                if sg == "energy":
                    # Operating Cost Ratio: a RISE is the deterioration.
                    _rise_bps = (_gm_cur - _gm_prv) * 10000
                    if _rise_bps > _gm_thr:
                        flags.append(
                            f"Operating cost ratio rose {_rise_bps:.0f}bps: "
                            f"{_pct(_gm_prv)} → {_pct(_gm_cur)} "
                            f"({_fy(_pr_gm)}→{_fy(_mr_gm)}) — "
                            f"cost inflation outpacing revenue"
                        )
                else:
                    _drop_bps = (_gm_prv - _gm_cur) * 10000
                    if _drop_bps > _gm_thr:
                        _lbl = "NIM" if sg == "financials" else "Gross margin"
                        _why = ("interest margin deterioration"
                                if sg == "financials"
                                else "exceeds warning threshold")
                        flags.append(
                            f"{_lbl} compressed {_drop_bps:.0f}bps: "
                            f"{_pct(_gm_prv)} → {_pct(_gm_cur)} "
                            f"({_fy(_pr_gm)}→{_fy(_mr_gm)}) — {_why} "
                            f"({int(_gm_thr)}bps threshold)"
                        )

        # Revenue CAGR — use financial_revenue for financials sector
        rev_series = inc.financial_revenue if sg == "financials" else inc.revenue
        revenue_cagr = "N/A"
        if len(periods) >= 2:
            start_rev = rev_series[-1]
            end_rev   = rev_series[0]
            years     = len(periods) - 1
            c = _cagr(start_rev, end_rev, years)
            revenue_cagr = _pct(c) if c is not None else "N/A"
            if c is not None and c < RED_FLAG_THRESHOLDS["revenue_cagr_negative"]:
                flags.append(
                    f"Revenue {years}-yr CAGR {_pct(c)} "
                    f"({_fy(periods[-1])}→{_fy(periods[0])}) below 0% — top-line contraction"
                )

        # Operating leverage (suppress for financials)
        # When the computed ratio is extreme (> _OP_LEV_DISTORTION_THRESHOLD),
        # the metric is almost always driven by a non-recurring item distorting one
        # period's operating income (e.g. litigation reserve reversals, restructuring
        # charges, production shutdowns, spinoff accounting). In those cases:
        #   — keep in Section 1 with [!] marker so the number is visible
        #   — add to red flags with the full operating margin trajectory so the
        #     analyst can see what "normal" looks like vs. the distorted period
        _OP_LEV_DISTORTION_THRESHOLD = RED_FLAG_THRESHOLDS.get("op_leverage_distortion", 15.0)

        op_leverage = {}
        if sg != "financials" and len(periods) >= 2:
            for i in range(len(periods) - 1):
                curr_p, prev_p = periods[i], periods[i + 1]
                dr = _safe_div(inc.revenue[i] - inc.revenue[i + 1], inc.revenue[i + 1])
                do = _safe_div(
                    inc.operating_income[i] - inc.operating_income[i + 1],
                    abs(inc.operating_income[i + 1]) if inc.operating_income[i + 1] else None
                )
                if isinstance(dr, float) and isinstance(do, float) and dr != 0:
                    ratio = do / dr
                    if abs(ratio) > _OP_LEV_DISTORTION_THRESHOLD:
                        # Keep visible in Section 1 with [!] so it's not silently hidden
                        op_leverage[f"{prev_p}→{curr_p}"] = f"{ratio:.2f}x [!]"
                        # Add red flag with full margin trajectory as "normal" reference
                        om_context = "  |  ".join(
                            f"{_fy(p)} {op_margin.get(p, 'N/A')}"
                            for p in periods
                        )
                        flags.append(
                            f"Operating leverage non-representative: {ratio:.2f}x "
                            f"({_fy(prev_p)}→{_fy(curr_p)}) — GAAP op. margin swung "
                            f"{op_margin.get(prev_p, 'N/A')} → {op_margin.get(curr_p, 'N/A')} "
                            f"on {_pct(dr)} revenue growth. "
                            f"Full op. margin history: {om_context}. "
                            f"Verify against adjusted operating income."
                        )
                    else:
                        op_leverage[f"{prev_p}→{curr_p}"] = f"{ratio:.2f}x"
                else:
                    op_leverage[f"{prev_p}→{curr_p}"] = "N/A"

        # Basel III capital ratios (financials only)
        # Primary source: FR Y-9C Schedule HC-R via FFIEC bulk CSV (fr_y9c_data kwarg).
        # Fallback: XBRL tags on the balance sheet (rarely tagged by large BHCs).
        # Benchmarks (Basel III + US conservation buffer):
        #   CET1          >= 7.0%  (4.5% min + 2.5% buffer)
        #   Tier 1        >= 8.5%  (6.0% min + 2.5% buffer)
        #   Total Capital >= 10.5% (8.0% min + 2.5% buffer)
        #   Tier 1 Lev.   >= 4.0%  (US enhanced SLR for G-SIBs)
        basel = {}
        if sg == "financials":
            for i, p in enumerate(periods):
                # FR Y-9C data takes priority when provided by the orchestrator
                fry9c_row = (fr_y9c_data or {}).get(p, {})

                cet1_v   = fry9c_row.get("cet1")
                t1_v     = fry9c_row.get("tier1")
                totcap_v = fry9c_row.get("total_capital")
                lev_v    = fry9c_row.get("lev_ratio")

                # Fallback to XBRL balance sheet tags if FR Y-9C returned nothing
                if not any([cet1_v, t1_v, totcap_v, lev_v]):
                    def _xbrl_ratio(vec, idx):
                        v = vec[idx] if idx < len(vec) else 0.0
                        if not v:
                            return None
                        if abs(v) > 1.0:
                            v = v / 100.0
                        return v if 0.01 <= abs(v) <= 0.50 else None

                    cet1_v   = _xbrl_ratio(bal.cet1_ratio, i)
                    t1_v     = _xbrl_ratio(bal.tier1_capital_ratio, i)
                    totcap_v = _xbrl_ratio(bal.total_capital_ratio, i)
                    lev_v    = _xbrl_ratio(bal.tier1_leverage_ratio, i)

                row = {
                    "cet1":          _pct(cet1_v)   if cet1_v   else "N/A",
                    "tier1":         _pct(t1_v)     if t1_v     else "N/A",
                    "total_capital": _pct(totcap_v) if totcap_v else "N/A",
                    "lev_ratio":     _pct(lev_v)    if lev_v    else "N/A",
                }

                # Flag if CET1 below 7% well-capitalised threshold
                if isinstance(cet1_v, float) and cet1_v < 0.07:
                    flags.append(
                        f"CET1 ratio {_pct(cet1_v)} ({_fy(p)}) below 7.0% "
                        f"well-capitalised floor — regulatory capital under pressure"
                    )

                basel[p] = row

        # ── Ownership flags ───────────────────────────────────────────────────
        ownership = ownership or {}
        if ownership:
            inst_pct  = ownership.get("institutional_pct")
            top10     = ownership.get("top10_concentration_pct")
            delta_1yr = ownership.get("institutional_pct_delta_1yr")

            if inst_pct is not None and inst_pct < 0.20:
                flags.append(
                    f"■ Low institutional ownership ({inst_pct*100:.1f}%) — "
                    f"below 20% threshold; may indicate limited sell-side coverage "
                    f"or foreign/closely-held structure"
                )
            if top10 is not None and top10 > 0.40:
                flags.append(
                    f"■ Top-10 holder concentration {top10*100:.1f}% — "
                    f"above 40%; crowding risk if sentiment shifts"
                )
            if delta_1yr is not None and delta_1yr < -0.05:
                flags.append(
                    f"■ Institutional ownership declined {delta_1yr*100:.1f}pp over 1yr — "
                    f"institutions reducing exposure; monitor for continued distribution"
                )

        # ── Combined insider distribution + rising short interest ──────────────
        # Both insider_transactions.py and short_interest_loader.py document
        # this combination as a more meaningful signal than either alone, but
        # neither module fires it itself — it's meant to be wired up by the
        # calling agent. Thresholds: insider sell_count>=3 and net_value<0 is
        # the exact "broad insider distribution" rule suggested in
        # insider_transactions.py's own docstring. The short-interest "rising
        # sharply MoM" threshold (>10%) is not specified anywhere in the
        # codebase — this is a judgment call made here, not a documented rule;
        # revisit if it fires too often or too rarely in practice.
        insider_activity = insider_activity or {}
        short_interest    = short_interest or {}
        if insider_activity and short_interest:
            sell_count = insider_activity.get("sell_count")
            net_value  = insider_activity.get("net_value")
            pct_mom    = short_interest.get("pct_change_mom")

            insider_distribution = (
                sell_count is not None and sell_count >= 3
                and net_value is not None and net_value < 0
            )
            short_rising = pct_mom is not None and pct_mom > 0.10

            if insider_distribution and short_rising:
                flags.append(
                    f"■ Insider distribution + rising short interest — "
                    f"{sell_count} insiders sold (net -${abs(net_value):,.0f}) "
                    f"while short interest rose {pct_mom*100:+.1f}% MoM; "
                    f"combined signal stronger than either alone"
                )

        # ── FCF margin deterioration flag ────────────────────────────────────
        # Fires when FCF margin drops >5pp from its peak to the most recent period.
        # Uses peak-to-newest rather than oldest-to-newest: catches companies where
        # FCF was rising then reversed (e.g. NKE: 9.51% → 12.88% → 7.06%).
        if sg != "financials":
            fcf_vals = []
            for p in reversed(periods):   # oldest first
                v_str = fcf_margin.get(p, "")
                if "%" in str(v_str):
                    v = _parse_pct(v_str)
                    if v is not None:
                        fcf_vals.append((p, v))
            if len(fcf_vals) >= 2:
                newest_p, newest_v = fcf_vals[-1]
                peak_p,   peak_v   = max(fcf_vals[:-1], key=lambda x: x[1])
                drop_pp = (peak_v - newest_v) * 100   # positive = deterioration
                _fcf_thresh = 5.0
                if drop_pp > _fcf_thresh:
                    flags.append(
                        f"FCF margin deterioration: {_pct(peak_v)} → {_pct(newest_v)} "
                        f"({_fy(peak_p)}→{_fy(newest_p)}) — "
                        f"{drop_pp:.0f}pp decline from peak; cash generation weakening"
                    )

        # ── Operating margin deterioration flag ───────────────────────────────
        # Fires when op margin drops >4pp in the most recent year-over-year move.
        if sg not in ("financials",):
            om_mr = op_margin.get(periods[0], "") if periods else ""
            om_pr = op_margin.get(periods[1], "") if len(periods) > 1 else ""
            if "%" in str(om_mr) and "%" in str(om_pr):
                om_mr_f = _parse_pct(om_mr)
                om_pr_f = _parse_pct(om_pr)
                if om_mr_f is not None and om_pr_f is not None:
                    drop_pp = (om_pr_f - om_mr_f) * 100   # positive = deterioration
                    _om_thresh = 4.0
                    if drop_pp > _om_thresh:
                        flags.append(
                            f"Operating margin compressed {drop_pp:.0f}pp YoY "
                            f"({om_pr} → {om_mr}) — "
                            f"significant profitability deterioration"
                        )

        # ── TTM (trailing twelve months) column ──────────────────────────────
        # Flow-based margins only (Gross/Operating/EBITDA/Net/FCF) -- ROE,
        # ROA, turnover ratios, DSI, efficiency ratio, ROIC/ROTCE/ROCE, and
        # FFO/AFFO all mix a TTM flow numerator against a balance-sheet
        # denominator that would need a most-recent-quarter (MRQ) balance
        # sheet to do properly; this pipeline doesn't fetch quarterly BS
        # data (see facts_processor._compute_ttm_bundle docstring), so
        # those rows simply have no TTM entry rather than pairing a fresh
        # TTM numerator with a stale FY-end denominator. gross_margin is
        # also absent here for filers (e.g. WMT) that only resolve it via
        # the annual pipeline's Revenue-minus-COGS derivation fallback,
        # which the TTM path doesn't replicate.
        ttm = getattr(profile, "ttm", None)
        ttm_out = {"available": False}
        if ttm:
            ttm_out = {
                "available":           True,
                "gross_margin":        _pct(ttm.get("gross_margin"))
                                       if ttm.get("gross_margin") is not None else "N/A",
                "operating_margin":    _pct(ttm.get("operating_margin"))
                                       if ttm.get("operating_margin") is not None else "N/A",
                "ebitda_margin":       _pct(ttm.get("ebitda_margin"))
                                       if ttm.get("ebitda_margin") is not None else "N/A",
                "net_margin":          _pct(ttm.get("net_margin"))
                                       if ttm.get("net_margin") is not None else "N/A",
                "fcf_margin":          _pct(ttm.get("fcf_margin"))
                                       if ttm.get("fcf_margin") is not None else "N/A",
                "quarters_used":       ttm.get("quarters_used"),
                "is_partial":          ttm.get("is_partial"),
                "most_recent_quarter": ttm.get("most_recent_quarter"),
            }

        return {
            "periods":              periods,
            "ttm":                  ttm_out,
            "gross_margin":         gross_margin,
            "gross_margin_label":   gross_margin_label,
            "operating_margin":     op_margin,
            "net_margin":           net_margin,
            "roe":                  roe,
            "roa":                  roa,
            "roic_proxy":           roic,
            "revenue_cagr":         revenue_cagr,
            "asset_turnover":       asset_turn,
            "inventory_turnover":   inv_turn,
            "dsi":                  dsi,
            "ebitda_margin":        ebitda_margin,
            "fcf_margin":           fcf_margin,
            "efficiency_ratio":     efficiency_ratio,
            "rotce":                rotce,
            "operating_leverage":   op_leverage,
            "basel":                basel,
            "sga_pct":              sga_pct,
            "rd_pct":               rd_pct,
            "effective_tax":        effective_tax,
            "dso":                  dso,
            "dpo":                  dpo,
            "ccc":                  ccc,
            "net_debt":             net_debt,
            "net_debt_ebitda":      net_debt_ebitda,
            "ebitda_coverage":      ebitda_coverage,
            "roce":                 roce,
            "bvps":                 bvps,
            "ffo_margin":           ffo_margin,
            "ffo":                  ffo_rows,
            "affo":                 affo_rows,
            "affo_margin":          affo_margin,
            "ffo_components":       ffo_components,
            "affo_components":      affo_components,
            "ffo_available":        sg == "real_estate",
            "affo_uses_total_capex": any(
                v.get("capex_is_total_proxy") for v in affo_components.values()
            ) if affo_components else False,
            "working_capital":      working_capital,
            "shareholder_returns":  shareholder_returns,
            "sbc":                  sbc_metrics,
            "sector_group":         sg,
            "suppressed_metrics":   _metric_suppression["suppress"],
            "suppression_footnotes": _metric_suppression["footnotes"],
            "ownership":            ownership or {},
            "flags":                flags,
        }


# ─────────────────────────────────────────────
# 2. RiskAgent
# ─────────────────────────────────────────────

class RiskAgent:
    """
    Sector-aware liquidity, solvency, and credit risk metrics.

    Sector routing:
        general    — current ratio, quick ratio, D/E, interest coverage, Altman Z
        financials — D/E, Tier-1 proxy (equity/assets), suppress current ratio
                     and interest coverage (interest is cost of funds, not financing)
                     suppress Altman Z (not valid for banks)
        energy     — current ratio, quick ratio, D/E, interest coverage, Altman Z
                     (same as general — energy companies have conventional leverage)

    Altman Z-Score (public, non-financial companies):
        Z = 1.2*X1 + 1.4*X2 + 3.3*X3 + 0.6*X4 + 1.0*X5
    """

    def assess(self, profile, market_cap: float = 0.0,
               debt_data: dict = None, fred_data: dict = None) -> dict:
        inc = profile.income_statement
        bal = profile.balance_sheet
        periods = profile.periods
        ticker = getattr(profile, 'ticker', '')
        sg = _sector_group(profile.sector, ticker)
        flags = []

        current_ratio = {}
        quick_ratio   = {}
        de_ratio      = {}
        interest_cov  = {}
        debt_ebitda   = {}
        debt_capital  = {}
        altman_z      = {}
        de_values        = []
        de_period_values = {}   # period -> de float, for trend flag with values
        de_trend_note = ""

        for i, p in enumerate(periods):
            ca   = bal.current_assets[i]
            cl   = bal.current_liabilities[i]
            inv  = bal.inventory[i]
            ta   = bal.total_assets[i]
            eq   = bal.equity[i]
            debt = bal.total_debt[i]
            rev  = inc.revenue[i]
            oi   = inc.operating_income[i]
            ie   = inc.interest_expense[i]
            # CF fallback: some companies (e.g. NEE, regulated utilities) do not tag
            # interest expense as a separate IS line — it is buried inside a combined
            # NonoperatingIncomeExpense total.  The CF statement's supplemental
            # disclosure tags it separately under the same InterestExpense concept.
            if not ie:
                ie = profile.cash_flow.interest_paid[i]
            ni   = inc.net_income[i]
            dep  = profile.cash_flow.capital_expenditures[i]

            # ── Current ratio (suppress for financials) ───────────────────
            if sg == "financials":
                current_ratio[p] = "N/A (financials)"
                quick_ratio[p]   = "N/A (financials)"
            else:
                if cl:
                    cr = _safe_div(ca, cl)
                    current_ratio[p] = _fmt_x(cr)
                    if isinstance(cr, float) and cr < RED_FLAG_THRESHOLDS["current_ratio"]:
                        flags.append(
                            f"Current ratio {_fmt_x(cr)} ({_fy(p)}) below "
                            f"{RED_FLAG_THRESHOLDS['current_ratio']}x threshold — "
                            f"current liabilities exceed current assets"
                        )
                else:
                    current_ratio[p] = "N/A"
                quick_ratio[p] = _fmt_x(_safe_div(ca - inv, cl)) if cl else "N/A"

            # ── Interest coverage (suppress for financials) ───────────────
            if sg == "financials":
                # For banks, interest expense IS cost of funds — coverage is irrelevant
                interest_cov[p] = "N/A (financials)"
            else:
                if ie and abs(ie) > 0:
                    ic = _safe_div(oi, abs(ie))
                    interest_cov[p] = _fmt_x(ic)
                    if isinstance(ic, float) and ic < RED_FLAG_THRESHOLDS["interest_coverage"]:
                        if ic < 0:
                            flags.append(
                                f"Interest coverage {_fmt_x(ic)} ({_fy(p)}) — "
                                f"EBIT was negative; operating loss not covering interest expense"
                            )
                        else:
                            flags.append(
                                f"Interest coverage {_fmt_x(ic)} ({_fy(p)}) below "
                                f"{RED_FLAG_THRESHOLDS['interest_coverage']}x threshold — "
                                f"EBIT barely covers debt service"
                            )
                else:
                    # Cross-check guard: "No int. exp." is only a defensible
                    # statement for a company with no debt. Any debt
                    # outstanding means interest exists somewhere -- expensed,
                    # capitalized into an asset, or filed under a concept the
                    # waterfall didn't resolve -- so saying "no interest
                    # expense" three rows above a debt schedule showing
                    # billions outstanding is a contradiction, not a finding.
                    # Applies to every filer with that combination.
                    _debt_material = (
                        debt and ta and debt > 0 and ta > 0
                        and (debt / ta) > 0.10
                    )
                    if debt and debt > 0:
                        interest_cov[p] = ("N/A (debt outstanding — interest may be "
                                           "capitalized or tag unresolved)")
                        if i == 0:
                            print(
                                f"[{ticker}] DATA FLAG: interest expense = 0 but "
                                f"total debt = ${debt/1e6:,.0f}M — check capitalized "
                                f"interest or missing tag"
                            )
                            # Materially-levered filers additionally get a red
                            # flag: at this debt load an unresolved interest
                            # line is a reporting gap worth chasing, not just
                            # a display caveat.
                            if _debt_material:
                                flags.append(
                                    f"Interest coverage data error ({_fy(p)}) — "
                                    f"interest expense tag did not resolve in XBRL but "
                                    f"debt/assets = {debt/ta*100:.0f}%. "
                                    f"Check IS InterestExpense tag or CF interest_paid fallback."
                                )
                    else:
                        interest_cov[p] = "No int. exp."

            # ── D/E (all sectors) ─────────────────────────────────────────
            # Equity floor: skip if equity < 1% of assets (UNH par-value issue)
            _eq_floor = (ta * 0.01) if ta else 1e8
            if eq and eq > 0 and eq >= _eq_floor:
                de = _safe_div(debt, eq)
                de_ratio[p] = _fmt_x(de)
                de_values.append(de if isinstance(de, float) else None)
                if isinstance(de, float):
                    de_period_values[p] = de
                if isinstance(de, float) and de > RED_FLAG_THRESHOLDS["de_ratio"]:
                    # Only flag the most recent period (i == 0). Prior-period
                    # breaches are covered by the trend flag — firing once per
                    # year produces duplicate bullets (e.g. WEN FY24 + FY25 + trend).
                    if i == 0:
                        flags.append(
                            f"D/E ratio {_fmt_x(de)} ({_fy(p)}) above "
                            f"{RED_FLAG_THRESHOLDS['de_ratio']}x threshold — "
                            f"elevated financial leverage"
                        )
            else:
                de_ratio[p] = "N/A (neg. equity)"

            # ── yfinance last-resort fallback (most recent period only) ────
            # Same rationale as FundamentalAgent's fallback: only fires when
            # the SEC-XBRL path produced nothing usable, never overrides a
            # real computed value, and always labels its source inline.
            if i == 0 and ticker:
                if sg != "financials" and isinstance(current_ratio.get(p), str) and current_ratio[p].startswith("N/A"):
                    val, src = _yfinance_ratio_fallback(ticker, "currentRatio")
                    if val is not None:
                        current_ratio[p] = f"{_fmt_x(val)} ({src})"
                # A bare "0.00x" is treated as unresolved only when there's
                # corroborating evidence (material interest expense against
                # debt under 0.5% of total assets -- same signal the
                # interest-coverage "DATA ERROR" guard above already checks
                # for, e.g. AMT resolves debt=0, T resolves debt=$190M
                # against ~$550B total assets, both clearly a tag mismatch
                # not real leverage). A genuinely debt-free company is a
                # real, if less common, case -- unlike net margin/ROE,
                # 0.00x isn't assumed unresolved on its own.
                _de_val = de_ratio.get(p)
                _debt_negligible = (not debt) or (ta and debt < ta * 0.005)
                _de_looks_unresolved = isinstance(_de_val, str) and (
                    _de_val.startswith("N/A")
                    or (_de_val == "0.00x" and ie and abs(ie) > 0 and _debt_negligible)
                )
                if _de_looks_unresolved:
                    val, src = _yfinance_ratio_fallback(ticker, "debtToEquity", divide_by=100.0)
                    if val is not None:
                        de_ratio[p] = f"{_fmt_x(val)} ({src})"

            # ── Debt/Capital (all sectors; primary for energy) ────────────
            if eq and eq > 0 and debt:
                total_cap = debt + eq
                debt_capital[p] = _pct(_safe_div(debt, total_cap))
            elif eq and eq > 0:
                debt_capital[p] = "0.00%"   # no debt
            else:
                debt_capital[p] = "N/A (neg. equity)"
                de_values.append(None)

            # ── Debt/EBITDA (suppress for financials) ─────────────────────
            if sg == "financials":
                debt_ebitda[p] = "N/A (financials)"
            else:
                ebitda_proxy = oi + abs(dep) if oi and dep else oi
                if ebitda_proxy and ebitda_proxy > 0:
                    debt_ebitda[p] = _fmt_x(_safe_div(debt, ebitda_proxy))
                else:
                    debt_ebitda[p] = "N/A"

            # ── Altman Z (suppress for financials, energy, and utilities) ──────
            if i == 0:
                if sg == "financials":
                    altman_z[p] = "N/A (not applicable — financials)"
                elif sg == "energy":
                    # Calibrated on manufacturers; asset-heavy E&P companies
                    # structurally score low without being financially distressed
                    altman_z[p] = "N/A (not applicable — E&amp;P)"
                elif sg == "utilities":
                    # Regulated utilities have structurally low working capital and
                    # high leverage — the Altman Z model (calibrated on manufacturers)
                    # would show distress for any healthy utility
                    altman_z[p] = "N/A (regulated utility — model not applicable)"
                elif ta and eq:
                    tl = ta - eq
                    if tl and tl > 0:
                        wc = (ca - cl) if ca and cl else 0
                        re_proxy = bal.retained_earnings[i] if bal.retained_earnings[i] else eq
                        x1 = _safe_div(wc, ta, 0)
                        x2 = _safe_div(re_proxy, ta, 0)
                        x3 = _safe_div(oi, ta, 0)
                        x4 = _safe_div(market_cap, tl, 0) if market_cap else 0
                        x5 = _safe_div(rev, ta, 0)
                        z  = 1.2*x1 + 1.4*x2 + 3.3*x3 + 0.6*x4 + 1.0*x5
                        if isinstance(z, float):
                            zone = "Safe" if z > 2.99 else ("Grey zone" if z > 1.81 else "Distress zone")
                            altman_z[p] = f"{z:.2f} ({zone})"
                            if zone == "Distress zone":
                                flags.append(
                                    f"Altman Z-Score {z:.2f} ({_fy(p)}) in distress zone "
                                    f"(below 1.81) — elevated default risk"
                                )
                            elif zone == "Grey zone":
                                flags.append(
                                    f"Altman Z-Score {z:.2f} ({_fy(p)}) in grey zone "
                                    f"(1.81–2.99) — monitor for deterioration"
                                )
                        else:
                            altman_z[p] = "N/A"
                    else:
                        altman_z[p] = "N/A (data incomplete)"
                else:
                    altman_z[p] = "N/A (data incomplete)"

        # ── Interest coverage declining trend flag ───────────────────────────
        # Fires when: coverage declines across all available periods AND
        # the most recent value is below 4x. Mirrors D/E trend pattern.
        ic_values = []
        ic_period_values = {}
        for p in periods:
            ic_str = interest_cov.get(p, "")
            if "x" in str(ic_str):
                ic_f = _parse_x(ic_str)
                if ic_f is not None:
                    ic_values.append(ic_f)
                    ic_period_values[p] = ic_f

        if len(ic_values) >= 2:
            ic_newest_p = periods[0]
            ic_prior_p  = next((p for p in periods[1:] if p in ic_period_values), None)
            ic_oldest_p = next((p for p in reversed(periods) if p in ic_period_values), None)
            if ic_newest_p in ic_period_values and ic_oldest_p and ic_oldest_p != ic_newest_p:
                ic_newest = ic_period_values[ic_newest_p]
                ic_oldest = ic_period_values[ic_oldest_p]
                ic_prior  = ic_period_values[ic_prior_p] if ic_prior_p else None
                # Require BOTH: overall decline (newest < oldest) AND most recent
                # YoY move also declining (newest < prior). Without the second check,
                # a recovery year (e.g. OXY 3.79x → 3.83x) would still fire the flag
                # despite the most recent move being an improvement.
                ic_declining_overall = ic_newest < ic_oldest
                ic_declining_recent  = (ic_prior is None) or (ic_newest < ic_prior)
                ic_below_threshold   = ic_newest < 4.0
                if ic_declining_overall and ic_declining_recent and ic_below_threshold:
                    # Build trajectory string e.g. "5.14x → 4.92x → 3.90x"
                    ic_trajectory = " → ".join(
                        _fmt_x(ic_period_values[p])
                        for p in reversed(periods)
                        if p in ic_period_values
                    )
                    flags.append(
                        f"Interest coverage declining trend: {ic_trajectory} — "
                        f"deterioration approaching debt service risk "
                        f"(most recent {_fmt_x(ic_newest)}, below 4.0x threshold)"
                    )

        # D/E trend note
        valid_de = [v for v in de_values if v is not None]
        if len(valid_de) >= 2:
            valid_de_periods = [(p, de_period_values[p]) for p in periods if p in de_period_values]
            newest_p, newest_de = valid_de_periods[0]
            oldest_p, oldest_de = valid_de_periods[-1]
            # Round to 2dp before comparing — same precision the report displays.
            # Prevents false positives when the underlying float delta is
            # sub-rounding (e.g. 0.7801 vs 0.7799 both display as 0.78x).
            newest_r, oldest_r = round(newest_de, 2), round(oldest_de, 2)
            if newest_r > oldest_r:
                de_trend_note = "increasing YoY"
                flags.append(
                    f"D/E ratio increasing: {_fmt_x(newest_de)} ({_fy(newest_p)}) "
                    f"vs {_fmt_x(oldest_de)} ({_fy(oldest_p)}) — re-leveraging trend"
                )
            elif newest_r < oldest_r:
                de_trend_note = "decreasing YoY"
            else:
                de_trend_note = "stable"

        # ── Credit Quality ─────────────────────────────────────────────────────
        # Credit quality: rating from ratings.csv, schedule from DebtNoteFetcher,
        # market metrics from FRED.
        credit_quality = _compute_credit_quality(
            ticker=profile.ticker,
            debt_data=debt_data,
            fred_data=fred_data or {},
            sector_group=sg,
            profile=profile,
            market_cap=market_cap,
        )

        # ── Largest maturity concentration flag ─────────────────────────────
        _lm_pct_ev = credit_quality.get("largest_maturity_pct_ev")
        if isinstance(_lm_pct_ev, (int, float)):
            _lm_year   = credit_quality.get("largest_maturity_year")
            _lm_amount = credit_quality.get("largest_maturity_amount")
            _lm_amount_m = _lm_amount / 1e6 if _lm_amount else 0
            if _lm_pct_ev > 20:
                flags.append(
                    f"Largest debt maturity {_lm_year} = ${_lm_amount_m:,.0f}M "
                    f"({_lm_pct_ev:.1f}% of EV) — significant refinancing cliff; "
                    f"monitor credit market access"
                )
            elif _lm_pct_ev > 10:
                flags.append(
                    f"Largest debt maturity {_lm_year} = ${_lm_amount_m:,.0f}M "
                    f"({_lm_pct_ev:.1f}% of EV) — material refinancing "
                    f"concentration risk"
                )

        # ── Estimated interest expense (fallback only) ──────────────────────
        # When the filed interest-expense line didn't resolve but the debt
        # note gives a weighted-average effective rate, debt x rate is a
        # serviceable order-of-magnitude estimate. Clearly labelled as
        # derived, never substituted into interest coverage itself.
        est_interest_expense = None
        _mr_p = periods[0] if periods else None
        if _mr_p and str(interest_cov.get(_mr_p, "")).startswith("N/A (debt outstanding"):
            _wtd = _parse_pct(credit_quality.get("wtd_avg_rate", ""))
            _debt_mr = _safe_val(bal.total_debt, 0)
            if _wtd and _debt_mr and _wtd > 0:
                _est = _debt_mr * _wtd
                est_interest_expense = (
                    f"${_est/1e6:,.0f}M (from ${_debt_mr/1e6:,.0f}M debt x "
                    f"{_wtd*100:.2f}% wtd avg rate — not a filed figure)"
                )

        return {
            "periods":           periods,
            "current_ratio":     current_ratio,
            "quick_ratio":       quick_ratio,
            "interest_coverage": interest_cov,
            "est_interest_expense": est_interest_expense,
            "de_ratio":          de_ratio,
            "debt_capital":      debt_capital,
            "debt_ebitda":       debt_ebitda,
            "altman_z":          altman_z,
            "de_trend_note":     de_trend_note,
            "credit_quality":    credit_quality,
            "sector_group":      sg,
            "flags":             flags,
        }


# ─────────────────────────────────────────────
# Credit Quality helpers
# ─────────────────────────────────────────────

# Damodaran US implied ERP — updated annually (last: Jan 2025)
# Source: https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/implprem.html
# ERP constant — fallback only; get_erp() is called at runtime for live value
_DAMODARAN_ERP_US   = 0.0431   # Jun 2026 — Damodaran implied ERP (T12m)
_DAMODARAN_ERP_DATE = "Jun 2026 (hardcoded fallback)"

# Rating tier → FRED OAS series tier label
_RATING_TO_TIER: dict[str, str] = {
    "AAA": "AA",  "Aaa": "AA",   # AAA uses AA proxy (no separate FRED series)
    "AA+": "AA",  "AA":  "AA",  "AA-": "AA",
    "Aa1": "AA",  "Aa2": "AA",  "Aa3": "AA",
    "A+":  "A",   "A":   "A",   "A-":  "A",
    "A1":  "A",   "A2":  "A",   "A3":  "A",
    "BBB+":"BBB", "BBB": "BBB", "BBB-":"BBB",
    "Baa1":"BBB", "Baa2":"BBB", "Baa3":"BBB",
    "BB+": "HY (BB and below)", "BB":  "HY (BB and below)",
    "BB-": "HY (BB and below)", "B+":  "HY (BB and below)",
    "B":   "HY (BB and below)", "B-":  "HY (BB and below)",
    "Ba1": "HY (BB and below)", "Ba2": "HY (BB and below)",
    "Ba3": "HY (BB and below)", "B1":  "HY (BB and below)",
    "B2":  "HY (BB and below)", "B3":  "HY (BB and below)",
}


def _compute_is_cost_of_debt(profile, debt_data: dict | None) -> float | None:
    """
    Derive blended cost of debt: Kd = Interest Expense / Average Total Debt

    Uses XBRL-sourced interest expense (already in millions, from the
    interest_expense waterfall in xbrl_debt_fetcher) — avoids unit mismatch
    with the IS profile values which may be in different scales.

    Returns decimal (0.052 = 5.2%) or None if data insufficient.
    """
    if debt_data is None:
        return None

    try:
        # ── Interest expense: XBRL waterfall (millions) ───────────────────────
        ie_m = debt_data.get("interest_expense_m")

        # Fallback to profile IS if XBRL didn't find it
        if not ie_m and profile is not None:
            inc = profile.income_statement
            cf  = profile.cash_flow
            for src in [getattr(inc, "interest_expense", None),
                        getattr(cf,  "interest_paid",    None)]:
                if src is None:
                    continue
                try:
                    val = abs(float(list(src)[0]))
                    if val > 0:
                        # Convert to millions using revenue as scale reference
                        rev = list(getattr(inc, "revenue", [None]) or [None])[0]
                        if rev and abs(float(rev)) > 1e9:
                            ie_m = val / 1e6   # dollars → millions
                        elif rev and abs(float(rev)) > 1e6:
                            ie_m = val / 1e3   # thousands → millions
                        else:
                            ie_m = val         # already millions
                        break
                except Exception:
                    continue

        if not ie_m or ie_m <= 0:
            return None

        # ── Average total debt (millions) ─────────────────────────────────────
        total_m = debt_data.get("total_debt_m")
        if not total_m or total_m <= 0:
            return None

        # Use maturity sum as prior-year proxy if no BS prior year available
        kd = ie_m / total_m

        # Sanity check 0.1% – 20%
        if not (0.001 <= kd <= 0.20):
            logger.debug("_compute_is_cost_of_debt: kd=%.4f outside range "
                         "(ie_m=%.1fM total_m=%.1fM)", kd, ie_m, total_m)
            return None

        logger.info("_compute_is_cost_of_debt: Kd=%.2f%% "
                    "(ie=$%.0fM / avg_debt=$%.0fM)", kd*100, ie_m, total_m)
        return kd

    except Exception as e:
        logger.debug("_compute_is_cost_of_debt: error — %s", e)
        return None


def _compute_credit_quality(ticker: str,
                             debt_data: dict | None,
                             fred_data: dict,
                             sector_group: str,
                             profile=None,
                             market_cap: float = 0.0) -> dict:
    """
    Builds the credit quality dict for Section 2.

    Parameters
    ----------
    ticker      : used to look up rating from ratings.csv via config.get_rating()
    debt_data   : structured dict from DebtNoteFetcher.fetch_debt_note()
                  keys: tranches, maturities, total_debt_m, wtd_avg_rate,
                        nearest_maturity, source
    fred_data   : {risk_free: float|None, all_spreads: dict}
    sector_group: from _sector_group()
    profile     : CompanyFinancialProfile — used to derive IS-based cost of debt
    market_cap  : current market cap — used only for the largest-maturity-as-
                  %-of-EV metric below (current-price EV, same formula as
                  ValuationAgent's curr_ev: market_cap + debt + operating
                  lease liabilities + finance lease liabilities - cash).

    Returns dict with all credit quality fields. All values default to "N/A".
    """
    from core.config import get_rating
    _NA = "N/A"

    result = {
        "available":         False,
        # Rating
        "sp_rating":         _NA,
        "moodys_rating":     _NA,
        "fitch_rating":      _NA,
        "outlook":           _NA,
        "rating_as_of":      _NA,
        # Market metrics
        "risk_free":         _NA,
        "oas_spread":        _NA,
        "oas_tier":          _NA,
        "cost_of_debt":      _NA,
        # ERP: fetched live from Damodaran via erp_fetcher; fallback to hardcoded
        # (populated below after get_erp() call)
        "erp":               f"{_DAMODARAN_ERP_US*100:.2f}% ({_DAMODARAN_ERP_DATE})",
        "crp":               "0.00% (US-domiciled)",
        # Debt schedule
        "tranches":          [],
        "maturities":        {},
        "maturity_wall":     {},   # computed below — year: {amount_m, pct, bar}
        "wam_years":         None, # weighted average maturity in years
        "maturity_flags":    [],   # lumpiness warnings
        # Largest single-year maturity tranche vs. EV / total debt
        "largest_maturity_amount":   None,   # dollars (not millions)
        "largest_maturity_year":     None,
        "largest_maturity_pct_ev":   None,   # 0-100 scale
        "largest_maturity_pct_debt": None,   # 0-100 scale
        "total_debt_m":      _NA,
        "wtd_avg_rate":      _NA,
        "nearest_maturity":  _NA,
        "debt_source":       _NA,
    }

    # ── Step 0: Live ERP from Damodaran ──────────────────────────────────────
    try:
        from market.erp_fetcher import get_erp
        _live_erp, _live_erp_date = get_erp()
        result["erp"] = f"{_live_erp*100:.2f}% ({_live_erp_date})"
    except Exception:
        pass   # keep hardcoded fallback already set above

    # ── Step 1: Credit rating from ratings.csv ────────────────────────────────
    rating_info = get_rating(ticker)
    sp  = rating_info.get("sp_rating")
    mdy = rating_info.get("moodys_rating")
    fit = rating_info.get("fitch_rating")

    if sp or mdy or fit:
        result["sp_rating"]     = sp  or _NA
        result["moodys_rating"] = mdy or _NA
        result["fitch_rating"]  = fit or _NA
        result["outlook"]       = rating_info.get("outlook",    _NA)
        result["rating_as_of"]  = rating_info.get("as_of_date", _NA)
        result["available"]     = True

    # ── Step 2: FRED market data ──────────────────────────────────────────────
    rf          = fred_data.get("risk_free")
    all_spreads = fred_data.get("all_spreads", {})

    # Use S&P rating for OAS tier lookup; fall back to Moody's
    primary_rating = sp or mdy or fit
    oas_tier = _RATING_TO_TIER.get(primary_rating) if primary_rating else None
    oas      = all_spreads.get(oas_tier) if oas_tier else None

    if rf is not None:
        result["risk_free"] = f"{rf*100:.2f}% (10Y UST)"

    if oas is not None and oas_tier:
        result["oas_spread"] = f"{oas*100:.2f}%"
        result["oas_tier"]   = oas_tier

    if rf is not None and oas is not None:
        result["cost_of_debt"] = f"{(rf + oas)*100:.2f}%"
        result["available"]    = True

    # ── Step 3: Debt schedule from DebtNoteFetcher ────────────────────────────
    if debt_data:
        result["tranches"]   = debt_data.get("tranches", [])
        result["maturities"] = debt_data.get("maturities", {})
        result["debt_source"] = debt_data.get("source", _NA)
        result["available"]   = True

        total = debt_data.get("total_debt_m")
        if total is not None:
            result["total_debt_m"] = f"${total:,.0f}M"

        wtd = debt_data.get("wtd_avg_rate")
        if wtd is not None:
            result["wtd_avg_rate"] = f"{wtd*100:.2f}% (tranche avg)"

        nm = debt_data.get("nearest_maturity")
        if nm:
            result["nearest_maturity"] = nm

    # IS-derived cost of debt (fallback when no tranche rates available)
    if result.get("wtd_avg_rate", "N/A") == "N/A":
        is_rate = _compute_is_cost_of_debt(profile, debt_data)
        if is_rate is not None:
            flag = " ⚠ IE>20%" if is_rate > 0.20 else ""
            result["wtd_avg_rate"] = f"{is_rate*100:.2f}% (IS-derived){flag}"
            result["available"]    = True

    # ── Step 4: Maturity wall ─────────────────────────────────────────────────
    # Compute per-year % of total debt, WAM, and lumpiness flags.
    # Uses maturities dict {year_label: amount_m} already populated from XBRL.
    maturities = result.get("maturities", {})
    total_raw  = debt_data.get("total_debt_m") if debt_data else None

    if maturities and total_raw and total_raw > 0:
        # Sort chronologically — "thereafter" label sorts last naturally
        def _sort_key(yr):
            try:    return int(str(yr)[:4])
            except: return 9999

        mat_sorted  = sorted(maturities.items(), key=lambda x: _sort_key(x[0]))
        mat_total_m = sum(v for _, v in mat_sorted)
        # Use maturity sum as denominator when available (more complete than
        # carrying value which may exclude current portion)
        denom = max(total_raw, mat_total_m)

        wall        = {}
        filing_year = int(str(fred_data.get("as_of_date", "2025"))[:4]) \
                      if fred_data.get("as_of_date") else 2025
        wam_num     = 0.0
        wam_den     = 0.0

        for yr_label, amt_m in mat_sorted:
            pct = (amt_m / denom * 100) if denom > 0 else 0
            wall[yr_label] = {
                "amount_m": amt_m,
                "pct":      round(pct, 1),
            }
            # WAM offset from filing year
            offset = _sort_key(yr_label) - filing_year
            if 0 < offset <= 30:   # skip "thereafter" for WAM (offset=9999-year)
                wam_num += offset * amt_m
                wam_den += amt_m

        result["maturity_wall"] = wall
        result["wam_years"]     = round(wam_num / wam_den, 1) if wam_den > 0 else None

        # Lumpiness flags
        flags = []
        for yr_label, data in wall.items():
            if data["pct"] >= 25 and "thereafter" not in str(yr_label).lower():
                flags.append(f"{yr_label}: {data['pct']:.0f}% of debt maturing")
        # 3-year rolling concentration
        years = [k for k in wall if "thereafter" not in str(k).lower()]
        for i in range(len(years) - 2):
            window_pct = sum(wall[years[j]]["pct"] for j in range(i, i + 3))
            if window_pct >= 45:
                flags.append(
                    f"{years[i]}–{years[i+2]}: {window_pct:.0f}% of debt in 3-year window"
                )
                break   # report first cluster only
        result["maturity_flags"] = flags

        # ── Largest single-year maturity tranche vs. EV / total debt ──────
        # Excludes "thereafter" -- that bucket aggregates every year beyond
        # the disclosed schedule, so it isn't a genuine single-year
        # refinancing cliff (same exclusion the lumpiness flags above use).
        _single_year = {
            yr: data["amount_m"] for yr, data in wall.items()
            if "thereafter" not in str(yr).lower()
            and isinstance(data.get("amount_m"), (int, float)) and data["amount_m"] > 0
        }
        if _single_year:
            largest_m  = max(_single_year.values())
            largest_yr = next(yr for yr, amt in _single_year.items() if amt == largest_m)
            result["largest_maturity_amount"] = largest_m * 1e6   # millions -> dollars
            result["largest_maturity_year"]   = largest_yr

            if total_raw and total_raw > 0:
                result["largest_maturity_pct_debt"] = largest_m / total_raw * 100

            if profile is not None:
                try:
                    debt0      = _safe_val(profile.balance_sheet.total_debt, 0)
                    op_lease0  = _safe_val(profile.balance_sheet.operating_lease_liabilities, 0)
                    fin_lease0 = _safe_val(profile.balance_sheet.finance_lease_liabilities, 0)
                    cash0      = _safe_val(profile.balance_sheet.cash, 0)
                    ev_current = ((market_cap or 0) + (debt0 or 0)
                                  + (op_lease0 or 0) + (fin_lease0 or 0) - (cash0 or 0))
                    if ev_current and ev_current > 0:
                        result["largest_maturity_pct_ev"] = (largest_m * 1e6) / ev_current * 100
                except Exception:
                    pass

    return result
# 3. ValuationAgent
# ─────────────────────────────────────────────

class ValuationAgent:
    """
    P/E and P/B using historical FY-end price and current price.
    Sector routing: financials get P/TBV as primary metric (book value is
    most meaningful), energy gets EV/Sales as primary.
    """

    def value(self, profile, historical_prices: list = None,
              current_price: float = None,
              fred_data: dict = None,
              fundamental: dict = None) -> dict:
        inc = profile.income_statement
        bal = profile.balance_sheet
        cf  = profile.cash_flow
        periods = profile.periods
        sg = _sector_group(profile.sector, getattr(profile, 'ticker', ''))
        flags = []

        pe_hist  = {}
        pb_hist  = {}
        eps_rows  = {}   # diluted EPS per period -- the denominator of P/E
        bvps_rows = {}   # book value per share   -- the denominator of P/B
        pe_curr  = {}
        pb_curr  = {}
        ptbv     = {}
        ev_sales = {}
        ev_ebitda = {}
        ev_sales_current  = {}
        ev_ebitda_current = {}
        fy_mcap_rows = {}   # FY-end market cap (FY-end price x shares), reused
                            # by the shareholder-yield rows below rather than
                            # recomputed there
        fy_ev    = {}   # FY-end Enterprise Value per period, hoisted and
                        # shared by EV/Sales, EV/EBITDA, EV/FCF, EV/CFO,
                        # FCF/EV, CFO/EV below (was computed inline twice)
        op_lease_rows  = {}   # operating lease liabilities per period, for
                              # the output dict (transparency on EV components)
        fin_lease_rows = {}   # finance lease liabilities per period, ditto
        fcf_ev_rows    = {}
        cfo_ev_rows    = {}
        cfo_share_rows = {}
        ev_fcf_rows    = {}
        ev_cfo_rows    = {}
        shares_rows    = {}   # diluted share count actually used per period
        shares_source  = {}   # "yfinance" | "xbrl" per period
        ev_ffo_rows    = {}   # REITs only
        ffo_ev_rows    = {}   # REITs only
        ev_affo_rows   = {}   # REITs only
        affo_ev_rows   = {}   # REITs only

        market_cap = profile.market_cap
        fcf_arr = cf.free_cash_flow
        cfo_arr = cf.operating_cash_flow
        # TTM (trailing twelve months) -- see facts_processor._compute_ttm_bundle().
        # None when TTM couldn't be computed (e.g. too little quarterly history);
        # every _ttm.get(...) below degrades to the existing FY-basis figure in
        # that case, so a ticker without TTM support just renders as it always did.
        _ttm = getattr(profile, "ttm", None)
        _ffo_by_period  = (fundamental or {}).get("ffo", {}) or {}
        _affo_by_period = (fundamental or {}).get("affo", {}) or {}

        # yfinance is now the primary diluted-share source (pre-cleaned,
        # no unit-tagging ambiguity); XBRL is the fallback for any period
        # yfinance doesn't cover (it only carries ~4 fiscal years) or when
        # the ticker has no yfinance income-statement data at all.
        ticker = getattr(profile, 'ticker', '')
        _yf_shares_outstanding = _yfinance_info_cache(ticker).get('sharesOutstanding')

        # ── Resolve every period's share count up front ─────────────────────
        # Must run before any per-share or EV metric: the split detector
        # needs the whole series to spot a ratio break, and any period it
        # flags has its per-share/EV metrics suppressed rather than
        # computed against a split-adjusted price.
        shares_rows, shares_source = _resolve_diluted_shares(
            profile, periods, ticker, current_price, market_cap
        )
        # Classify each discontinuity: unit errors are repaired in place,
        # splits and unclassifiable breaks are suppressed.
        _anomaly      = _resolve_share_anomalies(shares_rows, periods,
                                                 shares_source, ticker)
        shares_rows   = _anomaly["shares"]
        contaminated  = _anomaly["contaminated"]
        split_info    = _split_boundary_info(shares_rows, periods) if contaminated else {}
        if contaminated:
            print(f"[{ticker}] split detected: contaminated periods = "
                  f"{sorted(contaminated)}")
        else:
            print(f"[{ticker}] split detection: no contamination "
                  f"(contaminated periods = [])")

        for i, p in enumerate(periods):
            rev    = inc.revenue[i]
            ni     = inc.net_income[i]
            oi     = inc.operating_income[i]

            shares    = shares_rows.get(p)
            eq        = bal.equity[i]
            pref      = bal.preferred_stock[i]
            debt      = bal.total_debt[i]
            op_lease  = bal.operating_lease_liabilities[i]
            fin_lease = bal.finance_lease_liabilities[i]
            op_lease_rows[p]  = op_lease  or None
            fin_lease_rows[p] = fin_lease or None
            ta        = bal.total_assets[i]
            gi        = bal.goodwill_and_intangibles[i]

            eps       = _safe_div(ni, shares) if shares else "N/A"
            common_eq = (eq - pref) if (eq and pref) else eq
            bvps      = _safe_div(common_eq, shares) if shares else "N/A"

            # Surfaced as their own rows: P/E and P/B below are just the
            # FY-end price divided by these, so showing the multiple without
            # its denominator hides the input the reader needs to sanity-check.
            eps_rows[p]  = eps  if isinstance(eps, float)  else None
            bvps_rows[p] = bvps if isinstance(bvps, float) else None

            # TBV = equity - goodwill - intangibles
            tbv  = (common_eq - gi) if (common_eq and gi is not None) else common_eq
            tbvps = _safe_div(tbv, shares) if (tbv and tbv > 0 and shares) else None

            hist_p = historical_prices[i] if (historical_prices and i < len(historical_prices)) else None

            # ── P/E ──────────────────────────────────────────────────────
            if hist_p and isinstance(eps, float) and eps > 0:
                pe_hist[p] = _fmt_x(hist_p / eps)
            elif hist_p and isinstance(eps, float) and eps <= 0:
                pe_hist[p] = "Neg. EPS"
            else:
                pe_hist[p] = f"EPS: ${eps:.2f}" if isinstance(eps, float) else "N/A"

            if i == 0:
                cp = current_price
                # P/E (TTM): current price / TTM EPS, falling back to
                # FY-basis EPS when TTM isn't available for this ticker.
                _ttm_ni = _ttm.get("NetIncome") if _ttm else None
                curr_eps = (_ttm_ni / shares) if (_ttm_ni is not None and shares) \
                          else (eps if isinstance(eps, float) else None)
                if cp and isinstance(curr_eps, float) and curr_eps > 0:
                    pe_curr[p] = _fmt_x(cp / curr_eps)
                elif cp and isinstance(curr_eps, float):
                    pe_curr[p] = "Neg. EPS"
                else:
                    pe_curr[p] = "N/A"
            else:
                pe_curr[p] = "—"

            # ── P/B ──────────────────────────────────────────────────────
            if hist_p and isinstance(bvps, float) and bvps > 0:
                pb_hist[p] = _fmt_x(hist_p / bvps)
            else:
                pb_hist[p] = f"BVPS: ${bvps:.2f}" if isinstance(bvps, float) else "N/A"

            if i == 0:
                cp = current_price
                if cp and isinstance(bvps, float) and bvps > 0:
                    pb_curr[p] = _fmt_x(cp / bvps)
                else:
                    pb_curr[p] = "N/A"
            else:
                pb_curr[p] = "—"

            # ── P/TBV ─────────────────────────────────────────────────────
            if hist_p and tbvps and tbvps > 0:
                ptbv[p] = _fmt_x(hist_p / tbvps)
            elif tbv is not None and tbv <= 0:
                ptbv[p] = "N/A (neg. TBV)"
            elif gi and gi > 0:
                ptbv[p] = "N/A (intangibles)"
            else:
                ptbv[p] = "N/A"

            cash_i = bal.cash[i]
            # EBITDA for this period -- used by both the current-basis (i==0
            # only, as before) and historical FY-end-basis EV/EBITDA below.
            da     = profile.cash_flow.depreciation_amortization[i]
            ebitda = (oi + abs(da)) if (oi and da) else (oi if oi else None)

            # ── FY-end Enterprise Value (hoisted) ───────────────────────────
            # Computed once per period and reused by EV/Sales, EV/EBITDA,
            # EV/FCF, EV/CFO, FCF/EV, CFO/EV below -- previously recomputed
            # inline, separately, in both the EV/Sales and EV/EBITDA blocks.
            if hist_p and shares and shares > 0:
                fy_market_cap = hist_p * shares
                fy_mcap_rows[p] = fy_market_cap
                fy_ev[p] = (fy_market_cap + (debt or 0)
                            + (op_lease or 0) + (fin_lease or 0) - (cash_i or 0))
            else:
                fy_mcap_rows[p] = None
                fy_ev[p] = None

            # ── EV/Sales ─────────────────────────────────────────────────
            # EV/Sales is suppressed for financials: revenue includes gross
            # interest income on a multi-trillion asset base, making the ratio
            # economically meaningless and not comparable to non-bank peers.
            #
            # Two bases are computed: "current" (today's market cap -- the
            # original approximation, market cap + debt + lease liabilities,
            # no cash netting -- the no-cash-netting asymmetry vs. fy_ev/
            # curr_ev below is pre-existing and out of scope here) and a
            # per-FY historical figure using fy_ev above (FY-end price x
            # shares for market cap, netting FY-end cash).
            if sg == "financials":
                ev_sales[p]         = "N/A (financials)"
                ev_sales_current[p] = "N/A (financials)" if i == 0 else "—"
            else:
                if i == 0:
                    # EV/Sales (TTM): falls back to FY revenue when TTM isn't available.
                    curr_rev = (_ttm.get("Revenue") if _ttm and _ttm.get("Revenue") is not None
                               else rev)
                    if market_cap and curr_rev:
                        ev_approx = market_cap + (debt or 0) + (op_lease or 0) + (fin_lease or 0)
                        ev_sales_current[p] = _fmt_x(_safe_div(ev_approx, curr_rev))
                    else:
                        ev_sales_current[p] = "N/A"
                else:
                    ev_sales_current[p] = "—"

                if fy_ev[p] and rev:
                    ev_sales[p] = _fmt_x(_safe_div(fy_ev[p], rev))
                else:
                    ev_sales[p] = "N/A"

            # ── EV/EBITDA (general + energy; suppress for financials) ────
            if sg == "financials":
                ev_ebitda[p]         = "N/A (financials)"
                ev_ebitda_current[p] = "N/A (financials)" if i == 0 else "—"
            else:
                if i == 0:
                    # EV/EBITDA (TTM): falls back to FY EBITDA when TTM isn't available.
                    curr_ebitda = (_ttm.get("EBITDA") if _ttm and _ttm.get("EBITDA") is not None
                                  else ebitda)
                    if market_cap and curr_ebitda and curr_ebitda > 0:
                        ev_approx = market_cap + (debt or 0) + (op_lease or 0) + (fin_lease or 0)
                        ev_ebitda_current[p] = _fmt_x(_safe_div(ev_approx, curr_ebitda))
                    else:
                        ev_ebitda_current[p] = "N/A"
                else:
                    ev_ebitda_current[p] = "—"

                if fy_ev[p] and ebitda and ebitda > 0:
                    ev_ebitda[p] = _fmt_x(_safe_div(fy_ev[p], ebitda))
                else:
                    ev_ebitda[p] = "N/A"

            # ── FCF/EV, CFO/EV, CFO/Share, EV/FCF, EV/CFO (FY-end basis) ──
            # Suppressed for financials, matching EV/Sales and EV/EBITDA
            # above -- these are all EV-denominated, and EV isn't a
            # meaningful concept for banks (debt is raw material, not
            # capital structure).
            #
            # CFO-based metrics (CFO/EV, EV/CFO, CFO/Share) are additionally
            # suppressed for REITs: GAAP CFO for a REIT includes real-estate
            # depreciation add-backs that don't behave like a normal
            # operating-cash proxy -- validated against yfinance consensus,
            # REIT CFO shows ~-99% deltas. FFO (Funds From Operations) is
            # the sector-standard cash metric, not in scope here. FCF-based
            # metrics (FCF/EV, EV/FCF) are NOT suppressed -- FCF = CFO -
            # CapEx is still a GAAP figure, just annotated as such via the
            # REIT footnote in the renderer rather than per-cell (keeps
            # these dicts holding plain floats/None, not a third value
            # shape mixing numbers with inline annotation text).
            fcf_i = _safe_val(fcf_arr, i)
            cfo_i = _safe_val(cfo_arr, i)
            ev_p  = fy_ev[p]
            _reit_na = "N/A (REIT — use FFO)"

            # ── EV/FFO, FFO/EV, EV/AFFO, AFFO/EV (REITs only) ──────────────
            # FFO/AFFO come from FundamentalAgent's per-period computation
            # (NAREIT definitions); reuses the same fy_ev[p] as EV/EBITDA.
            if sg == "real_estate":
                ffo_i  = _ffo_by_period.get(p)
                affo_i = _affo_by_period.get(p)
                if ev_p and ev_p > 0:
                    if isinstance(ffo_i, (int, float)) and ffo_i > 0:
                        ev_ffo_rows[p] = round(ev_p / ffo_i, 2)
                        ffo_ev_rows[p] = round(ffo_i / ev_p * 100, 2)
                    elif isinstance(ffo_i, (int, float)) and ffo_i <= 0:
                        ev_ffo_rows[p] = "N/A (neg FFO)"
                        ffo_ev_rows[p] = round(ffo_i / ev_p * 100, 2)
                    else:
                        ev_ffo_rows[p] = None
                        ffo_ev_rows[p] = None
                    if isinstance(affo_i, (int, float)) and affo_i > 0:
                        ev_affo_rows[p] = round(ev_p / affo_i, 2)
                        affo_ev_rows[p] = round(affo_i / ev_p * 100, 2)
                    elif isinstance(affo_i, (int, float)) and affo_i <= 0:
                        ev_affo_rows[p] = "N/A (neg AFFO)"
                        affo_ev_rows[p] = round(affo_i / ev_p * 100, 2)
                    else:
                        ev_affo_rows[p] = None
                        affo_ev_rows[p] = None
                else:
                    ev_ffo_rows[p] = None
                    ffo_ev_rows[p] = None
                    ev_affo_rows[p] = None
                    affo_ev_rows[p] = None

            _fin_na = "N/A (financials — use P/TBV instead)"
            if sg == "financials":
                # CapEx/FCF are non-core for banks/insurers (PP&E is not the
                # capital-allocation lever -- loan/investment portfolio is),
                # so the FCF-based multiples are suppressed with a pointer to
                # P/TBV. CFO-based metrics are kept: operating cash flow is
                # still a meaningful figure for financials.
                fcf_ev_rows[p] = _fin_na
                ev_fcf_rows[p] = _fin_na
                cfo_ev_rows[p] = round(cfo_i / ev_p * 100, 2) if (cfo_i is not None and ev_p and ev_p > 0) else None
                if ev_p and ev_p > 0:
                    if cfo_i is not None and cfo_i > 0:
                        ev_cfo_rows[p] = round(ev_p / cfo_i, 2)
                    elif cfo_i is not None and cfo_i <= 0:
                        ev_cfo_rows[p] = "N/A (neg CFO)"
                    else:
                        ev_cfo_rows[p] = None
                else:
                    ev_cfo_rows[p] = None
            else:
                # FCF/EV -- yield metric, negative is a meaningful (if
                # unwelcome) signal, so no sign guard.
                fcf_ev_rows[p] = round(fcf_i / ev_p * 100, 2) if (fcf_i is not None and ev_p and ev_p > 0) else None
                cfo_ev_rows[p] = (_reit_na if sg == "real_estate" else
                                  (round(cfo_i / ev_p * 100, 2) if (cfo_i is not None and ev_p and ev_p > 0) else None))

                # EV/FCF -- multiple ("how many years of cash flow to pay
                # for EV"), where a negative value is nonsensical rather
                # than meaningful, so shown as an explicit N/A instead of a
                # misleading negative multiple.
                if ev_p and ev_p > 0:
                    if fcf_i is not None and fcf_i > 0:
                        ev_fcf_rows[p] = round(ev_p / fcf_i, 2)
                    elif fcf_i is not None and fcf_i <= 0:
                        ev_fcf_rows[p] = "N/A (neg FCF)"
                    else:
                        ev_fcf_rows[p] = None
                else:
                    ev_fcf_rows[p] = None

                if sg == "real_estate":
                    ev_cfo_rows[p] = _reit_na
                elif ev_p and ev_p > 0:
                    if cfo_i is not None and cfo_i > 0:
                        ev_cfo_rows[p] = round(ev_p / cfo_i, 2)
                    elif cfo_i is not None and cfo_i <= 0:
                        ev_cfo_rows[p] = "N/A (neg CFO)"
                    else:
                        ev_cfo_rows[p] = None
                else:
                    ev_cfo_rows[p] = None

            # Implausibility guard: diluted share counts outside this range
            # signal a bad XBRL tag (e.g. reported in millions rather than
            # absolute count) rather than a real mega/micro-cap share count.
            _shares_plausible = bool(shares) and 1_000_000 < shares < 500_000_000_000
            if sg == "real_estate":
                cfo_share_rows[p] = _reit_na
            else:
                cfo_share_rows[p] = round(cfo_i / shares, 4) if (cfo_i is not None and _shares_plausible) else None

            # ── Pre-split period suppression ─────────────────────────────
            # This period's share count is as-filed (never retroactively
            # split-adjusted) while its FY-end price is split-adjusted, so
            # every metric that divides one by the other -- or that scales
            # market cap off the share count -- is wrong by the split
            # factor. Suppress rather than publish a number that is off by
            # an order of magnitude. Non-per-share metrics (margins, ROE,
            # ROA, turnover, revenue, CCC) are unaffected and untouched:
            # they never see the share count.
            if p in contaminated:
                # EPS and BVPS divide by the same share count as the
                # multiples, so they suppress on the same condition.
                eps_rows[p] = bvps_rows[p]              = _PRESPLIT_NA
                pe_hist[p]  = pb_hist[p] = ptbv[p]      = _PRESPLIT_NA
                ev_sales[p] = ev_ebitda[p]              = _PRESPLIT_NA
                ev_fcf_rows[p]  = ev_cfo_rows[p]        = _PRESPLIT_NA
                fcf_ev_rows[p]  = cfo_ev_rows[p]        = _PRESPLIT_NA
                cfo_share_rows[p]                       = _PRESPLIT_NA
                fy_ev[p] = None
                if sg == "real_estate":
                    ev_ffo_rows[p]  = ffo_ev_rows[p]    = _PRESPLIT_NA
                    ev_affo_rows[p] = affo_ev_rows[p]   = _PRESPLIT_NA

        # ── FCF/EV, CFO/EV, CFO/Share, EV/FCF, EV/CFO -- current-price basis ──
        # Current EV nets FY-end... i.e. today's cash (market_cap + debt -
        # cash), unlike ev_ebitda_current/ev_sales_current above, which
        # don't net cash (kept unchanged -- out of scope for this change).
        # Both are legitimate "current EV" readings that differ only in
        # whether cash is netted; this is a pre-existing asymmetry between
        # the historical (nets cash) and original current-basis (doesn't)
        # formulas, not something introduced here.
        curr_debt      = _safe_val(bal.total_debt, 0)
        curr_op_lease  = _safe_val(bal.operating_lease_liabilities, 0)
        curr_fin_lease = _safe_val(bal.finance_lease_liabilities, 0)
        curr_cash      = _safe_val(bal.cash, 0)
        curr_ev        = ((market_cap or 0) + (curr_debt or 0)
                           + (curr_op_lease or 0) + (curr_fin_lease or 0) - (curr_cash or 0))
        # EV/FCF, EV/CFO, FCF/EV, CFO/EV (TTM): fall back to FY-basis FCF/CFO
        # when TTM isn't available for this ticker.
        _ttm_fcf = _ttm.get("FCF") if _ttm else None
        _ttm_cfo = _ttm.get("CFO") if _ttm else None
        curr_fcf   = _ttm_fcf if _ttm_fcf is not None else _safe_val(fcf_arr, 0)
        curr_cfo   = _ttm_cfo if _ttm_cfo is not None else _safe_val(cfo_arr, 0)
        if _yf_shares_outstanding:
            curr_shares = float(_yf_shares_outstanding)
            curr_shares_source = "yfinance"
        else:
            curr_shares = _safe_val(inc.diluted_shares, 0)
            curr_shares_source = "xbrl"

        if not curr_ev or curr_ev <= 0:
            curr_fcf_ev = None
            curr_cfo_ev = None
            curr_ev_fcf = None
            curr_ev_cfo = None
        elif sg == "financials":
            curr_fcf_ev = _fin_na
            curr_ev_fcf = _fin_na
            curr_cfo_ev = round(curr_cfo / curr_ev * 100, 2) if curr_cfo is not None else None
            if curr_cfo is not None and curr_cfo > 0:
                curr_ev_cfo = round(curr_ev / curr_cfo, 2)
            elif curr_cfo is not None and curr_cfo <= 0:
                curr_ev_cfo = "N/A (neg CFO)"
            else:
                curr_ev_cfo = None
        else:
            curr_fcf_ev = round(curr_fcf / curr_ev * 100, 2) if curr_fcf is not None else None
            curr_cfo_ev = _reit_na if sg == "real_estate" else (
                round(curr_cfo / curr_ev * 100, 2) if curr_cfo is not None else None)
            if curr_fcf is not None and curr_fcf > 0:
                curr_ev_fcf = round(curr_ev / curr_fcf, 2)
            elif curr_fcf is not None and curr_fcf <= 0:
                curr_ev_fcf = "N/A (neg FCF)"
            else:
                curr_ev_fcf = None
            if sg == "real_estate":
                curr_ev_cfo = _reit_na
            elif curr_cfo is not None and curr_cfo > 0:
                curr_ev_cfo = round(curr_ev / curr_cfo, 2)
            elif curr_cfo is not None and curr_cfo <= 0:
                curr_ev_cfo = "N/A (neg CFO)"
            else:
                curr_ev_cfo = None

        _curr_shares_plausible = bool(curr_shares) and 1_000_000 < curr_shares < 500_000_000_000
        if sg == "real_estate":
            curr_cfo_share = _reit_na
        else:
            curr_cfo_share = (round(curr_cfo / curr_shares, 4)
                              if (curr_cfo is not None and _curr_shares_plausible) else None)

        # ── EV/FFO, FFO/EV, EV/AFFO, AFFO/EV -- current-price basis (REITs) ──
        # No live/TTM FFO source exists, so "current" reuses the most recent
        # fiscal year's FFO/AFFO against today's EV (same convention as
        # curr_fcf/curr_cfo above, which reuse fcf_arr[0]/cfo_arr[0]).
        curr_ev_ffo = curr_ffo_ev = curr_ev_affo = curr_affo_ev = None
        if sg == "real_estate" and curr_ev and curr_ev > 0 and periods:
            _curr_ffo  = _ffo_by_period.get(periods[0])
            _curr_affo = _affo_by_period.get(periods[0])
            if isinstance(_curr_ffo, (int, float)):
                curr_ffo_ev = round(_curr_ffo / curr_ev * 100, 2)
                curr_ev_ffo = round(curr_ev / _curr_ffo, 2) if _curr_ffo > 0 else "N/A (neg FFO)"
            if isinstance(_curr_affo, (int, float)):
                curr_affo_ev = round(_curr_affo / curr_ev * 100, 2)
                curr_ev_affo = round(curr_ev / _curr_affo, 2) if _curr_affo > 0 else "N/A (neg AFFO)"

        # ── Premium valuation flag (most recent period only) ────────────────
        # Uses ev_ebitda_current (today's price basis), not ev_ebitda (now a
        # per-FY historical figure -- see EV/EBITDA block above) -- this flag
        # is about current valuation risk, matching pe_curr's basis below.
        mr_p = periods[0] if periods else None
        if mr_p:
            ev_eb_str = ev_ebitda_current.get(mr_p, "")
            pe_cr_str = pe_curr.get(mr_p, "")
            ev_eb_f   = _parse_x(ev_eb_str) if "x" in str(ev_eb_str) else None
            pe_cr_f   = _parse_x(pe_cr_str) if "x" in str(pe_cr_str) else None
            _ev_thresh = RED_FLAG_THRESHOLDS.get("ev_ebitda_premium", 30.0)
            _pe_thresh = RED_FLAG_THRESHOLDS.get("pe_current_premium", 45.0)
            ev_trigger = ev_eb_f is not None and ev_eb_f > _ev_thresh
            pe_trigger = pe_cr_f is not None and pe_cr_f > _pe_thresh
            if ev_trigger or pe_trigger:
                parts = []
                if ev_trigger:
                    parts.append(f"EV/EBITDA {ev_eb_str}")
                if pe_trigger:
                    parts.append(f"P/E {pe_cr_str} (current price)")
                flags.append(
                    f"{' | '.join(parts)} — premium valuation; "
                    f"limited margin of safety if guidance misses"
                )

        # ── Shareholder yields + diluted share change ───────────────────────
        # These live here rather than in FundamentalAgent because they need
        # the FY-end market cap already derived above for fy_ev, and must
        # honour the same split/unit-anomaly suppression as the other
        # per-share metrics -- a yield computed against a pre-split share
        # count is wrong by the split factor exactly as P/E is.
        _sr = (fundamental or {}).get("shareholder_returns", {}) or {}
        dividend_yield_rows = {}
        buyback_yield_rows  = {}
        total_yield_rows    = {}
        share_change_rows   = {}
        if _sr.get("available"):
            for i, p in enumerate(periods):
                if p in contaminated:
                    dividend_yield_rows[p] = _PRESPLIT_NA
                    buyback_yield_rows[p]  = _PRESPLIT_NA
                    total_yield_rows[p]    = _PRESPLIT_NA
                    share_change_rows[p]   = _PRESPLIT_NA
                    continue
                mcap = fy_mcap_rows.get(p)
                div  = _sr.get("dividends_paid", {}).get(p)
                nbb  = _sr.get("net_buyback", {}).get(p)
                dy = (div / mcap * 100) if (div and mcap and mcap > 0) else None
                by = (nbb / mcap * 100) if (nbb is not None and mcap and mcap > 0) else None
                dividend_yield_rows[p] = dy
                buyback_yield_rows[p]  = by
                total_yield_rows[p] = ((dy or 0) + (by or 0)) if (dy is not None or by is not None) else None

                # Diluted share count change vs. the prior year, on the
                # anomaly-resolved series (shares_rows), not the raw XBRL
                # vector -- otherwise a split boundary reads as a 900%
                # "dilution" event.
                prev_p = periods[i + 1] if (i + 1) < len(periods) else None
                cur_sh = shares_rows.get(p)
                prv_sh = shares_rows.get(prev_p) if prev_p else None
                if (prev_p and prev_p not in contaminated
                        and cur_sh and prv_sh and prv_sh > 0):
                    share_change_rows[p] = (cur_sh - prv_sh) / prv_sh * 100
                else:
                    share_change_rows[p] = None

            # Dilution flag, most recent period only.
            _sc_mr = share_change_rows.get(periods[0]) if periods else None
            if isinstance(_sc_mr, (int, float)) and _sc_mr > 2.0:
                flags.append(
                    f"Diluted share count +{_sc_mr:.1f}% YoY ({_fy(periods[0])}) — "
                    f"dilution outpacing buybacks"
                )

        # ── YoY / CAGR trend annotations (non-multiple metrics only) ────────
        # Only the yield and per-share metrics get a growth annotation:
        # FCF/EV, CFO/EV, CFO/Share (+ FFO/EV, AFFO/EV for REITs). The
        # multiples above (P/E, P/B, EV/EBITDA, EV/Sales, EV/FCF, EV/CFO)
        # are point-in-time valuations, not growth series, so a YoY change
        # or CAGR on them would not mean anything.
        eps_trend       = _compute_yoy_and_cagr(eps_rows,       periods)
        bvps_trend      = _compute_yoy_and_cagr(bvps_rows,      periods)
        fcf_ev_trend    = _compute_yoy_and_cagr(fcf_ev_rows,    periods)
        cfo_ev_trend    = _compute_yoy_and_cagr(cfo_ev_rows,    periods)
        cfo_share_trend = _compute_yoy_and_cagr(cfo_share_rows, periods)
        ffo_ev_trend    = _compute_yoy_and_cagr(ffo_ev_rows,    periods)
        affo_ev_trend   = _compute_yoy_and_cagr(affo_ev_rows,   periods)

        # ── WACC ─────────────────────────────────────────────────────────────
        wacc = _compute_wacc(
            ticker     = profile.ticker,
            market_cap = market_cap,
            profile    = profile,
            fred_data  = fred_data or {},
            fundamental= fundamental or {},
        )

        return {
            "periods":        periods,
            "pe_historical":  pe_hist,
            "pb_historical":  pb_hist,
            "pe_current":     pe_curr,
            "pb_current":     pb_curr,
            "ptbv":           ptbv,
            "ev_sales":         ev_sales,
            "ev_ebitda":        ev_ebitda,
            "operating_lease_liabilities": {"current": op_lease_rows.get(periods[0]) if periods else None,
                                             "by_period": op_lease_rows},
            "finance_lease_liabilities":   {"current": fin_lease_rows.get(periods[0]) if periods else None,
                                             "by_period": fin_lease_rows},
            "ev_sales_current":  ev_sales_current,
            "ev_ebitda_current": ev_ebitda_current,
            "fcf_ev":         {"current": curr_fcf_ev, "by_period": fcf_ev_rows},
            "cfo_ev":         {"current": curr_cfo_ev, "by_period": cfo_ev_rows},
            "cfo_per_share":  {"current": curr_cfo_share, "by_period": cfo_share_rows},
            "ev_fcf":         {"current": curr_ev_fcf, "by_period": ev_fcf_rows},
            "ev_cfo":         {"current": curr_ev_cfo, "by_period": ev_cfo_rows},
            "ev_ffo":         {"current": curr_ev_ffo, "by_period": ev_ffo_rows},
            "ffo_ev":         {"current": curr_ffo_ev, "by_period": ffo_ev_rows},
            "ev_affo":        {"current": curr_ev_affo, "by_period": ev_affo_rows},
            "affo_ev":        {"current": curr_affo_ev, "by_period": affo_ev_rows},
            "split_contamination": {
                "periods": sorted(contaminated),
                "factor":  split_info.get("factor"),
                "direction": split_info.get("direction"),
                "before":  split_info.get("before"),
                "after":   split_info.get("after"),
                "classes":     _anomaly["classes"],
                "ratios":      _anomaly["ratios"],
                "corrections": _anomaly["corrections"],
            },
            "shareholder_yields": {
                "dividend_yield":   dividend_yield_rows,
                "buyback_yield":    buyback_yield_rows,
                "total_yield":      total_yield_rows,
                "share_change_yoy": share_change_rows,
            },
            # "Current" reuses the most recent fiscal year's figure: there is
            # no live EPS/BVPS series, and the Current P/E and P/B columns
            # divide today's price by exactly these values -- so showing them
            # here makes the multiple above reproducible from the table.
            "eps":  {"current": eps_rows.get(periods[0]) if periods else None,
                     "by_period": eps_rows},
            "bvps": {"current": bvps_rows.get(periods[0]) if periods else None,
                     "by_period": bvps_rows},
            "eps_trend":       eps_trend,
            "bvps_trend":      bvps_trend,
            "fcf_ev_trend":    fcf_ev_trend,
            "cfo_ev_trend":    cfo_ev_trend,
            "cfo_share_trend": cfo_share_trend,
            "ffo_ev_trend":    ffo_ev_trend,
            "affo_ev_trend":   affo_ev_trend,
            "diluted_shares": {"current": curr_shares, "by_period": shares_rows},
            "shares_outstanding": {"current": curr_shares if curr_shares_source == "yfinance" else None,
                                    "by_period": {}},
            "shares_source":  {"current": curr_shares_source, "by_period": shares_source},
            "current_price":  current_price,
            "market_cap":     market_cap,
            "sector_group":   sg,
            "wacc":           wacc,
            "flags":          flags,
        }


def _compute_wacc(ticker: str,
                  market_cap: float | None,
                  profile,
                  fred_data: dict,
                  fundamental: dict) -> dict:
    """
    Compute WACC using CAPM for cost of equity and RF+OAS for cost of debt.

    Cost of Equity  = RF + Beta × ERP          (CAPM)
    After-tax Kd    = (RF + OAS) × (1 - tax)
    WACC            = We × Ke + Wd × Kd_at

    Returns dict with all components plus final WACC. All values decimal.
    N/A strings used when a component cannot be computed.
    """
    from market.erp_fetcher import get_erp
    _NA = "N/A"

    result = {
        "beta":          _NA,
        "erp":           _NA,
        "erp_date":      _NA,
        "cost_of_equity":_NA,
        "cost_of_debt":  _NA,
        "tax_rate":      _NA,
        "kd_after_tax":  _NA,
        "weight_equity": _NA,
        "weight_debt":   _NA,
        "wacc":          _NA,
        "available":     False,
    }

    # ── Financials guard ──────────────────────────────────────────────────────
    # WACC is not meaningful for banks/insurers: debt IS their raw material
    # (deposits, repo, issued notes fund the asset book). A CAPM+WACC framework
    # conflates funding costs with the hurdle rate and is not used by bank analysts.
    sg = _sector_group(profile.sector, getattr(profile, "ticker", ""))
    if sg == "financials":
        result["wacc"] = "N/A (financials — WACC not applicable)"
        return result

    # ── ERP (Damodaran, auto-fetched) ─────────────────────────────────────────
    try:
        erp, erp_date = get_erp()
        result["erp"]      = erp
        result["erp_date"] = erp_date
    except Exception as e:
        logger.warning("_compute_wacc: ERP fetch failed — %s", e)
        return result

    # ── Risk-free rate ────────────────────────────────────────────────────────
    rf = fred_data.get("risk_free")
    if rf is None:
        logger.warning("_compute_wacc: no risk-free rate from FRED")
        return result

    # ── Beta from yfinance ────────────────────────────────────────────────────
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        beta = info.get("beta")
        if beta is None or not isinstance(beta, (int, float)):
            raise ValueError("beta not available")
        result["beta"] = round(float(beta), 2)
    except Exception as e:
        logger.warning("_compute_wacc: beta fetch failed for %s — %s", ticker, e)
        return result

    # ── Cost of Equity (CAPM) ─────────────────────────────────────────────────
    ke = rf + float(result["beta"]) * erp
    result["cost_of_equity"] = ke

    # ── Cost of Debt ──────────────────────────────────────────────────────────
    # Use rating-implied OAS + RF; fall back to wtd avg effective rate if available
    all_spreads    = fred_data.get("all_spreads", {})
    from core.config import get_rating
    rating_info    = get_rating(ticker)
    primary_rating = rating_info.get("sp_rating") or rating_info.get("moodys_rating")

    oas = None
    if primary_rating:
        oas_tier = _RATING_TO_TIER.get(primary_rating)
        oas      = all_spreads.get(oas_tier) if oas_tier else None

    if oas is not None:
        kd = rf + oas
    else:
        logger.warning("_compute_wacc: no OAS for %s, cost of debt unavailable", ticker)
        return result

    result["cost_of_debt"] = kd

    # ── Tax rate ──────────────────────────────────────────────────────────────
    periods = profile.periods
    tax_str = fundamental.get("effective_tax", {}).get(periods[0], "") if periods else ""
    try:
        tax = _parse_pct(tax_str)
        if tax is None or tax <= 0 or tax > 0.50:
            tax = 0.21   # US statutory fallback
    except Exception:
        tax = 0.21
    result["tax_rate"] = tax

    kd_at = kd * (1 - tax)
    result["kd_after_tax"] = kd_at

    # ── Capital structure weights ─────────────────────────────────────────────
    # Total debt from balance sheet (most recent period)
    try:
        total_debt = profile.balance_sheet.total_debt[0] or 0.0
    except Exception:
        total_debt = 0.0

    if not market_cap or market_cap <= 0:
        logger.warning("_compute_wacc: no market cap for %s", ticker)
        return result

    total_capital = market_cap + total_debt
    we = market_cap  / total_capital
    wd = total_debt  / total_capital

    result["weight_equity"] = we
    result["weight_debt"]   = wd

    # ── WACC ─────────────────────────────────────────────────────────────────
    wacc = we * ke + wd * kd_at
    result["wacc"]      = wacc
    result["available"] = True

    logger.info(
        "_compute_wacc: %s  beta=%.2f  Ke=%.2f%%  Kd=%.2f%%  "
        "We=%.1f%%  Wd=%.1f%%  WACC=%.2f%%",
        ticker, result["beta"], ke*100, kd_at*100, we*100, wd*100, wacc*100
    )
    return result


# ─────────────────────────────────────────────
# 4. TrendCommentaryAgent
# ─────────────────────────────────────────────

_GUIDANCE_PATTERNS = [
    r"we expect[^.]{0,200}\.",
    r"we anticipate[^.]{0,200}\.",
    r"we believe[^.]{0,200}\.",
    r"our outlook[^.]{0,200}\.",
    r"going forward[^.]{0,200}\.",
    r"for (?:fiscal|the full year|20\d\d)[^.]{0,200}\.",
    r"guidance[^.]{0,200}\.",
    r"capital allocation[^.]{0,200}\.",
    r"we plan[^.]{0,200}\.",
    r"we intend[^.]{0,200}\.",
    r"we remain confident[^.]{0,200}\.",
    r"our (?:strategy|priority|focus)[^.]{0,200}\.",
    r"dividend[^.]{0,200}\.",
    r"share repurchase[^.]{0,200}\.",
    r"return to shareholders[^.]{0,200}\.",
]

_GUIDANCE_RE = re.compile(
    "|".join(_GUIDANCE_PATTERNS),
    re.IGNORECASE | re.DOTALL
)

_MIN_GUIDANCE_LEN = 40   # chars — filters fragments like "we expect" and table cells


def _extract_mda_guidance(filing_obj, max_sentences: int = 8) -> list:
    try:
        doc = filing_obj.obj()
        mda_text = ""
        if hasattr(doc, 'sections'):
            for section in doc.sections:
                name = str(section).lower()
                if "item 7" in name or ("management" in name and "discussion" in name):
                    mda_text = str(section)
                    break
        if not mda_text and hasattr(filing_obj, 'text'):
            full = filing_obj.text()
            idx = full.lower().find("management")
            mda_text = full[idx: idx + 15000] if idx > 0 else full[:15000]
        if not mda_text:
            return []

        # Split into sentences first, then match patterns only against full sentences.
        # This prevents mid-sentence captures (e.g. "...we expect will translate...")
        # and table-cell matches (e.g. "Dividends paid 1").
        sentence_endings = re.compile(r'(?<=[.!?])\s+')
        sentences = sentence_endings.split(mda_text)

        seen, results = set(), []
        for sentence in sentences:
            clean = " ".join(sentence.split()).strip(". ")
            if len(clean) < _MIN_GUIDANCE_LEN:
                continue
            if _GUIDANCE_RE.search(clean):
                if clean not in seen:
                    seen.add(clean)
                    results.append(clean)
            if len(results) >= max_sentences:
                break
        return results
    except Exception:
        return []


# ── Boilerplate exclusion ─────────────────────────────────────────────────────
# 8-K filings embed exhibit metadata, officer titles, and section headers before
# the actual narrative. These match content patterns (they contain "revenue",
# "net income", "capital", etc.) but are not guidance. Filter them out first.
_BOILERPLATE_RE = re.compile(
    r"(?:"
    r"8-K Exhibit|EX-99\.|Exhibit\s+99\.|"
    r"(?:Senior|Executive|Vice)\s+(?:Vice\s+)?President|"
    r"Chief\s+(?:Financial|Executive|Accounting|Operating|Legal)\s+Officer|"
    r"Title:\s*(?:Executive|Senior|Vice|President|Chief)|"
    r"Date:\s*\w+\s+\d{1,2},\s*\d{4}\s+\d+\s+8-K|"
    r"Controller\s+8-K|"
    r"Deputy\s+(?:General\s+Counsel|Corporate\s+Secretary)|"
    # Business description boilerplate (appears in 10-Q business overview)
    r"We also earn revenues from|"
    r"we generate revenue(?:s)? from|"
    r"our (?:primary|principal) source(?:s)? of revenue|"
    r"forward-looking statements.*safe harbor|"
    r"actual results.*differ materially|"
    r"risks and uncertainties.*annual report"
    r")",
    re.IGNORECASE
)

# ── Reported-results exclusion ───────────────────────────────────────────────
# Sentences that describe what already happened — quarterly actuals, comparisons
# to prior periods, and highlights bullets. These match forward_re keywords
# (net income, revenue, earnings per share) but are not guidance.
# Exclude before categorisation.
_ACTUALS_RE = re.compile(
    r"(?:"
    r"(?:fiscal|for\s+(?:the\s+)?)?(?:Q[1-4]|first|second|third|fourth)\s+(?:quarter|fiscal)(?:\s+20\d\d)?"
    r"(?:[^.]{0,50}(?:highlight|result|report|record|revenue|income|earning))?"
    r"|(?:revenue|net\s+income|earnings?\s+per\s+share|diluted\s+(?:eps|earnings))\s+(?:of|was|were)\s+\$"
    r"|versus\s+(?:the\s+)?(?:prior|same|last)\s+(?:quarter|period|year)"
    r"|compared\s+to\s+(?:the\s+)?(?:prior|same|last)\s+(?:quarter|period|year)"
    r"|for\s+the\s+(?:quarter|period|year)\s+ended"
    r"|(?:increased|decreased|grew|declined|rose|fell)\s+(?:\d|from\s+\$)"
    r")",
    re.IGNORECASE
)

# ── TAKEAWAYS bullet classification ───────────────────────────────────────────
# Used to split Motley Fool TAKEAWAYS section bullets into forward/backward.
# Runs BEFORE the general sentence extractor so bullet format is handled
# correctly without requiring full sentence structure.

# Forward-looking bullet triggers (guidance, outlook, projections)
_BULLET_FORWARD_RE = re.compile(
    r"(?:"
    r"\bguidance\b|\boutlook\b|\bforecast(?:ed|s)?\b|"
    r"\bexpect(?:ed|s)?\b|\bproject(?:ed|s)?\b|\banticipat(?:ed|es)?\b|"
    r"\btarget(?:ed|s|ing)?\b|"
    r"\bwill\s+(?:be|decline|grow|increase|decrease|improve|remain)\b|"
    r"\bnext\s+(?:quarter|year|fiscal|month)\b|"
    r"\bQ[1-4]\s*(?:FY|fiscal|20[0-9]{2})\b|"
    r"\bfull.year\s+(?:20[0-9]{2}|guid|expect|outlook)\b|"
    r"\b20[0-9]{2}\s+(?:guid|expect|outlook|target|forecast)\b|"
    r"\bgoing forward\b|\bfor(?:ecast)?\s+(?:fiscal|the full year|20[0-9]{2})\b|"
    r"\bprojected\b|\bforecasted\b|\bexpected\b|\banticipated\b|"
    r"\bplan(?:s|ning)?\s+to\b|\bintend(?:s)?\s+to\b|"
    r"^Guidance:|\bGuide(?:s|d)?:|\bOutlook:|"
    r"\badjusted\s+(?:EPS|eps|operating\s+margin|revenue)\s+(?:of\s+)?\$[0-9]|"
    r"\bfull.year\s+(?:adjusted|guidance|EPS|eps|revenue|margin)\b"
    r")",
    re.IGNORECASE
)

# Backward-looking: reported results, actuals, comparisons — must NOT appear in guidance
_BULLET_BACKWARD_RE = re.compile(
    r"(?:"
    # Reported financial results
    r"\breported\b|\brecorded\b|\bdelivered\b|\bachieved\b|"
    r"\brecord(?:\s+(?:result|revenue|quarter|high|low))?\b|"
    r"\bsurpass(?:ing|ed)?\b|\bexceed(?:ing|ed)?\s+guidance\b|"
    r"\bmark(?:ing|ed)\s+a\s+record\b|"
    r"\bup\s+[0-9]+%|\bdown\s+[0-9]+%|"
    r"\b(?:grew|grown|declined|fell|rose|increased|decreased)\s+[0-9]|"
    r"(?:earnings|EPS|income|margin|revenue)\s+(?:of|was|were)\s+\$|"
    r"\brevenue\s+(?:of|was|were)\b|"
    # Comparison language
    r"\bcompared\s+to\b|\bversus\s+(?:prior|last|same)\b|"
    r"\byear.over.year\s+(?:growth|decline|increase|decrease)\b|"
    r"\b(?:prior|previous|last)\s+(?:year|quarter|period)\b|"
    r"for the (?:quarter|period|year) end|"
    # Reporting basis
    r"on a reported basis|"
    r"on a (?:currency.neutral|constant.currency) basis|"
    # Result-framing language
    r"\bbenefitting\s+from\b|\bdriven\s+by\b|\bdue\s+to\b|"
    r"\baligning\s+with\b|\bin\s+line\s+with\s+(?:prior|previous|last)\b|"
    r"\brepresenting\s+[0-9]+%|"
    # Exceeded/beat language (reported vs guidance)
    r"\bexceed(?:ing|ed)\s+(?:our\s+)?guidance\b|"
    r"\bsurpass(?:ing|ed)?\s+(?:the\s+)?(?:top|high|upper)\b|"
    r"\babove\s+(?:our\s+|the\s+)?guidance\b|"
    r"\bbelow\s+guidance\s+due\b"
    r")",
    re.IGNORECASE
)



def _classify_takeaway_bullets(text: str) -> tuple[list[str], list[str]]:
    """
    Extract and classify bullets from a TAKEAWAYS section.

    Returns (forward_bullets, backward_bullets).

    The TAKEAWAYS format is:
        * **Label** -- content text
    or
        * content text

    Forward bullets: contain guidance/outlook/projection language
    Backward bullets: contain reported results / historical figures
    Ambiguous bullets: classified as backward (safer — avoids false guidance)
    """
    forward  = []
    backward = []

    # Extract all bullet lines from the TAKEAWAYS block.
    # Handles two formats:
    #   Markdown: "* **Label** -- content"  (web fetch from sandbox/markdown)
    #   Plain text: "Label -- content."     (actual Fool HTML stripped)
    bullets = []

    # Format 1: markdown bullets (* or - prefix)
    md_bullets = re.findall(
        r'^\s*[\*\-•]\s*(?:\*\*[^*]+\*\*\s*[-–:]+\s*)?(.+)$',
        text, re.MULTILINE
    )
    bullets.extend(md_bullets)

    # Format 2: plain text "Label -- content" (no * prefix)
    # Keep full "Label -- content" so forward classifier sees label triggers
    if not md_bullets:
        plain = re.findall(
            r'([A-Z][^\n-]{2,50}\s*[-–]+\s*[^\n]{20,})',
            text
        )
        bullets.extend(plain)

    # Format 2b: newline-separated prose sentences (SUMMARY_BULLETS — no label, no * prefix)
    # Each line is a complete sentence. Split on \n, keep lines ≥40 chars.
    if not bullets:
        line_bullets = [l.strip() for l in text.split('\n') if len(l.strip()) >= 40]
        if line_bullets:
            bullets.extend(line_bullets)

    # Format 3: last resort — sentence splitter (rarely needed now)
    if not bullets:
        bullets = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if len(s.strip()) > 30]

    for bullet in bullets:
        bullet = bullet.strip()
        if len(bullet) < 20:
            continue

        is_forward  = bool(_BULLET_FORWARD_RE.search(bullet))
        is_backward = bool(_BULLET_BACKWARD_RE.search(bullet))

        # Hard backward signals override any forward signal.
        # These patterns unambiguously describe reported results.
        _HARD_BACKWARD = re.compile(
            r"marking a record|surpassing|exceeding (?:our |the )?guidance|"
            r"above (?:the |our )?(?:top|high|upper)|"
            r"below guidance due|"
            r"[0-9]+% year.over.year|"
            r"\$[0-9]+(?:\.[0-9]+)?\s*(?:billion|million|B\b|M\b).*(?:record|up\s+[0-9]|grew\s+[0-9]|all.time)",
            re.IGNORECASE
        )
        if _HARD_BACKWARD.search(bullet):
            backward.append(bullet)
        elif is_forward and not is_backward:
            forward.append(bullet)
        elif is_forward and is_backward:
            # Both signals — forward wins only if it has a concrete future number
            has_future_num = bool(re.search(
                r"(?:guid|forecast|project|expect|target|raised|increas|updat).*\$[0-9]|"
                r"\$[0-9]+(?:\.[0-9]+)?\s*(?:billion|million|B\b|M\b)|"
                r"[0-9]+%.*(?:guid|forecast|project|expect|target)|"
                r"(?:guid|forecast|project|expect|target).*[0-9]+%",
                bullet, re.IGNORECASE
            ))
            if has_future_num:
                forward.append(bullet)
            else:
                backward.append(bullet)
        else:
            backward.append(bullet)

    return forward, backward


# ── Earnings release guidance patterns ─────────────────────────────────────────
# Tuned for 8-K press release language: concrete numbers, forward guidance,
# NII/expense outlooks, capital deployment statements, CEO/CFO quotes.
_EARNINGS_PATTERNS = [
    r"we expect[^.]{0,300}\.",
    r"we anticipate[^.]{0,300}\.",
    r"we are (?:guiding|targeting)[^.]{0,300}\.",
    r"(?:NII|net interest income)[^.]{0,300}(?:billion|million|guidance|outlook|expect)[^.]{0,100}\.",
    r"(?:expense|expenses)[^.]{0,200}(?:billion|million|guidance|expect)[^.]{0,100}\.",
    r"(?:full.year|full year)[^.]{0,200}(?:expect|guid|target|approximately)[^.]{0,100}\.",
    r"(?:capital)[^.]{0,200}(?:deploy|return|distribut|buyback|repurchase|dividend)[^.]{0,100}\.",
    r"(?:outlook|guidance)[^.]{0,300}\.",
    r"(?:approximately|roughly)\s+\$[\d\.]+\s*(?:billion|million)[^.]{0,200}\.",
    r"going forward[^.]{0,300}\.",
    r"for (?:fiscal|the full year|20\d\d)[^.]{0,300}\.",
    r"(?:2025|2026)[^.]{0,200}(?:expect|guid|target|approximately|billion)[^.]{0,100}\.",
    r"(?:dividend|buyback|repurchase)[^.]{0,300}\.",
    r"(?:loan growth|deposit growth|revenue growth)[^.]{0,300}\.",
    r"we remain[^.]{0,300}\.",
]

_EARNINGS_RE = re.compile(
    "|".join(_EARNINGS_PATTERNS),
    re.IGNORECASE | re.DOTALL
)

_MIN_EARNINGS_LEN = 60   # longer minimum — press releases have very short sentences too


def _extract_earnings_guidance(text: str, max_sentences: int = 6) -> list:
    """
    Extract forward-looking guidance from an 8-K earnings press release narrative.
    Prefers concrete statements with numbers over generic outlook language.
    """
    try:
        if not text:
            return []

        # Split into sentences
        sentence_endings = re.compile(r'(?<=[.!?])\s+')
        sentences = sentence_endings.split(text)

        # Score each sentence: prefer those with dollar amounts or percentages
        scored = []
        seen = set()
        for sentence in sentences:
            clean = " ".join(sentence.split()).strip(". ")
            if len(clean) < _MIN_EARNINGS_LEN:
                continue
            if _BOILERPLATE_RE.search(clean):
                continue
            if not _EARNINGS_RE.search(clean):
                continue
            if clean in seen:
                continue
            seen.add(clean)
            # Higher score for sentences with concrete numbers
            has_number = bool(re.search(r'\$[\d,\.]+\s*(?:billion|million)|[\d\.]+%', clean))
            scored.append((clean, 1 if has_number else 0))

        # Sort: concrete numbers first, then other guidance
        scored.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in scored[:max_sentences]]

    except Exception:
        return []



# ─────────────────────────────────────────────
# 5. GuidanceAgent
# ─────────────────────────────────────────────

# ── FinBERT singleton ────────────────────────────────────────────────────────
# Loaded once per process, reused across all tickers in a batch run.
# Falls back to regex _score_tone() silently when torch/model unavailable.
_finbert_bundle = None

def _load_finbert_once():
    global _finbert_bundle
    if _finbert_bundle is not None:
        return _finbert_bundle
    try:
        import torch
        from transformers import BertTokenizer, BertForSequenceClassification
        device    = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        tokenizer = BertTokenizer.from_pretrained("yiyanghkust/finbert-tone")
        model     = BertForSequenceClassification.from_pretrained(
                        "yiyanghkust/finbert-tone")
        model     = model.to(device)
        model.eval()

        class _Bundle: pass
        b           = _Bundle()
        b.tokenizer = tokenizer
        b.model     = model
        b.device    = device
        b.id2label  = model.config.id2label  # {0:Neutral, 1:Positive, 2:Negative}
        _finbert_bundle = b
        logger.info("FinBERT-tone loaded on %s", device)
        return b
    except Exception as e:
        logger.warning("FinBERT unavailable (%s) — regex fallback active", e)
        return None


def _finbert_score(text: str) -> tuple[str, float, list[str]]:
    """
    Score text with FinBERT-tone (primary scorer when fool_summary available).
    Returns (label, confidence, signals) — same signature as _score_tone().
    Falls back to _score_tone() if model unavailable or inference fails.
    """
    bundle = _load_finbert_once()
    if bundle is None:
        return _score_tone(text)
    try:
        import torch
        inputs = bundle.tokenizer(
            text, truncation=True, max_length=512, return_tensors="pt"
        ).to(bundle.device)
        with torch.no_grad():
            probs = torch.softmax(bundle.model(**inputs).logits, dim=-1)[0]

        idx       = probs.argmax().item()
        raw_label = bundle.id2label.get(idx, "Neutral")
        label_map = {"Positive": "Confident", "Negative": "Cautious",
                     "Neutral": "Neutral"}
        label     = label_map.get(raw_label, "Neutral")
        score     = round(probs[idx].item(), 4)

        # Top-2 labels as signals for transparency in PDF
        top2    = probs.topk(min(2, len(probs)))
        signals = [
            f"{label_map.get(bundle.id2label.get(i.item(), 'Neutral'), 'Neutral')}"
            f":{v:.2f}"
            for i, v in zip(top2.indices, top2.values)
        ]
        return label, score, signals

    except Exception as e:
        logger.warning("FinBERT inference failed (%s) — regex fallback", e)
        return _score_tone(text)


# ── Tone lexicons ─────────────────────────────────────────────────────────────

# Tone lexicons use regex patterns so inflections match cleanly and
# signal words displayed are readable English, not stem fragments.
# Each entry: (display_label, compiled_pattern)
# Lexicon updated from FinBERT corpus analysis (854 transcripts, 21.3% mismatch).
# Primary gap was Neutral→Confident (91 cases): management satisfaction language
# not captured by the original growth/momentum-focused lexicon.
# Cautious additions from Confident→Cautious mismatches (18 cases): decline/shortfall.
_TONE_CONFIDENT = [
    # Original entries
    ("record",              re.compile(r"record(?:-high|-breaking)?", re.I)),
    ("strong",              re.compile(r"strong(?:er|est)?", re.I)),
    ("robust",              re.compile(r"robust", re.I)),
    ("resilient",           re.compile(r"resilien(?:t|ce)", re.I)),
    ("momentum",            re.compile(r"momentum", re.I)),
    ("accelerating",        re.compile(r"accele?rat(?:e|ing|ion|ed)?", re.I)),
    ("outperform",          re.compile(r"outperform(?:ing|ed|ance)?", re.I)),
    ("exceed",              re.compile(r"exceed(?:ing|ed|s)?", re.I)),
    ("deliver",             re.compile(r"deliver(?:ed|ing|y)?", re.I)),
    ("growth",              re.compile(r"grow(?:th|ing|n|s)?", re.I)),
    ("expansion",           re.compile(r"expan(?:d|sion|ding|ded)", re.I)),
    ("opportunity",         re.compile(r"opportunit(?:y|ies)", re.I)),
    ("optimistic",          re.compile(r"optimis(?:t|tic|m)", re.I)),
    ("confident",           re.compile(r"confiden(?:t|ce)", re.I)),
    ("disciplined",         re.compile(r"disciplined?", re.I)),
    ("solid",               re.compile(r"solid", re.I)),
    ("constructive",        re.compile(r"constructive", re.I)),
    ("well-positioned",     re.compile(r"well.positioned", re.I)),
    ("durable",             re.compile(r"durable", re.I)),
    ("healthy",             re.compile(r"healthy", re.I)),
    # FinBERT-corpus additions — management satisfaction language (91 Neutral→Confident gaps)
    ("pleased",             re.compile(r"(?:very|extremely|incredibly|quite)?\s*pleased", re.I)),
    ("encouraged",          re.compile(r"encourag(?:ed|ing)?", re.I)),
    ("successfully",        re.compile(r"success(?:ful(?:ly)?|fully)", re.I)),
    ("executing",           re.compile(r"execut(?:e|ing|ed|ion)", re.I)),
    ("progress",            re.compile(r"progress(?:ing)?", re.I)),
    ("advantageous",        re.compile(r"advantag(?:eous|e|ed|ing)", re.I)),
    ("beat",                re.compile(r"(?<!\w)beat(?:ing)?(?!\w)", re.I)),
    ("favorable",           re.compile(r"favor(?:able|ably)", re.I)),
    ("improving",           re.compile(r"improv(?:e|ing|ed|ement)", re.I)),
    ("positive",            re.compile(r"positive(?:ly)?", re.I)),
    ("profitability",       re.compile(r"profitab(?:le|ility)", re.I)),
    ("ahead",               re.compile(r"(?<!\w)ahead(?!\w)", re.I)),
]

_TONE_CAUTIOUS = [
    # Original entries
    ("uncertainty",         re.compile(r"uncertain(?:ty|ties)?", re.I)),
    ("headwind",            re.compile(r"headwind(?:s)?", re.I)),
    ("challenging",         re.compile(r"challeng(?:e|ing|ed|es)?", re.I)),
    ("volatile",            re.compile(r"volatilit?(?:y|ies)?|volatile", re.I)),
    ("difficult",           re.compile(r"difficult(?:y|ies)?", re.I)),
    ("deteriorating",       re.compile(r"deteriorat(?:e|ing|ion|ed)?", re.I)),
    ("slowdown",            re.compile(r"slowdown", re.I)),
    ("pressure",            re.compile(r"pressur(?:e|es|ed|ing)?", re.I)),
    ("concern",             re.compile(r"concern(?:s|ed|ing)?", re.I)),
    ("cautious",            re.compile(r"cautious(?:ly)?", re.I)),
    ("tariff",              re.compile(r"tariff(?:s)?", re.I)),
    ("inflation",           re.compile(r"inflationar?(?:y)?|inflation", re.I)),
    ("recession",           re.compile(r"recess(?:ion|ions)?", re.I)),
    ("credit loss",         re.compile(r"credit\s+loss(?:es)?", re.I)),
    ("softening",           re.compile(r"softe?n(?:ing|ed)?", re.I)),
    ("elevated",            re.compile(r"elevated", re.I)),
    ("weakness",            re.compile(r"weak(?:ness|er|est)?", re.I)),
    ("disappointed",        re.compile(r"disappoint(?:ed|ing|ment)?", re.I)),
    ("miss",                re.compile(r"(?<!\w)miss(?:ed|ing|es)?(?!\w)", re.I)),
    ("geopolitical",        re.compile(r"geopolit(?:ical|ics)?", re.I)),
    # FinBERT-corpus additions — decline/shortfall language (18 Confident→Cautious gaps)
    ("decline",             re.compile(r"declin(?:e|ed|ing|es)?", re.I)),
    ("fell short",          re.compile(r"fell\s+(?:well\s+)?short", re.I)),
    ("compression",         re.compile(r"compress(?:ion|ed|ing)?", re.I)),
    ("contraction",         re.compile(r"contract(?:ion|ed|ing)?", re.I)),
    ("below expectations",  re.compile(r"below\s+(?:our\s+)?expectations?", re.I)),
    ("margin pressure",     re.compile(r"margin\s+(?:pressure|compress)", re.I)),
    ("down year-on-year",   re.compile(r"down\s+\d+%?\s+year.on.year", re.I)),
]

# ── Category patterns ─────────────────────────────────────────────────────────
# Grounded in real 10-Q/10-K MD&A language studied from MU, NKE, ORCL filings.
# First-match-wins across categories — order matters.

_CAT_PATTERNS = {
    # Financial targets: concrete guidance on revenue, margins, EPS, FCF, capex.
    # Covers both exact numbers ($X billion, X%) and directional language
    # (expand, improve, grow, decline, pressure). Matches first-person and
    # third-person ("Micron expects", "the company is targeting").
    "financial_targets": re.compile(
        r"(?:"
        # Metric keyword anchor
        r"(?:NII|net interest income|EPS|earnings per share|diluted earnings|"
        r"adjusted EPS|diluted EPS|non-GAAP EPS|"
        r"net income|gross margin|operating margin|net margin|ebitda margin|"
        r"expense|expenses|operating expense|SG&A|R&D expense|"
        r"revenue|total revenue|net revenue|"
        r"return on (?:equity|assets|tangible|invested capital)|ROTCE|ROE|ROA|ROIC|"
        r"operating cash flow|free cash flow|adjusted free cash flow|"
        r"capital expenditure|capex|construction.related capex|"
        r"tax rate|effective tax rate|bit shipment|bit growth|ASP|"
        r"production volume|wafer output|HBM supply)"
        r"[^.]{0,300}"
        # Qualifier — number OR directional word
        r"(?:\$[\d,\.]+\s*(?:billion|million|B|M)|[\d\.]+\s*%|"
        r"approximately|guidance|target|expect|anticipat|project|forecast|"
        r"step up|ramp|increase|declin|expand|improv|grow|compress|"
        r"widen|strengthen|accelerat|moderat|stabili|recover|"
        r"pressure|pressur|contract|dilut|exceed|surpass|record|"
        r"sequential|year-over-year|YoY|significant|exceptional|substantial)"
        r"[^.]{0,100}\."
        r"|"
        # Arm 2: qualifier-before-metric (e.g. "expect records across revenue/EPS")
        r"(?:we\s+expect|we\s+anticipate|we\s+are\s+targeting|we\s+plan)"
        r"[^.]{0,80}(?:record|exceptional|significant|substantial)\s+[^.]{0,80}"
        r"(?:revenue|margin|EPS|earnings per share|free cash flow|capex|net income)"
        r"[^.]{0,100}\."
        r")",
        re.IGNORECASE | re.DOTALL
    ),

    # Capital allocation: dividends, buybacks, debt management, capital returns.
    # Distinct from capex (which belongs in financial_targets as a cost metric).
    "capital_allocation": re.compile(
        r"(?:buyback|repurchas|dividend|quarterly dividend|annual dividend|"
        r"capital return|capital distribut|shareholder return|share repurchas|"
        r"CET1|capital ratio|tier 1|"
        r"excess capital|capital deployment|"
        r"debt reduction|debt repay|repaid|refinanc|"
        r"principal debt|long.term debt|leverage target|net leverage|"
        r"share count|diluted share|treasury stock|"
        r"return.*shareholder|shareholder.*return)[^.]{0,300}\.",
        re.IGNORECASE | re.DOTALL
    ),

    # Growth outlook: volume, demand, market share, product adoption, pipeline.
    # Covers supply/demand dynamics (key for semis, energy, commodities),
    # SaaS metrics (ARR, RPO, bookings), and traditional lending metrics.
    "growth_outlook": re.compile(
        r"(?:"
        # Supply/demand dynamics — critical for semis, energy, materials
        r"supply.{0,30}(?:tight|constrain|exceed|outpac|short)|"
        r"demand.{0,30}(?:strong|robust|exceed|outpac|grow|AI|data center)|"
        r"bit shipment|bit growth|HBM demand|DRAM demand|NAND demand|"
        r"industry supply|industry demand|supply constraint|demand environment|"
        # Market/volume metrics
        r"loan growth|deposit growth|revenue growth|volume growth|"
        r"card growth|mortgage|market share|wallet share|"
        r"new customer|new account|AUM|asset under management|"
        r"production ramp|production guid|production target|"
        r"design win|customer adoption|customer win|"
        # SaaS / tech metrics
        r"daily active|monthly active|user growth|subscriber|"
        r"remaining performance obligation|RPO|backlog|bookings|"
        r"deferred revenue|contract value|annual recurring|ARR|"
        r"net revenue retention|NRR|attach rate|"
        # General
        r"pipeline|ad revenue|search revenue|cloud revenue|"
        r"content revenue|streaming|units sold|shipments)[^.]{0,300}\.",
        re.IGNORECASE | re.DOTALL
    ),

    # Risk factors: credit, operational, regulatory, competitive, geopolitical.
    # Matches both prose risk sentences and structured bullet risk disclosures.
    "risk_factors": re.compile(
        r"(?:credit loss|provision|charge-off|delinquenc|default|write.?down|impairment|"
        r"geopolit|tariff|trade war|trade restriction|export control|sanction|"
        r"CAC|China.*restrict|restrict.*China|"
        r"regulat|stress test|compliance|litigation|legal proceeding|antitrust|"
        r"headwind|uncertain|concern|challeng|volatile|volatility|"
        r"inventory.{0,50}(?:elevated|excess|build|correction)|"
        r"promotional|pricing pressure|competi|supply chain|"
        r"demand softness|demand weakness|demand uncertaint|"
        r"commodity price|oil price|interest rate risk|"
        r"fx risk|foreign exchange|currency|"
        r"cybersecurit|data breach|operational risk|"
        r"construction delay|fab ramp|yield risk)[^.]{0,300}\.",
        re.IGNORECASE | re.DOTALL
    ),

    # Macro view: economy-wide conditions management references as context.
    "macro_view": re.compile(
        r"(?:economy|economic|GDP|recession|inflation|interest rate|"
        r"federal reserve|fed|monetary policy|fiscal policy|"
        r"consumer spending|consumer demand|consumer confidence|"
        r"labor market|employment|unemployment|"
        r"deregulat|tariff environment|trade policy|"
        r"macro|global growth|global economy|"
        r"market condition|industry cycle|memory cycle|"
        r"semiconductor cycle|PC market|smartphone market|"
        r"AI infrastructure|data center build|hyperscaler)[^.]{0,300}\.",
        re.IGNORECASE | re.DOTALL
    ),
}


def _categorise_sentences(sentences: list[str]) -> dict:
    """
    Assign each sentence to the FIRST matching category only.
    First-match-wins prevents the same sentence appearing under multiple
    category headers (common for sentences that mention both revenue numbers
    and cloud/growth keywords, e.g. MSFT and NVDA earnings releases).
    Returns dict of category → list of sentences (capped at 3 each).
    """
    result = {cat: [] for cat in _CAT_PATTERNS}
    seen_per_cat = {cat: set() for cat in _CAT_PATTERNS}

    for s in sentences:
        for cat, pattern in _CAT_PATTERNS.items():
            if pattern.search(s) and s not in seen_per_cat[cat]:
                result[cat].append(s)
                seen_per_cat[cat].add(s)
                break  # first-match-wins: stop after first matching category

    return {cat: sents[:5] for cat, sents in result.items()}


def _score_tone(text: str) -> tuple[str, float, list[str]]:
    """
    Score management tone from -1.0 (very cautious) to +1.0 (very confident).
    Returns (label, score, signal_words_found).

    Each lexicon entry is (display_label, compiled_regex).
    A label is counted once per text regardless of how many times it appears
    — prevents a single repeated word from dominating the score.
    Signal words returned are the readable display labels, not stem fragments.
    """
    confident_labels = []
    cautious_labels  = []
    seen_conf = set()
    seen_caut = set()

    for label, pattern in _TONE_CONFIDENT:
        if pattern.search(text) and label not in seen_conf:
            confident_labels.append(label)
            seen_conf.add(label)

    for label, pattern in _TONE_CAUTIOUS:
        if pattern.search(text) and label not in seen_caut:
            cautious_labels.append(label)
            seen_caut.add(label)

    c_count = len(confident_labels)
    k_count = len(cautious_labels)
    total   = c_count + k_count

    if total == 0:
        return "Neutral", 0.0, []

    raw_score = (c_count - k_count) / total  # -1 to +1

    if raw_score >= RED_FLAG_THRESHOLDS.get("tone_positive_threshold", 0.20):
        label = "Confident"
    elif raw_score <= RED_FLAG_THRESHOLDS.get("tone_negative_threshold", -0.20):
        label = "Cautious"
    else:
        label = "Neutral"

    # Return up to 4 signals — balanced: top confident then top cautious.
    # Prioritise cautious signals when tone is Neutral or Cautious.
    if label == "Confident":
        signals = confident_labels[:3] + cautious_labels[:1]
    elif label == "Cautious":
        signals = cautious_labels[:3] + confident_labels[:1]
    else:
        signals = confident_labels[:2] + cautious_labels[:2]

    return label, round(raw_score, 3), signals[:4]


def _split_sentences(text: str) -> list[str]:
    """
    Split text into sentences, handling both period-terminated prose and
    bullet-style 8-K press releases that use • or newlines as delimiters.

    Strategy:
      1. Normalise bullet characters and bare newlines into '. ' so the
         period splitter treats each bullet item as its own sentence.
      2. Run the standard period/question/exclamation splitter.
      3. Strip and discard empty fragments.
    """
    # Replace bullet characters with a period+space so they split cleanly
    normalised = re.sub(r'\s*[•·–—]\s*', '. ', text)
    # Replace bare newlines — but only if the line doesn't already end with punctuation
    # This prevents "31.5% for the year.\n" → "31.5% for the year.. " double-period
    normalised = re.sub(r'(?<![.!?])\n+', '. ', normalised)
    normalised = re.sub(r'(?<=[.!?])\n+', ' ', normalised)
    # Collapse runs of period+space created by the above
    normalised = re.sub(r'(?:\.\s*){2,}', '. ', normalised)

    # Protect decimal numbers and common abbreviations from sentence splitting
    # Split on sentence boundaries — NOT on decimals (31.5%) or abbreviations
    # Negative lookbehind: skip split when period directly follows a digit
    sentence_endings = re.compile(r'(?<![0-9])(?<=[.!?])\s+')
    return [s.strip() for s in sentence_endings.split(normalised) if s.strip()]








def _classify_source_sections(text: str) -> dict[str, str]:
    """
    Split structured transcript text (from TranscriptLoader) into named sections.
    Returns dict: {section_name: section_text}
    Handles both modern (## SECTION) and legacy formats.
    """
    sections = {}
    current  = "body"
    current_lines = []

    for line in text.split('\n'):
        header = re.match(r'^([A-Z][A-Z\s_]+):\s*$', line.strip())
        if header:
            if current_lines:
                sections[current] = '\n'.join(current_lines).strip()
            current       = header.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections[current] = '\n'.join(current_lines).strip()

    return sections


def _extract_all_guidance_sentences(text: str) -> list[str]:
    """
    Extract every forward-looking sentence from earnings text.

    Handles two input formats:
      1. Structured transcript (TranscriptLoader output):
           TAKEAWAYS:\\n bullets...\\n\\nRISKS:\\n...\\n\\nSUMMARY:\\n...
         → TAKEAWAYS bullets classified with _classify_takeaway_bullets()
         → SUMMARY + PREPARED REMARKS run through general sentence extractor
      2. Unstructured prose (SEC filings, 8-K, older transcripts):
         → General sentence extractor as before
    """
    if not text:
        return []

    seen    = set()
    results = []

    def _add(sentences):
        for s in sentences:
            clean = ' '.join(s.split()).strip('. ')
            if len(clean) < 60:
                continue
            # Dedup key: lowercase, strip punctuation/quotes for fuzzy match
            dedup_key = re.sub(r'[^a-z0-9\s]', '', clean.lower())[:80]
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            results.append(clean if clean[-1] in '."' else clean + '.')

    # ── Detect structured vs unstructured ────────────────────────────────────
    sections = _classify_source_sections(text)
    is_structured = any(
        k in sections for k in ('TAKEAWAYS', 'RISKS', 'SUMMARY', 'SUMMARY_PROSE', 'SUMMARY_BULLETS', 'PREPARED REMARKS', 'OUTLOOK')
    )

    if is_structured:
        # ── Path 1: Structured transcript (Motley Fool) ───────────────────────
        # TAKEAWAYS: classify each bullet forward/backward via regex.
        # SUMMARY_BULLETS: positionally guaranteed forward by Fool editorial structure
        #   (always sits between prose paragraph and INDUSTRY GLOSSARY) — add directly.
        # SUMMARY_PROSE is passed verbatim by GuidanceAgent.analyse as fool_summary.
        if 'TAKEAWAYS' in sections:
            fwd, bwd = _classify_takeaway_bullets(sections['TAKEAWAYS'])
            _add(fwd)
            # backward bullets skipped here — collected separately in GuidanceAgent.analyse

    else:
        # ── Path 2: Unstructured prose (SEC filings, older transcripts) ───────
        _add(_general_guidance_sentences(text))

    # Prioritise sentences with concrete numbers
    with_numbers    = [s for s in results if re.search(r'\$[\d,\.]+\s*(?:billion|million)|[\d\.]+\s*%', s)]
    without_numbers = [s for s in results if s not in set(with_numbers)]
    return with_numbers + without_numbers


def _general_guidance_sentences(text: str) -> list[str]:
    """
    General forward-looking sentence extractor for unstructured prose.
    Original logic extracted from _extract_all_guidance_sentences.
    """
    if not text:
        return []

    sentences = _split_sentences(text)

    forward_re = re.compile(
        r"(?:we expect|we anticipate|we are guid|we target|we plan|we intend|"
        r"we remain|going forward|full.year|for 20\d\d|outlook|guidance|"
        r"approximately \$|NII|net interest income|expense guid|"
        r"loan growth|deposit growth|capital return|buyback|repurchas|dividend|"
        r"geopolit|tariff|inflation|economy|consumer|labor market|"
        r"production guid|capital expenditure|free cash flow|operating cash|"
        r"debt reduction|debt repay|"
        r"user growth|monthly active|daily active|cloud|search revenue|ad revenue|"
        r"fiscal 20\d\d|for (?:fiscal year|the full year)|next (?:quarter|year|fiscal)|"
        r"(?:we\s+expect|we\s+anticipate)\s+[^.]{0,50}record[^.]{0,200}(?:revenue|margin|EPS|cash flow)|"
        r"(?:the\s+company|management)\s+(?:expect|anticipat|target|guid|plan|project)|"
        r"is\s+(?:targeting|guiding|projecting|expecting|planning)|"
        r"(?:provide[sd]?|reaffirm|reiterat|issu[ed]+)\s+guidance|"
        r"expected\s+(?:revenue|margin|EPS|earnings|growth|decline)|"
        r"projected?\s+(?:revenue|margin|EPS|earnings|growth|decline)|"
        r"anticipated?\s+(?:revenue|margin|EPS|earnings|growth|decline))",
        re.IGNORECASE
    )

    seen, results = set(), []
    for sentence in sentences:
        clean = ' '.join(sentence.split()).strip('. ')
        if len(clean) < 60:
            continue
        if _BOILERPLATE_RE.search(clean):
            continue
        if _ACTUALS_RE.search(clean):
            continue
        if not forward_re.search(clean):
            continue
        if clean in seen:
            continue
        seen.add(clean)
        results.append(clean)

    return results



class GuidanceAgent:
    """
    Standalone management guidance and tone analysis agent.

    Analyses the most recent earnings press release (8-K) to produce:
      - Categorised guidance statements (financial targets, capital allocation,
        growth outlook, risk factors, macro view)
      - Management tone score (Cautious / Neutral / Confident)
      - Source attribution (8-K date or fallback label)

    Falls back to 10-K MD&A extraction if no earnings text is available.

    Roadmap (not in this version):
      - Guidance vs actuals: compare prior quarter guidance against current results
      - Management quality score: consistency of guidance over multiple periods
    """

    def analyse(self,
                earnings_text: str = None,
                filing_obj=None,
                filing_objs: list = None,
                transcript: dict = None,        # legacy single-source param
                fool_transcript: dict = None,   # Motley Fool earnings call
                ex99_1: dict = None,            # EDGAR 8-K Exhibit 99.1 press release
                ex99_2: dict = None,            # EDGAR 8-K Exhibit 99.2 prepared remarks
                filing_date: str = None,
                ticker: str = None) -> dict:
        """
        Extract guidance from all available sources independently.
        Each source is extracted and categorised separately so the
        renderer can display them as distinct subsections.

        Sources (all optional, shown independently in PDF):
          fool_transcript : Motley Fool earnings call transcript
          ex99_2          : EDGAR 8-K Exhibit 99.2 prepared remarks
          filing_objs     : 10-Q / 10-K MD&A text (last 4 quarters + annual)
          ex99_1          : EDGAR 8-K Exhibit 99.1 press release

        Legacy params (backward compat):
          transcript      : maps to fool_transcript if fool_transcript is None
          earnings_text   : maps to ex99_1 text if ex99_1 is None
        """
        # ── Backward compat mapping ───────────────────────────────────────
        if fool_transcript is None and transcript is not None:
            fool_transcript = transcript
        if ex99_1 is None and earnings_text:
            ex99_1 = {
                "available": True,
                "source":    "Earnings press release (8-K)",
                "text":      earnings_text,
            }

        # ── Extract each source — conditional priority ───────────────────
        # Priority 1: Fool transcript (always extracted when available)
        # Priority 2: EDGAR 99.2/99.1 (only if Fool unavailable or empty)
        # Priority 3: SEC MD&A filings (last resort — only if both above failed)

        def _extract(src: dict) -> tuple[str, str, list, dict]:
            """Returns (source_label, text, sents, cats) for one source."""
            if not src or not src.get("available") or not src.get("text","").strip():
                return None, "", [], {cat: [] for cat in _CAT_PATTERNS}
            text  = src["text"]
            label = src.get("source", "unknown")
            sents = _extract_all_guidance_sentences(text)
            cats  = _categorise_sentences(sents)
            return label, text, sents, cats

        def _has_content(cats: dict) -> bool:
            return any(v for v in cats.values())

        # Always extract Fool transcript
        fool_label, fool_text, fool_sents, fool_cats = _extract(fool_transcript)
        # Available = transcript fetched successfully, regardless of forward sentence count.
        # A transcript with only backward bullets (e.g. LLY actuals-heavy TAKEAWAYS)
        # still suppresses EDGAR fallback — Results & Context + SUMMARY will render.
        fool_available = bool(fool_label and fool_text and fool_text.strip())

        # Extract backward bullets from TAKEAWAYS only (results/actuals — kept for context)
        # SUMMARY_BULLETS are positionally guaranteed forward — not collected as backward.
        fool_backward_sents = []
        if fool_text:
            _sections = _classify_source_sections(fool_text)
            if 'TAKEAWAYS' in _sections:
                _, _bwd = _classify_takeaway_bullets(_sections['TAKEAWAYS'])
                fool_backward_sents = [b.strip() for b in _bwd if len(b.strip()) >= 20]
        fool_backward_cats = _categorise_sentences(fool_backward_sents)

        # Extract SUMMARY_PROSE verbatim for narrative block in renderer
        fool_summary = None
        fool_summary_bullets = []
        if fool_text:
            _sm = re.search(r'^SUMMARY_PROSE:\s*(.+?)(?=^[A-Z][A-Z\s_]+:|\Z)', fool_text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
            if _sm:
                fool_summary = re.sub(r'\s*\([A-Z]{1,5}\s*[+-]\d+\.?\d*%\)\s*', ' ', _sm.group(1).strip()).strip()
            _sb = re.search(r'^SUMMARY_BULLETS:\s*(.+)', fool_text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
            if _sb:
                # Each line is one sentence bullet — split on newlines and on '. [Capital]'
                raw = _sb.group(1).strip()
                lines = [l.strip() for l in raw.split('\n') if l.strip()]
                bullets = []
                for line in lines:
                    # Split line further on sentence boundaries in case still collapsed
                    sents = re.split(r'(?<=\.)\s+(?=[A-Z])', line)
                    bullets.extend([s.strip() for s in sents if len(s.strip()) >= 30])
                fool_summary_bullets = [
                    re.sub(r'\s*\([A-Z]{1,5}\s*[+-]\d+\.?\d*%\)\s*', ' ', b).strip()
                    for b in bullets
                ]

        # EDGAR sources — only if Fool is unavailable or produced no guidance
        if not fool_available:
            ex99_2_label, ex99_2_text, ex99_2_sents, ex99_2_cats = _extract(ex99_2)
            ex99_1_label, ex99_1_text, ex99_1_sents, ex99_1_cats = _extract(ex99_1)
        else:
            # Still show EDGAR if available — richer picture — but skip if Fool is rich
            fool_sent_count = sum(len(v) for v in fool_cats.values())
            if fool_sent_count >= 3:
                # Fool has enough — skip EDGAR to avoid noise
                ex99_2_label = ex99_2_text = None
                ex99_2_sents = []; ex99_2_cats = {cat: [] for cat in _CAT_PATTERNS}
                ex99_1_label = ex99_1_text = None
                ex99_1_sents = []; ex99_1_cats = {cat: [] for cat in _CAT_PATTERNS}
            else:
                ex99_2_label, ex99_2_text, ex99_2_sents, ex99_2_cats = _extract(ex99_2)
                ex99_1_label, ex99_1_text, ex99_1_sents, ex99_1_cats = _extract(ex99_1)

        # SEC MD&A — last resort only if Fool AND EDGAR both failed/empty
        edgar_available = bool(
            (ex99_2_label and _has_content(ex99_2_cats)) or
            (ex99_1_label and _has_content(ex99_1_cats))
        )
        use_mda = not fool_available and not edgar_available

        mda_text   = ""
        mda_source = None
        if use_mda:
            if filing_objs:
                parts, labels = [], []
                for f in filing_objs:
                    t = self._text_from_filing(f)
                    if t and t.strip():
                        form  = getattr(f, "form", "filing")
                        fdate = getattr(f, "filing_date", "")
                        parts.append(f"--- {form} ({fdate}) ---\n" + t)
                        labels.append(f"{form} ({fdate})")
                if parts:
                    mda_text   = "\n\n".join(parts)
                    mda_source = f"SEC filings: {', '.join(labels[:3])}" + \
                                 (f" +{len(labels)-3} more" if len(labels) > 3 else "")
            if not mda_text and filing_obj is not None:
                mda_text = self._text_from_filing(filing_obj)
                if mda_text and mda_text.strip():
                    form  = getattr(filing_obj, "form", "filing")
                    fdate = getattr(filing_obj, "filing_date", "")
                    mda_source = f"{form} MD&A ({fdate})"

        if mda_text and mda_text.strip():
            mda_sents = _extract_all_guidance_sentences(mda_text)
            mda_cats  = _categorise_sentences(mda_sents)
        else:
            mda_sents, mda_cats = [], {cat: [] for cat in _CAT_PATTERNS}

        # ── Need at least one source ──────────────────────────────────────
        all_texts = [t or "" for t in [fool_text, ex99_2_text, mda_text, ex99_1_text]]
        if not any(all_texts):
            return self._empty(source="unavailable")

        # ── Tone — prefer SUMMARY_PROSE + SUMMARY_BULLETS (Fool editorial
        # distillation) over raw transcript bulk text. The summary is denser
        # signal per word and reflects the Fool editor's own tone read.
        # Primary scorer: FinBERT-tone on fool_summary (dense, correct length for BERT).
        # Fallback chain: regex on full fool_text → richest available source.
        if fool_summary:
            # FinBERT primary — most accurate on editorial summary prose
            tone_label, tone_score, tone_signals = _finbert_score(fool_summary)
        else:
            # Regex fallback — no summary available (older transcripts / EDGAR-only)
            if fool_text:
                tone_text = fool_text[:20000]
            else:
                tone_text = max(all_texts, key=len)[:20000]
            tone_label, tone_score, tone_signals = _score_tone(tone_text)

        # ── Deduplicate within structured sources, preserve transcript priority ──
        # MDA often repeats sentences across multiple 10-Q filings — dedup within.
        # Transcript (fool) and ex99_2 are kept as-is since they're single sources.
        # Cross-source dedup: remove from MDA/ex99_1 anything already in transcript.
        def _dedup_within(sents: list) -> list:
            seen, out = set(), []
            for s in sents:
                k = re.sub(r'[^a-z0-9]', '', s.lower())[:80]
                if k not in seen:
                    seen.add(k)
                    out.append(s)
            return out

        def _dedup_against(sents: list, already_seen: set) -> list:
            out = []
            for s in sents:
                k = re.sub(r'[^a-z0-9]', '', s.lower())[:80]
                if k not in already_seen:
                    already_seen.add(k)
                    out.append(s)
            return out

        # Transcript sources (single-source, just dedup internally)
        fool_sents   = _dedup_within(fool_sents)
        ex99_2_sents = _dedup_within(ex99_2_sents)

        # Build seen set from transcript sources first
        _seen_transcript: set = set()
        for s in fool_sents + ex99_2_sents:
            _seen_transcript.add(re.sub(r'[^a-z0-9]', '', s.lower())[:80])

        # MDA and ex99_1: dedup internally AND remove transcript duplicates
        mda_sents    = _dedup_against(_dedup_within(mda_sents),    set(_seen_transcript))
        ex99_1_sents = _dedup_against(_dedup_within(ex99_1_sents), set(_seen_transcript))
        # Recategorise after dedup
        fool_cats   = _categorise_sentences(fool_sents)
        ex99_2_cats = _categorise_sentences(ex99_2_sents)
        mda_cats    = _categorise_sentences(mda_sents)
        ex99_1_cats = _categorise_sentences(ex99_1_sents)
        all_sents = fool_sents + ex99_2_sents + mda_sents + ex99_1_sents
        all_cats  = _categorise_sentences(all_sents)

        # Primary source label for header
        source = (fool_label or ex99_2_label or mda_source or ex99_1_label or "unavailable")

        # ── Structured forward guidance (LLM extraction) ──────────────────
        # Run on the actual earnings-call text only (Fool transcript first,
        # then EDGAR prepared remarks) — MD&A fallback text is not an
        # earnings call and is far more likely to produce false positives.
        _structured_source_text = fool_text or ex99_2_text or ""
        structured_guidance = _extract_structured_guidance(
            _structured_source_text, ticker or "UNKNOWN"
        )

        return {
            "available":    True,
            "source":       source,
            "filing_date":  filing_date or "N/A",
            "structured_guidance": structured_guidance,
            # Legacy combined key
            "categories":   all_cats,
            # Per-source keys for renderer subsections
            "fool_source":  fool_label,
            "fool_cats":    fool_cats,
            "fool_backward_cats": fool_backward_cats,  # reported results / actuals from TAKEAWAYS
            "fool_summary": fool_summary,               # verbatim SUMMARY_PROSE narrative block
            "fool_summary_bullets": fool_summary_bullets,  # SUMMARY editorial bullets, one per sentence
            "ex99_2_source": ex99_2_label,
            "ex99_2_cats":  ex99_2_cats,
            "mda_source":   mda_source,
            "mda_cats":     mda_cats,
            "ex99_1_source": ex99_1_label,
            "ex99_1_cats":  ex99_1_cats,
            "tone": {
                "label":   tone_label,
                "score":   tone_score,
                "signals": tone_signals,
            },
            "raw_sentences": all_sents,
        }


    @staticmethod
    def _clean_filing_text(text: str) -> str:
        """Clean HTML entities and artifacts from filing text."""
        import re as _re
        for _ in range(2):
            text = _re.sub(r'&amp;',  '&', text)
        text = _re.sub(r'&nbsp;', ' ', text)
        text = _re.sub(r'&quot;', '"', text)
        text = _re.sub(r'&#\d+;', ' ', text)
        text = _re.sub(r';(?=[\s,.()\-]|$)', '', text)  # M&A; -> M&A
        text = _re.sub(r'[ 	]+', ' ', text)
        return text

    @staticmethod
    def _text_from_filing(filing_obj) -> str:
        """
        Extract the forward-looking portion of Item 7 (MD&A) from a 10-K.

        Strategy:
          1. Find Item 7 section via edgartools section iterator
          2. Within Item 7, locate the outlook/forward-looking subsection
             by scanning for common anchor headings
          3. Return 6,000 chars from the anchor (enough for the full outlook)
          4. Fall back to second half of Item 7 if no anchor found (forward-
             looking discussion typically appears after historical analysis)
          5. Fall back to raw filing text search if no sections available
        """
        # Anchor patterns to locate forward-looking content within MD&A.
        # Grounded in real 10-Q structure: guidance lives inside "Results of
        # Operations" and "Liquidity and Capital Resources" — NOT under a
        # standalone "Outlook" heading (which rarely exists in 10-Qs).
        # Also covers 10-K patterns: "Fiscal 20XX Outlook", "Looking Ahead".
        # Uses MULTILINE so ^ matches line starts.
        _OUTLOOK_ANCHORS = re.compile(
            r"(?:"
            # 10-Q section headings containing forward-looking discussion
            r"(?:^|\n)\s*(?:results\s+of\s+operations|"
            r"liquidity\s+and\s+capital\s+resources|"
            r"business\s+overview|overview\s+of\s+results)"
            r"|"
            # 10-K explicit outlook headings
            r"(?:^|\n)\s*(?:fiscal\s+20\d\d\s+outlook|financial\s+outlook|"
            r"business\s+outlook|strategic\s+outlook|looking\s+ahead|"
            r"our\s+outlook|priorities\s+and\s+outlook|"
            r"(?:fiscal\s+)?(?:year\s+)?20\d\d\s+guidance|"
            r"future\s+outlook|market\s+outlook)"
            r")",
            re.IGNORECASE | re.MULTILINE
        )

        try:
            # ── Step 1: get full document text ───────────────────────────────
            full_text = ""

            # Try edgartools sections first
            try:
                doc = filing_obj.obj()
                if hasattr(doc, "sections"):
                    for section in doc.sections:
                        name = str(section).lower()
                        if "item 7" in name or ("management" in name and "discussion" in name):
                            full_text = str(section)
                            break
            except Exception:
                pass

            # Fall back to raw text
            if not full_text and hasattr(filing_obj, "text"):
                full_text = filing_obj.text()

            if not full_text:
                return ""

            # ── Step 2: locate MD&A section within the full document ──────────
            # 10-Q uses Item 2 (MD&A), 10-K uses Item 7.
            # Search by string — more reliable than edgartools sections iterator.
            #
            # Try Item 2 first (10-Q), then Item 7 (10-K).
            _ITEM2_RE = re.compile(
                r"item\s+2[\.\s]*management.{0,60}discussion",
                re.IGNORECASE
            )
            _ITEM7_RE = re.compile(
                r"item\s+7[\.\s]*management.{0,60}discussion",
                re.IGNORECASE
            )

            m_mda = _ITEM2_RE.search(full_text) or _ITEM7_RE.search(full_text)

            if m_mda:
                mda_start = m_mda.start()
                # Determine end boundary — next item section heading.
                # Require at least 15,000 chars gap to skip table-of-contents hits.
                # 10-Q Item 2 ends at Item 3; 10-K Item 7 ends at Item 7A or 8.
                _ITEM_END_RE = re.compile(
                    r"item\s+(?:3|7a|8)[\.\s]",
                    re.IGNORECASE
                )
                m_end = _ITEM_END_RE.search(full_text, mda_start + 15000)
                mda_end = m_end.start() if m_end else mda_start + 60000
                mda_text = full_text[mda_start:mda_end]
            else:
                mda_text = full_text

            # ── Step 3: locate outlook/guidance subsection within Item 7 ─────
            # Search the FULL mda_text for the outlook anchor — do not slice
            # before searching, as the outlook section may be deep in Item 7
            m = _OUTLOOK_ANCHORS.search(mda_text)
            if m:
                start = max(0, m.start() - 100)
                return GuidanceAgent._clean_filing_text(mda_text[start: start + 6000])

            # No section heading anchor — scan for first substantive
            # forward-looking sentence. Skip first 5,000 chars since
            # 10-Q Item 2 opens with financial tables before narrative.
            _SENTENCE_TRIGGER = re.compile(
                r"(?:we\s+expect|we\s+anticipate|we\s+are\s+targeting|"
                r"we\s+plan\s+to|our\s+outlook|looking\s+ahead|"
                r"we\s+believe|we\s+remain|we\s+continue\s+to|"
                r"demand\s+(?:for|is|remains|continue)|"
                r"supply\s+(?:is|remains|continue|constraint))",
                re.IGNORECASE
            )
            m2 = _SENTENCE_TRIGGER.search(mda_text, 5000)
            if m2:
                start = max(0, m2.start() - 200)
                return GuidanceAgent._clean_filing_text(mda_text[start: start + 6000])

            # Last resort — final quarter of Item 7 (outlook is always late)
            start = max(0, len(mda_text) - 10000)
            return GuidanceAgent._clean_filing_text(mda_text[start: start + 8000])

        except Exception:
            return ""

    @staticmethod
    def _empty(source: str = "unavailable", filing_date: str = None) -> dict:
        return {
            "available":    False,
            "source":       source,
            "filing_date":  filing_date or "N/A",
            "structured_guidance": [],
            "categories":   {cat: [] for cat in _CAT_PATTERNS},
            "tone":         {"label": "N/A", "score": 0.0, "signals": []},
            "raw_sentences":     [],
            "fool_source":       None,
            "fool_cats":         {cat: [] for cat in _CAT_PATTERNS},
            "fool_backward_cats": {cat: [] for cat in _CAT_PATTERNS},
            "fool_summary":        None,
            "fool_summary_bullets": [],
            "ex99_2_source":     None,
            "ex99_2_cats":       {cat: [] for cat in _CAT_PATTERNS},
            "mda_source":        None,
            "mda_cats":          {cat: [] for cat in _CAT_PATTERNS},
            "ex99_1_source":     None,
            "ex99_1_cats":       {cat: [] for cat in _CAT_PATTERNS},
            # legacy keys
            "transcript_source": None,
            "transcript_cats":   {cat: [] for cat in _CAT_PATTERNS},
            "edgar_source":      None,
            "edgar_cats":        {cat: [] for cat in _CAT_PATTERNS},
            "transcript_source": None,
            "transcript_cats":   {cat: [] for cat in _CAT_PATTERNS},
            "edgar_source":      None,
            "edgar_cats":        {cat: [] for cat in _CAT_PATTERNS},
        }


# ─────────────────────────────────────────────────────────────────────────
# Structured guidance extraction (LLM-based) — Section 7 rework, Stage 2
#
# Independent of GuidanceTracker's regex-based backward comparison
# (financials/guidance_tracker.py): this extracts structured numeric
# guidance from a single transcript via the Claude API, for display as
# forward guidance and — via _compare_guidance_to_actuals below — for a
# second, LLM-driven guidance-vs-actuals credibility read.
# ─────────────────────────────────────────────────────────────────────────

_STRUCTURED_GUIDANCE_METRICS = {
    "Revenue", "EPS", "Gross Margin", "Operating Income", "Other"
}
_STRUCTURED_GUIDANCE_CONFIDENCE = {"high", "medium", "low"}

_STRUCTURED_GUIDANCE_SYSTEM = (
    "Extract numerical guidance from earnings call transcripts. "
    "Return JSON only. Never invent figures not in the text."
)

_anthropic_client = None
_anthropic_unavailable_reason = None
_anthropic_auth_failed = False   # set once a real call 401s (key present, invalid)


def _structured_guidance_api_available() -> bool:
    """
    True if the Claude API appears usable for structured guidance
    extraction this run — a client can be constructed and no prior call
    has failed with an authentication error. Distinguishes "API
    unavailable" from "API worked, found nothing" for Mode C's message.
    """
    if _anthropic_auth_failed:
        return False
    return _get_anthropic_client() is not None


def _get_anthropic_client():
    """
    Lazily construct and cache a module-level Anthropic client.

    Returns None when the SDK isn't installed or ANTHROPIC_API_KEY isn't
    configured, rather than raising — callers treat that as "extraction
    unavailable" and degrade to an empty result, the same as a transcript
    with no guidance in it.
    """
    global _anthropic_client, _anthropic_unavailable_reason
    if _anthropic_client is not None:
        return _anthropic_client
    if _anthropic_unavailable_reason is not None:
        return None
    try:
        import anthropic
    except ImportError:
        _anthropic_unavailable_reason = "anthropic package not installed"
        logger.warning("_get_anthropic_client: %s", _anthropic_unavailable_reason)
        return None
    from core.config import ANTHROPIC_API_KEY
    if not ANTHROPIC_API_KEY:
        _anthropic_unavailable_reason = "ANTHROPIC_API_KEY not set"
        logger.warning("_get_anthropic_client: %s", _anthropic_unavailable_reason)
        return None
    _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


def _extract_structured_guidance(transcript_text: str, ticker: str) -> list[dict]:
    """
    Extract explicit numerical guidance statements from an earnings call
    transcript.

    Returns list of:
    {
        "metric":    "Revenue" | "EPS" | "Gross Margin"
                     | "Operating Income" | "Other",
        "period":    "Q2 FY2027" | "FY2027" | etc.,
        "low":       float | None,   # in native units
        "high":      float | None,
        "midpoint":  float | None,
        "unit":      "B USD" | "M USD" | "%" | "$",
        "raw_text":  str,  # the sentence it came from
        "confidence":"high" | "medium" | "low",
    }

    Uses the Claude API to extract; the model is instructed never to invent
    figures not present in the text. Returns an empty list if the transcript
    is empty, the API is unavailable, or no guidance is found — never raises.
    API-only: no regex fallback. A regex fallback was tried and reverted —
    without forward-looking context anchoring it mislabeled reported
    actuals as guidance and produced false beat/miss verdicts (e.g. a
    22x-gap "beat"), which is worse than Mode C's honest absence.
    """
    if not transcript_text or not transcript_text.strip():
        return []

    client = _get_anthropic_client()
    if client is None:
        return []

    user_prompt = (
        f"Extract all explicit numerical guidance from this transcript for "
        f"{ticker}:\n{transcript_text[:4000]}"
    )

    try:
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=2000,
            output_config={"effort": "low"},
            system=_STRUCTURED_GUIDANCE_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        global _anthropic_auth_failed
        if "401" in str(e) or "authentication" in str(e).lower():
            # Free-resources users without API access — expected, not worth
            # logging on every ticker. Mode C degrades honestly for these.
            _anthropic_auth_failed = True
        else:
            logger.warning("_extract_structured_guidance(%s): unexpected API "
                           "error — %s", ticker, e)
        return []

    text = "".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    )
    if not text.strip():
        return []

    # Model sometimes wraps the JSON in a markdown code fence — strip it.
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    raw_json = fenced.group(1) if fenced else text.strip()

    try:
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        logger.warning("_extract_structured_guidance(%s): non-JSON response: %.200s",
                       ticker, text)
        return []

    if not isinstance(parsed, list):
        return []

    def _num(v):
        return float(v) if isinstance(v, (int, float)) else None

    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        metric  = item.get("metric")
        raw_txt = item.get("raw_text")
        if not metric or not raw_txt:
            continue
        if metric not in _STRUCTURED_GUIDANCE_METRICS:
            metric = "Other"
        confidence = item.get("confidence")
        if confidence not in _STRUCTURED_GUIDANCE_CONFIDENCE:
            confidence = "low"

        out.append({
            "metric":     metric,
            "period":     str(item.get("period") or "").strip() or None,
            "low":        _num(item.get("low")),
            "high":       _num(item.get("high")),
            "midpoint":   _num(item.get("midpoint")),
            "unit":       str(item.get("unit") or "").strip() or None,
            "raw_text":   str(raw_txt).strip(),
            "confidence": confidence,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────
# Guidance vs. actuals comparison (LLM-extracted guidance) — Stage 3
#
# A second, independent credibility read from the structured guidance
# above — separate from GuidanceTracker's regex-based multi-quarter
# tracker (financials/guidance_tracker.py), which continues to drive
# Execution Quality / Communication Score / the original credibility line.
# ─────────────────────────────────────────────────────────────────────────

_COMPARABLE_STRUCTURED_METRICS = {"Revenue", "Gross Margin"}


def _to_guidance_native_unit(value: float, metric: str, unit: str) -> float:
    """
    Convert an actual (dollars for Revenue, fraction for Gross Margin — the
    scale _build_actual_results in orchestrator.py reports them in) into the
    same native scale as a guidance item's low/high/unit, so beat/meet/miss
    compares like-for-like. Extracted guidance is in whatever unit the model
    reported (e.g. low=89.0 with unit="B USD" means $89 billion) — comparing
    that directly against a raw dollar actual would make every comparison a
    trivial "beat".
    """
    u = (unit or "").upper()
    if metric == "Revenue":
        if "B" in u:
            return value / 1e9
        if "M" in u:
            return value / 1e6
        return value
    if metric == "Gross Margin":
        if "%" in u:
            return value * 100
        return value
    return value


def _compare_guidance_to_actuals(prior_guidance: list[dict],
                                 actual_results: dict) -> list[dict]:
    """
    For each guidance item in prior_guidance (from a Q-1 transcript, via
    _extract_structured_guidance), find the corresponding actual result and
    compute beat/meet/miss.

    actual_results is keyed by lower-cased metric name, in absolute units —
    e.g. {"revenue": 96.3e9, "gross margin": 0.758} (dollars, fraction) —
    and is converted to each guidance item's own native unit before
    comparing (see _to_guidance_native_unit).

    Only Revenue and Gross Margin are compared — the most consistently
    extractable and comparable metrics across tickers. Returns [] if
    prior_guidance or actual_results is unavailable/empty.
    """
    if not prior_guidance or not actual_results:
        return []

    out = []
    for g in prior_guidance:
        metric = g.get("metric")
        if metric not in _COMPARABLE_STRUCTURED_METRICS:
            continue
        lo, hi = g.get("low"), g.get("high")
        if lo is None or hi is None:
            continue
        actual_raw = actual_results.get(metric.lower())
        if actual_raw is None:
            continue
        unit   = g.get("unit")
        actual = _to_guidance_native_unit(actual_raw, metric, unit)

        if actual > hi:
            outcome = "beat"
        elif actual < lo:
            outcome = "miss"
        else:
            outcome = "meet"

        mid = g.get("midpoint")
        if mid is None:
            mid = (lo + hi) / 2
        delta_pct = (actual - mid) / abs(mid) * 100 if mid else 0.0

        out.append({
            "metric":      metric,
            "period":      g.get("period"),
            "guided_low":  lo,
            "guided_high": hi,
            "guided_mid":  mid,
            "actual":      actual,
            "unit":        unit,
            "outcome":     outcome,
            "delta_pct":   round(delta_pct, 1),
        })
    return out


def _score_structured_credibility(comparisons: list[dict]) -> int | None:
    """
    Credibility score (0-100) from _compare_guidance_to_actuals() output.
    Base 50; beat +15, meet +10, miss -10 per comparison; capped [0, 100].
    None when there are no comparisons — updated only once at least one
    guidance-vs-actual comparison is available.
    """
    if not comparisons:
        return None
    score = 50
    for c in comparisons:
        outcome = c.get("outcome")
        if outcome == "beat":
            score += 15
        elif outcome == "meet":
            score += 10
        elif outcome == "miss":
            score -= 10
    return max(0, min(100, score))


class TrendCommentaryAgent:
    """
    Sector-aware deterministic narrative commentary from ratio deltas,
    plus management guidance from the 10-K MD&A.
    """

    def narrate(self, profile, fundamental: dict, risk: dict,
                valuation: dict, filing_obj=None,
                earnings_text: str = None) -> dict:

        periods = profile.periods
        sg = fundamental.get("sector_group", "general")
        comments = []
        flags = []

        inc = profile.income_statement

        if len(periods) < 2:
            return {
                "narrative": ["Insufficient periods for trend analysis."],
                "management_guidance": [],
                "flags": [],
            }

        mr, pr = periods[0], periods[1]

        # ── Revenue trend (all sectors) ───────────────────────────────────
        rev_arr = inc.financial_revenue if sg == "financials" else inc.revenue
        r0, r1 = rev_arr[0], rev_arr[1]
        if r0 and r1 and r1 > 0:
            chg = (r0 - r1) / r1 * 100
            direction = "grew" if chg >= 0 else "declined"
            comments.append(
                f"Revenue {direction} {abs(chg):.1f}% YoY "
                f"({_fmt_money(r1)} → {_fmt_money(r0)})."
            )

        # ── Margin trend (sector-aware label; skip for financials — NIM block below covers it) ──
        gm_label = fundamental.get("gross_margin_label", "Gross Margin")
        gm0 = fundamental["gross_margin"].get(mr, "")
        gm1 = fundamental["gross_margin"].get(pr, "")

        if sg != "financials" and "%" in str(gm0) and "%" in str(gm1):
            gm0_f = _parse_pct(gm0)
            gm1_f = _parse_pct(gm1)
            if gm0_f is not None and gm1_f is not None:
                # Fix: delta is in decimal form (e.g. 0.007 = 0.7pp = 70bps)
                delta_bps = (gm0_f - gm1_f) * 10000
                if sg == "energy":
                    # For energy, higher cost ratio = worse
                    direction = "worsened" if delta_bps > 0 else "improved"
                elif sg == "financials":
                    direction = "expanded" if delta_bps >= 0 else "compressed"
                else:
                    direction = "expanded" if delta_bps >= 0 else "compressed"
                if abs(delta_bps) >= 1:
                    comments.append(
                        f"{gm_label} {direction} {abs(delta_bps):.0f}bps YoY "
                        f"({gm1} → {gm0})."
                    )

        # ── Net margin trend ──────────────────────────────────────────────
        nm0 = fundamental["net_margin"].get(mr, "")
        nm1 = fundamental["net_margin"].get(pr, "")
        if "%" in str(nm0) and "%" in str(nm1):
            nm0_f = _parse_pct(nm0)
            nm1_f = _parse_pct(nm1)
            if nm0_f is not None and nm1_f is not None:
                delta = nm0_f - nm1_f
                direction = "improved" if delta >= 0 else "contracted"
                delta_bps = abs(delta) * 10000
                if delta_bps >= 1:
                    comments.append(
                        f"Net margin {direction} {delta_bps:.0f}bps YoY "
                        f"({nm1} → {nm0})."
                    )

        # ── ROE commentary ────────────────────────────────────────────────
        roe0 = fundamental["roe"].get(mr, "")
        if "%" in str(roe0):
            roe_f = _parse_pct(roe0)
            if roe_f is not None:
                if sg == "financials":
                    benchmark = RED_FLAG_THRESHOLDS.get("roe_benchmark_financials", 0.12)
                    if roe_f >= benchmark:
                        comments.append(f"ROE of {roe0} meets the ~{_pct(benchmark)} threshold for financials.")
                    else:
                        comments.append(f"ROE of {roe0} is below the ~{_pct(benchmark)} benchmark for financials.")
                elif roe_f > RED_FLAG_THRESHOLDS.get("roe_benchmark_general", 0.20):
                    # Guard: high ROE driven purely by leverage is not a shareholder
                    # returns story. Check D/E of most recent period.
                    de_mr_val = _parse_x(risk["de_ratio"].get(mr, ""))
                    de_threshold = RED_FLAG_THRESHOLDS.get("de_ratio", 3.0)
                    if de_mr_val is not None and de_mr_val > de_threshold:
                        comments.append(
                            f"ROE of {roe0} is amplified by financial leverage "
                            f"(D/E: {risk['de_ratio'].get(mr, 'N/A')}); "
                            f"not indicative of operational returns."
                        )
                    else:
                        comments.append(f"ROE of {roe0} reflects strong shareholder returns.")
                elif roe_f > 0:
                    comments.append(f"ROE of {roe0} is positive but below a 20% benchmark.")
                else:
                    comments.append(f"Negative ROE ({roe0}) warrants monitoring of earnings recovery.")
                    flags.append(
                        f"ROE {roe0} ({_fy(mr)}) below 0% — "
                        f"shareholders experiencing negative returns"
                    )

        # ── Leverage commentary ───────────────────────────────────────────
        de_note = risk.get("de_trend_note", "")
        de_mr   = risk["de_ratio"].get(mr, "")
        if de_note and de_mr not in ("N/A", "N/A (neg. equity)"):
            # de_trend_note now stores just the predicate e.g. "decreasing YoY"
            comments.append(f"Leverage {de_note} (D/E: {de_mr} most recently).")

        # ── Liquidity commentary (skip for financials) ────────────────────
        if sg != "financials":
            cr_mr = risk["current_ratio"].get(mr, "")
            if cr_mr and cr_mr not in ("N/A", "N/A (financials)"):
                cr_f = _parse_x(cr_mr)
                if cr_f is not None:
                    if cr_f < 1.0:
                        comments.append(
                            f"Current ratio of {cr_mr} indicates near-term liquidity pressure."
                        )
                    elif cr_f >= 2.0:
                        comments.append(
                            f"Current ratio of {cr_mr} reflects a comfortable liquidity position."
                        )

        # ── Interest coverage commentary (skip for financials) ────────────
        if sg != "financials":
            ic_mr = risk["interest_coverage"].get(mr, "")
            if ic_mr and "x" in str(ic_mr):
                ic_f = _parse_x(ic_mr)
                if ic_f is not None and ic_f < 2.0:
                    comments.append(
                        f"Interest coverage of {ic_mr} is tight; debt service absorbs most EBIT."
                    )
                elif ic_f is not None and ic_f > 10:
                    comments.append(
                        f"Interest coverage of {ic_mr} is strong; debt service is well-covered."
                    )

        # ── NIM commentary (financials only) ──────────────────────────────
        if sg == "financials":
            nim_mr = fundamental["gross_margin"].get(mr, "")
            nim_pr = fundamental["gross_margin"].get(pr, "")
            if "%" in str(nim_mr) and "%" in str(nim_pr):
                nim_f  = _parse_pct(nim_mr)
                nim_pf = _parse_pct(nim_pr)
                if nim_f is not None and nim_pf is not None:
                    delta_bps = (nim_f - nim_pf) * 10000
                    direction = "widened" if delta_bps >= 0 else "compressed"
                    if abs(delta_bps) >= 1:
                        comments.append(
                            f"Net interest margin {direction} {abs(delta_bps):.0f}bps YoY "
                            f"({nim_pr} → {nim_mr})."
                        )

        # ── Valuation commentary ──────────────────────────────────────────
        pe_c = valuation["pe_current"].get(mr, "")
        pe_h = valuation["pe_historical"].get(mr, "")
        if "x" in str(pe_c) and "x" in str(pe_h):
            comments.append(
                f"Trades at {pe_c} P/E (current price) vs. {pe_h} on FY-end price basis."
            )

        # ── DSI outlier note (general sector only) ───────────────────────
        # Fires when DSI exceeds a threshold that would surprise a reader.
        # Sector-aware: pharma/healthcare has long manufacturing lead times
        # and strategic API stockpiling — context note differs from general.
        dsi_mr = fundamental.get("dsi", {}).get(mr, "")
        if "days" in str(dsi_mr) and sg == "general":
            try:
                dsi_val = float(dsi_mr.split()[0])
                _dsi_thresh = 180.0   # configurable if added to pipeline_config.csv
                if dsi_val > _dsi_thresh:
                    sector_lower = (profile.sector or "").lower()
                    if any(k in sector_lower for k in ["health", "pharma", "biotech", "life science"]):
                        comments.append(
                            f"Days Sales of Inventory elevated at {dsi_mr} — "
                            f"typical for pharma/biotech given manufacturing lead times "
                            f"and strategic API stockpiling; monitor for further build."
                        )
                    else:
                        comments.append(
                            f"Days Sales of Inventory elevated at {dsi_mr} — "
                            f"above {_dsi_thresh:.0f}-day threshold; "
                            f"review for demand softness or supply chain buildup."
                        )
            except (ValueError, IndexError):
                pass

        # ── Revenue CAGR ──────────────────────────────────────────────────
        cagr = fundamental.get("revenue_cagr", "N/A")
        if cagr and cagr != "N/A":
            comments.append(f"Revenue {len(periods)-1}-yr CAGR: {cagr}.")

        # ── REIT red flags (FFO/AFFO) ───────────────────────────────────────
        if sg == "real_estate" and fundamental.get("ffo_available"):
            ffo_mr  = fundamental.get("ffo", {}).get(mr)
            ffo_pr  = fundamental.get("ffo", {}).get(pr)
            affo_mr = fundamental.get("affo", {}).get(mr)
            ffo_margin_mr = _parse_pct(fundamental.get("ffo_margin", {}).get(mr, ""))
            ffo_margin_pr = _parse_pct(fundamental.get("ffo_margin", {}).get(pr, ""))
            affo_margin_mr = _parse_pct(fundamental.get("affo_margin", {}).get(mr, ""))
            affo_margin_pr = _parse_pct(fundamental.get("affo_margin", {}).get(pr, ""))

            # FFO margin declining >500bps YoY
            if ffo_margin_mr is not None and ffo_margin_pr is not None:
                delta_bps = (ffo_margin_mr - ffo_margin_pr) * 10000
                if delta_bps < -500:
                    flags.append(
                        f"FFO margin contracted {abs(delta_bps):.0f}bps YoY "
                        f"({_fy(pr)}→{_fy(mr)}) — monitor distribution sustainability"
                    )

            # AFFO margin declining >300bps YoY
            if affo_margin_mr is not None and affo_margin_pr is not None:
                delta_bps = (affo_margin_mr - affo_margin_pr) * 10000
                if delta_bps < -300:
                    flags.append(
                        f"AFFO margin contracted {abs(delta_bps):.0f}bps YoY "
                        f"({_fy(pr)}→{_fy(mr)}) — monitor capex intensity and lease economics"
                    )

            # AFFO negative
            if isinstance(affo_mr, (int, float)) and affo_mr < 0:
                flags.append(
                    "AFFO negative — distribution likely exceeds adjusted cash generation"
                )

            # Dividend payout exceeding FFO (most recent period)
            if isinstance(ffo_mr, (int, float)) and ffo_mr > 0:
                try:
                    divs_paid = abs(profile.cash_flow.common_dividends_paid[0])
                    if divs_paid and divs_paid > ffo_mr:
                        flags.append(
                            f"Dividends (${divs_paid/1e6:,.0f}M) exceed FFO "
                            f"(${ffo_mr/1e6:,.0f}M) — distribution may not be covered "
                            f"by operations"
                        )
                except (IndexError, TypeError):
                    pass

            # FFO-to-AFFO spread > 20%
            if (isinstance(ffo_mr, (int, float)) and ffo_mr > 0
                    and isinstance(affo_mr, (int, float))):
                spread_pct = (ffo_mr - affo_mr) / ffo_mr * 100
                if spread_pct > 20:
                    flags.append(
                        f"Large FFO-to-AFFO gap ({spread_pct:.0f}%) — high non-cash "
                        f"adjustments or maintenance capex intensity"
                    )

            # EV/FFO, EV/AFFO premium valuation (most recent period)
            ev_ffo_mr = valuation.get("ev_ffo", {}).get("by_period", {}).get(mr)
            ev_ffo_f = ev_ffo_mr if isinstance(ev_ffo_mr, (int, float)) else None
            if ev_ffo_f is not None and ev_ffo_f > 30:
                flags.append(
                    f"EV/FFO {ev_ffo_f:.1f}x — premium valuation vs REIT peers "
                    f"(typical range 15-25x)"
                )
            ev_affo_mr = valuation.get("ev_affo", {}).get("by_period", {}).get(mr)
            ev_affo_f = ev_affo_mr if isinstance(ev_affo_mr, (int, float)) else None
            if ev_affo_f is not None and ev_affo_f > 35:
                flags.append(
                    f"EV/AFFO {ev_affo_f:.1f}x — elevated vs REIT peers "
                    f"(typical range 20-30x)"
                )

        # ── Working capital red flags (Section 2B) ──────────────────────────
        # Skipped entirely for industries where Section 2B isn't rendered
        # (banks, insurers, REITs) -- there is no operating working-capital
        # cycle there to deteriorate.
        _wc = fundamental.get("working_capital", {}) or {}
        if _wc.get("mode") in ("full", "partial"):

            def _wc_delta(key):
                """(current, prior, current - prior) for a WC series, or Nones."""
                cur = _wc.get(key, {}).get(mr)
                prv = _wc.get(key, {}).get(pr)
                if isinstance(cur, (int, float)) and isinstance(prv, (int, float)):
                    return cur, prv, cur - prv
                return None, None, None

            # CCC lengthening > 30 days YoY
            _ccc_c, _ccc_p, _ccc_d = _wc_delta("ccc")
            if _ccc_d is not None and _ccc_d > 30:
                flags.append(
                    f"Cash conversion cycle lengthened {_ccc_d:.0f} days YoY "
                    f"({_ccc_p:.0f} → {_ccc_c:.0f} days, {_fy(pr)}→{_fy(mr)}) — "
                    f"receivables or inventory buildup vs payables"
                )

            # DSO expansion > 15 days YoY
            _dso_c, _dso_p, _dso_d = _wc_delta("dso")
            if _dso_d is not None and _dso_d > 15:
                flags.append(
                    f"DSO expanded {_dso_d:.0f} days YoY "
                    f"({_dso_p:.0f} → {_dso_c:.0f} days, {_fy(pr)}→{_fy(mr)}) — "
                    f"monitor receivables collection"
                )

            # DPO compression > 20 days YoY (decline, so delta is negative)
            _dpo_c, _dpo_p, _dpo_d = _wc_delta("dpo")
            if _dpo_d is not None and _dpo_d < -20:
                flags.append(
                    f"DPO compressed {abs(_dpo_d):.0f} days YoY "
                    f"({_dpo_p:.0f} → {_dpo_c:.0f} days, {_fy(pr)}→{_fy(mr)}) — "
                    f"reduced supplier financing benefit"
                )

            # CapEx consuming more than all operating cash flow
            _capex_cfo = _wc.get("capex_pct_cfo", {}).get(mr)
            if isinstance(_capex_cfo, (int, float)) and _capex_cfo > 100:
                flags.append(
                    f"CapEx exceeds operating cash flow "
                    f"({_capex_cfo:.0f}% of CFO, {_fy(mr)}) — "
                    f"free cash flow negative; external financing required"
                )

            # Negative NWC corroborated by a sub-1.0x current ratio
            _nwc = _wc.get("nwc", {}).get(mr)
            _cr_f = _parse_x(risk.get("current_ratio", {}).get(mr, ""))
            if (isinstance(_nwc, (int, float)) and _nwc < 0
                    and _cr_f is not None and _cr_f < 1.0):
                flags.append(
                    f"Negative NWC (-${abs(_nwc)/1e9:,.1f}B, {_fy(mr)}) confirmed by "
                    f"current ratio below 1.0x ({_fmt_x(_cr_f)}) — "
                    f"operational liquidity pressure"
                )

            # Inventory growing faster than revenue (>500bps of revenue YoY)
            _inv_c, _inv_p, _inv_d = _wc_delta("inv_pct_rev")
            if _inv_d is not None and _inv_d > 5.0:
                flags.append(
                    f"Inventory build {_inv_d*100:.0f}bps above revenue growth "
                    f"({_inv_p:.1f}% → {_inv_c:.1f}% of revenue, "
                    f"{_fy(pr)}→{_fy(mr)}) — monitor demand signals"
                )

        # ── Guidance: prefer 8-K earnings release, fall back to 10-K MD&A ────
        guidance = []
        if earnings_text:
            guidance = _extract_earnings_guidance(earnings_text)
        if not guidance and filing_obj is not None:
            guidance = _extract_mda_guidance(filing_obj)

        # Aggregate and deduplicate flags
        all_flags = (
            fundamental.get("flags", [])
            + risk.get("flags", [])
            + valuation.get("flags", [])
            + flags
        )
        seen = set()
        deduped_flags = []
        for f in all_flags:
            if f not in seen:
                seen.add(f)
                deduped_flags.append(f)

        # Collapse per-period threshold flags that repeat across years into
        # one range line, then order by recency (current-period findings
        # first, structural/multi-year last) with severity breaking ties.
        deduped_flags = _collapse_repeated_flags(deduped_flags, periods)
        deduped_flags = _sort_flags_by_recency(deduped_flags, periods)

        return {
            "narrative":           comments,
            "management_guidance": guidance,
            "flags":               deduped_flags,
        }


# ─────────────────────────────────────────────
# Internal formatting helpers
# ─────────────────────────────────────────────

def _fmt_money(val):
    if not val:
        return "N/A"
    if abs(val) >= 1e9:
        return f"${val / 1e9:.1f}B"
    if abs(val) >= 1e6:
        return f"${val / 1e6:.1f}M"
    return f"${val:,.0f}"


def _parse_pct(s: str):
    """Parse '12.34%' -> 0.1234. Also handles annotated strings like '2.31% (loans+AFS)'."""
    try:
        # Extract the first number before or at the '%' sign
        m = re.match(r'^\s*(-?[\d.]+)\s*%', str(s))
        if m:
            return float(m.group(1)) / 100
        return float(str(s).replace("%", "").strip()) / 100
    except Exception:
        return None


def _parse_x(s: str):
    """Parse '2.34x' -> 2.34. Also handles annotated strings like '2.34x (yfinance TTM)'."""
    try:
        m = re.match(r'^\s*(-?[\d.]+)\s*x', str(s))
        if m:
            return float(m.group(1))
        return float(str(s).replace("x", "").strip())
    except Exception:
        return None