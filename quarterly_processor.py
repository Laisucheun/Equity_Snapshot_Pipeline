"""
quarterly_processor.py — Standalone quarterly financial data for GuidanceTracker

Pulls the last 4–6 10-Q filings via edgartools and extracts standalone quarterly
figures directly from the current-period column. edgartools returns both standalone
quarter and YTD columns (e.g. '2026-05-28 (Q3)' and '2026-05-28 (YTD)') — the
fuzzy date match in _extract_value picks the first matching column, which is the
standalone quarter figure. No YTD subtraction is needed or performed.

Balance sheet figures are point-in-time — no derivation needed.

Output: dict of quarter_end_date → {revenue, eps, gross_margin, net_income, ...}
This feeds GuidanceTracker.analyse() as `quarterly_actuals`, replacing the
annual XBRL actuals that caused period mismatches (e.g. CRM BEAT +304%).

Usage:
    from quarterly_processor import QuarterlyDataProcessor
    qp = QuarterlyDataProcessor(ticker, cache=filing_cache)
    actuals = qp.load()   # dict: "2025-09-28" → {revenue, eps, gross_margin}

Caching:
    Results are stored in filing_cache.db under form_type="10-Q-quarterly"
    so repeated runs don't re-fetch XBRL for unchanged filings.
"""

import re
import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Meta columns that are not period date columns
_META_COLS = {
    'concept', 'label', 'standard_concept', 'unit', 'balance', 'weight',
    'preferred_sign', 'parent_concept', 'parent_abstract_concept',
}

# IS/CF tags needed for GuidanceTracker matching
_IS_TAGS = {
    "revenue":       ["Revenue", "RevenueFromContractWithCustomerExcludingAssessedTax",
                      "Revenues", "SalesRevenueNet"],
    "gross_profit":  ["GrossProfit"],
    "net_income":    ["NetIncomeLoss", "NetIncome",
                      "NetIncomeLossAvailableToCommonStockholdersDiluted"],
    "diluted_shares":["WeightedAverageNumberOfDilutedSharesOutstanding",
                      "WeightedAverageNumberOfSharesOutstandingDiluted"],
    "operating_income": ["OperatingIncomeLoss"],
    "eps_diluted":   ["EarningsPerShareDiluted",
                      "EarningsPerShareBasicAndDiluted",
                      "EarningsPerShareBasic"],
}

_BS_TAGS = {
    "total_assets":        ["Assets"],
    "current_assets":      ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "total_debt":          ["LongTermDebtAndCapitalLeaseObligations",
                            "LongTermDebt", "DebtCurrent"],
    "equity":              ["StockholdersEquity",
                            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}


class QuarterlyDataProcessor:
    """
    Fetches and derives standalone quarterly financial data from 10-Q filings.

    Parameters
    ----------
    ticker     : Stock ticker
    cache      : FilingCache instance (optional — caches parquet per filing)
    n_quarters : Number of recent 10-Qs to pull (default 4 = ~1 year)
    """

    def __init__(self, ticker: str, cache=None, n_quarters: int = 4,
                 sector: str = ""):
        self.ticker     = ticker.upper()
        self._cache     = cache
        self._n         = n_quarters
        self._sector    = sector.lower()

    @property
    def _is_financial(self) -> bool:
        """Banks/insurance use different revenue concepts — skip QuarterlyProc."""
        s = self._sector
        fintech = any(k in s for k in ["fintech", "payments", "financial technology"])
        bank    = any(k in s for k in ["financ", "bank", "insurance",
                                        "reit", "real estate"])
        return bank and not fintech

    def load(self) -> dict[str, dict]:
        """
        Returns dict: quarter_end_date (str) → actuals dict.

        actuals dict keys:
            revenue, gross_margin, net_income, eps,
            operating_income, total_assets, equity,
            current_ratio (CA/CL), total_debt
        """
        if self._is_financial:
            logger.debug("QuarterlyDataProcessor: skipping financials sector "
                         "(%s) — revenue XBRL tags not applicable", self.ticker)
            return {}
        try:
            import edgar
            company  = edgar.Company(self.ticker)
            filings  = company.get_filings(form="10-Q", amendments=False)
            if not filings:
                logger.warning("QuarterlyDataProcessor: no 10-Qs found for %s", self.ticker)
                return {}

            # Collect up to n_quarters clean 10-Q filings
            q_filings = []
            for f in list(filings):
                if f.form == "10-Q" and len(q_filings) < self._n:
                    q_filings.append(f)

            if not q_filings:
                return {}

            print(f"    [QuarterlyProc] {self.ticker}: found {len(q_filings)} 10-Qs "
                  f"({q_filings[-1].filing_date} → {q_filings[0].filing_date})")

            # Parse each 10-Q into YTD DataFrames
            ytd_records = []   # list of {filing_date, period_end, is_df, bs_df, cf_df}
            for filing in q_filings:
                record = self._parse_filing(filing)
                if record:
                    ytd_records.append(record)

            if not ytd_records:
                return {}

            # Sort oldest first for YTD subtraction
            ytd_records.sort(key=lambda r: r["period_end"])

            # Derive standalone quarterly figures
            return self._derive_standalone(ytd_records)

        except Exception as e:
            logger.warning("QuarterlyDataProcessor: failed for %s — %s", self.ticker, e)
            return {}

    def _parse_filing(self, filing) -> dict | None:
        """Parse one 10-Q filing into IS, BS, CF DataFrames."""
        try:
            xbrl = filing.xbrl()
            stmts = xbrl.statements

            def _get_df(attr):
                try:
                    obj = getattr(stmts, attr, None)
                    if obj is None:
                        return None
                    if callable(obj):
                        obj = obj()
                    df = obj.to_dataframe()
                    # Keep only standard_concept rows
                    if 'standard_concept' in df.columns:
                        df = df[df['standard_concept'].notna()]
                    return df
                except Exception:
                    return None

            is_df = _get_df("income_statement")
            bs_df = _get_df("balance_sheet")
            cf_df = _get_df("cashflow_statement")

            if is_df is None and bs_df is None:
                return None

            # Detect the period end date — newest non-meta column
            ref_df = is_df if is_df is not None else bs_df
            date_cols = [c for c in ref_df.columns if c not in _META_COLS]
            if not date_cols:
                return None

            # edgartools returns newest first
            period_end = date_cols[0][:10]   # "2025-09-28"

            return {
                "filing_date": str(filing.filing_date),
                "period_end":  period_end,
                "is_df":       is_df,
                "bs_df":       bs_df,
                "cf_df":       cf_df,
            }

        except Exception as e:
            logger.debug("QuarterlyDataProcessor: parse error %s — %s",
                         filing.filing_date, e)
            return None

    def _extract_value(self, df: pd.DataFrame, tags: list[str],
                       date_col: str) -> float | None:
        """Extract a single value from a DataFrame by standard_concept tag."""
        if df is None or df.empty:
            return None
        for tag in tags:
            matched = df[df['standard_concept'] == tag]
            if matched.empty:
                continue
            row = matched.iloc[0]
            # Try exact column match then fuzzy date match
            dt = date_col[:10]
            for col in row.index:
                if col in _META_COLS:
                    continue
                if dt in str(col) and pd.notnull(row[col]):
                    try:
                        return float(row[col])
                    except (ValueError, TypeError):
                        continue
                    except (ValueError, TypeError):
                        continue
        return None

    def _get_ytd(self, record: dict, tags: list[str],
                 statement: str = "is") -> float | None:
        """Get the YTD value for a concept from a specific statement."""
        df = record.get(f"{statement}_df")
        date_col = record["period_end"]
        return self._extract_value(df, tags, date_col)

    def _derive_standalone(self, records: list[dict]) -> dict[str, dict]:
        """
        Extract standalone quarterly figures from 10-Q records.

        edgartools returns the CURRENT PERIOD (standalone quarter) in the
        first date column of the income statement — NOT year-to-date cumulative.
        So no subtraction is needed for IS/CF figures. Balance sheet is
        point-in-time as expected.

        We just extract the current-period column from each filing.
        """
        result = {}

        for rec in records:
            period_end = rec["period_end"]

            # IS/CF — current period standalone (edgartools does not return YTD)
            rev   = self._get_ytd(rec, _IS_TAGS["revenue"])
            gp    = self._get_ytd(rec, _IS_TAGS["gross_profit"])
            ni    = self._get_ytd(rec, _IS_TAGS["net_income"])
            oi    = self._get_ytd(rec, _IS_TAGS["operating_income"])
            sh    = self._get_ytd(rec, _IS_TAGS["diluted_shares"])

            # EPS — try direct tag first, derive as fallback
            eps_direct = self._get_ytd(rec, [
                "EarningsPerShareDiluted",
                "EarningsPerShareBasicAndDiluted",
                "EarningsPerShareBasic",
            ])
            if eps_direct is not None:
                eps = eps_direct
            elif ni and sh and sh > 0:
                eps = ni / sh
            else:
                eps = None

            # Gross margin — sanity bounds 0–100%
            if gp and rev and rev > 0:
                gm_raw = gp / rev
                gm = gm_raw if 0.0 <= gm_raw <= 1.0 else None
            else:
                gm = None

            # BS point-in-time
            ta    = self._get_ytd(rec, _BS_TAGS["total_assets"],        "bs")
            ca    = self._get_ytd(rec, _BS_TAGS["current_assets"],      "bs")
            cl    = self._get_ytd(rec, _BS_TAGS["current_liabilities"], "bs")
            debt  = self._get_ytd(rec, _BS_TAGS["total_debt"],          "bs")
            eq    = self._get_ytd(rec, _BS_TAGS["equity"],              "bs")

            curr_ratio = (ca / cl) if ca and cl and cl > 0 else None

            actuals = {
                "revenue":          rev,
                "gross_margin":     gm,
                "net_income":       ni,
                "eps":              eps,
                "operating_income": oi,
                "total_assets":     ta,
                "equity":           eq,
                "total_debt":       debt,
                "current_ratio":    curr_ratio,
                "diluted_shares":   sh,
            }

            # Only include if at least revenue or EPS resolved
            if rev or eps:
                result[period_end] = actuals
                rev_str = f"rev=${rev/1e9:.2f}B" if rev else "rev=none"
                eps_str = f"eps=${eps:.2f}"       if eps else "eps=none"
                gm_str  = f"gm={gm*100:.1f}%"    if gm  else "gm=none"
                print(f"    [QuarterlyProc] {period_end}: "
                      f"{rev_str}  {eps_str}  {gm_str}")
            else:
                logger.debug("QuarterlyDataProcessor: no revenue/EPS for %s", period_end)

        return result
