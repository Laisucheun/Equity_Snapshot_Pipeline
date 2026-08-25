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


def _sector_group(sector: str, ticker: str = "") -> str:
    """
    Map a sector string to one of four routing groups.
    Returns: 'financials' | 'energy' | 'utilities' | 'general'

    'utilities' is separate from 'energy' because:
      - Regulated utilities should not show Operating Cost Ratio (an E&P metric)
      - Altman Z warrants its own suppression note (model calibrated for manufacturers,
        not regulated utilities with structurally low working capital and high leverage)
      - Interest coverage and D/E are meaningful and should be computed normally

    'real estate' is intentionally NOT in the financials group — REITs have different
    characteristics from banks/insurers and should receive the full general ratio set
    (Gross Margin, D/E, Current Ratio, Altman Z). They will score low on Z-score due
    to structural leverage, which is expected rather than a bug.
    """
    s = sector.lower()
    # Ticker-based fallback — catches banks run without explicit sector
    if ticker.upper() in _FINANCIAL_TICKERS:
        return "financials"

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
    if any(k in s for k in ["energy", "oil", "gas", "mining"]):
        return "energy"
    return "general"


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
        config_path = pathlib.Path(__file__).parent / "pipeline_config.csv"

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
        sg = _sector_group(profile.sector, getattr(profile, 'ticker', ''))

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

        prev_gm = None

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

                # Check prev NIM for compression flag
                if isinstance(gross_margin.get(p), str) and "%" in gross_margin[p]:
                    gm_val = _parse_pct(gross_margin[p])
                    if gm_val is not None and prev_gm is not None:
                        drop_bps = (prev_gm - gm_val) * 10000
                        if drop_bps > RED_FLAG_THRESHOLDS["gross_margin_drop_bps"]:
                            flags.append(
                                f"NIM compressed {drop_bps:.0f}bps: "
                                f"{_pct(prev_gm)} → {_pct(gm_val)} ({_fy(p)}) — "
                                f"interest margin deterioration (threshold "
                                f"{int(RED_FLAG_THRESHOLDS['gross_margin_drop_bps'])}bps)"
                            )
                    prev_gm = _parse_pct(gross_margin[p]) if "%" in gross_margin.get(p, "") else prev_gm

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
                        if prev_gm is not None:
                            # Flag if cost ratio worsened > 500bps
                            rise_bps = (op_cost_ratio - prev_gm) * 10000
                            if rise_bps > RED_FLAG_THRESHOLDS["gross_margin_drop_bps"]:
                                flags.append(
                                    f"Operating cost ratio rose {rise_bps:.0f}bps: "
                                    f"{_pct(prev_gm)} → {_pct(op_cost_ratio)} ({_fy(p)}) — "
                                    f"cost inflation outpacing revenue"
                                )
                        prev_gm = op_cost_ratio
                else:
                    gross_margin[p] = "N/A (costs missing)"

                if rev and oi:
                    op_margin[p] = _pct(_safe_div(oi, rev))
                else:
                    op_margin[p] = "N/A"

                net_margin[p] = _pct(_safe_div(ni, rev)) if rev else "N/A"

            # ── Sector: General (default) ─────────────────────────────────
            else:
                # Gross profit fallback: revenue - cogs
                if not gp and cogs:
                    gp = rev - cogs if rev else 0

                if rev:
                    # Zero-GP guard: when gross profit is zero but operating income
                    # is positive, the filer reports no COGS line (common for REITs
                    # and some utilities — e.g. DLR, MAA). Show N/A rather than 0.00%
                    # which implies a valid zero gross margin.
                    if not gp and oi and oi > 0:
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

                    if isinstance(gm_val, float) and prev_gm is not None:
                        drop_bps = (prev_gm - gm_val) * 10000
                        if drop_bps > RED_FLAG_THRESHOLDS["gross_margin_drop_bps"]:
                            flags.append(
                                f"Gross margin compressed {drop_bps:.0f}bps: "
                                f"{_pct(prev_gm)} → {_pct(gm_val)} ({_fy(p)}) — "
                                f"exceeds {int(RED_FLAG_THRESHOLDS['gross_margin_drop_bps'])}bps warning threshold"
                            )
                    prev_gm = gm_val if isinstance(gm_val, float) else prev_gm

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

            # ── FFO margin (REITs only) ───────────────────────────────────
            # FFO = Net income + D&A - gains on property sales
            # Only computed for Real Estate sector; left blank for all others.
            if "real estate" in (profile.sector or "").lower():
                da_ffo  = profile.cash_flow.depreciation_amortization[i]
                if not da_ffo:
                    da_ffo = inc.depreciation_amortization[i]
                gains   = inc.gain_loss_disposals[i]
                if ni and da_ffo and rev:
                    ffo = ni + abs(da_ffo) - (gains if gains and gains > 0 else 0)
                    ffo_margin[p] = _pct(_safe_div(ffo, rev))
                else:
                    ffo_margin[p] = "N/A"
            else:
                ffo_margin[p] = "N/A (sector)"

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

        return {
            "periods":              periods,
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
            "sector_group":         sg,
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
        sg = _sector_group(profile.sector, getattr(profile, 'ticker', ''))
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
                    # Guard: if the company carries meaningful debt but interest
                    # expense resolves to zero/None, this is a data gap (XBRL
                    # tag missing), not genuinely zero interest expense.
                    # Threshold: debt > 10% of total assets flags as suspect.
                    _debt_material = (
                        debt and ta and debt > 0 and ta > 0
                        and (debt / ta) > 0.10
                    )
                    if _debt_material:
                        interest_cov[p] = "[DATA ERROR — int. exp. tag missing; verify]"
                        if i == 0:
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
        )

        return {
            "periods":           periods,
            "current_ratio":     current_ratio,
            "quick_ratio":       quick_ratio,
            "interest_coverage": interest_cov,
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
                             profile=None) -> dict:
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

    Returns dict with all credit quality fields. All values default to "N/A".
    """
    from config import get_rating
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
        "total_debt_m":      _NA,
        "wtd_avg_rate":      _NA,
        "nearest_maturity":  _NA,
        "debt_source":       _NA,
    }

    # ── Step 0: Live ERP from Damodaran ──────────────────────────────────────
    try:
        from erp_fetcher import get_erp
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
        periods = profile.periods
        sg = _sector_group(profile.sector, getattr(profile, 'ticker', ''))
        flags = []

        pe_hist  = {}
        pb_hist  = {}
        pe_curr  = {}
        pb_curr  = {}
        ptbv     = {}
        ev_sales = {}
        ev_ebitda = {}

        market_cap = profile.market_cap

        for i, p in enumerate(periods):
            rev    = inc.revenue[i]
            ni     = inc.net_income[i]
            oi     = inc.operating_income[i]
            shares = inc.diluted_shares[i]
            # Fallback 1: derive shares from market cap / current price when the
            # XBRL filing does not tag a diluted share count (e.g. OXY).
            if (not shares or shares == 0) and current_price and market_cap and current_price > 0:
                shares = market_cap / current_price
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
            eq     = bal.equity[i]
            pref   = bal.preferred_stock[i]
            debt   = bal.total_debt[i]
            ta     = bal.total_assets[i]
            gi     = bal.goodwill_and_intangibles[i]

            eps       = _safe_div(ni, shares) if shares else "N/A"
            common_eq = (eq - pref) if (eq and pref) else eq
            bvps      = _safe_div(common_eq, shares) if shares else "N/A"

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
                if cp and isinstance(eps, float) and eps > 0:
                    pe_curr[p] = _fmt_x(cp / eps)
                elif cp and isinstance(eps, float):
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

            # ── EV/Sales ─────────────────────────────────────────────────
            # EV/Sales is suppressed for financials: revenue includes gross
            # interest income on a multi-trillion asset base, making the ratio
            # economically meaningless and not comparable to non-bank peers.
            if sg == "financials":
                ev_sales[p] = "N/A (financials)"
            elif i == 0 and market_cap:
                rev_denom = rev
                if rev_denom:
                    ev_approx = market_cap + debt if debt else market_cap
                    ev_sales[p] = _fmt_x(_safe_div(ev_approx, rev_denom))
                else:
                    ev_sales[p] = "N/A"
            else:
                ev_sales[p] = "N/A"

            # ── EV/EBITDA (general + energy; suppress for financials) ────
            if sg == "financials":
                ev_ebitda[p] = "N/A (financials)"
            elif i == 0 and market_cap:
                da = profile.cash_flow.depreciation_amortization[i]
                ebitda = (oi + abs(da)) if (oi and da) else (oi if oi else None)
                ev_approx = market_cap + debt if debt else market_cap
                if ebitda and ebitda > 0:
                    ev_ebitda[p] = _fmt_x(_safe_div(ev_approx, ebitda))
                else:
                    ev_ebitda[p] = "N/A"
            else:
                ev_ebitda[p] = "N/A"

        # ── Premium valuation flag (most recent period only) ────────────────
        mr_p = periods[0] if periods else None
        if mr_p:
            ev_eb_str = ev_ebitda.get(mr_p, "")
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
            "ev_sales":       ev_sales,
            "ev_ebitda":      ev_ebitda,
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
    from erp_fetcher import get_erp
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
    from config import get_rating
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
                filing_date: str = None) -> dict:
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

        return {
            "available":    True,
            "source":       source,
            "filing_date":  filing_date or "N/A",
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
    """Parse '2.34x' -> 2.34"""
    try:
        return float(str(s).replace("x", "").strip())
    except Exception:
        return None