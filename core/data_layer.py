"""
data_layer.py — Financial data pipeline with corrected XBRL concept mappings.

Concept tags are sourced from edgartools' bundled gaap_mappings.json (5.36.0),
which maps 2,924 raw XBRL concepts to 235 standard_tags across 10,000+ real
company filings. Every _get_vector() call now uses the correct standard_tag
string as it actually appears in the df['standard_concept'] column.

Classes:
    RobustDataProcessor          — SEC EDGAR ingestion with amendment filtering
    StatementProfile             — base vector extractor
    IncomeStatementProfile       — IS line items
    BalanceSheetProfile          — BS line items
    CashFlowProfile              — CF line items
    CompanyFinancialProfile      — ratio engine + period alignment
"""

import numpy as np
import pandas as pd
import yfinance as yf
import edgar

from data.filing_cache import FilingCache

edgar.set_identity("DataTester Corp research_pipeline@datatester.com")


# ─────────────────────────────────────────────────────────────────────────────
# RobustDataProcessor  (unchanged from original — ingestion logic was correct)
# ─────────────────────────────────────────────────────────────────────────────

class RobustDataProcessor:
    def __init__(self, ticker, sector="General", cache: FilingCache | None = None):
        self.ticker     = ticker.upper()
        self.sector     = sector
        self.financials = {}
        self.market_cap = 0.0
        self._cache     = cache

    @staticmethod
    def _get_primary_concept_line(df, column_range=6):
        if df is None or df.empty:
            return None
        return df[~df['standard_concept'].isnull()].iloc[:, :column_range]

    def _extract_statement(self, statements_obj, attribute_name):
        try:
            target = getattr(statements_obj, attribute_name, None)
            if target is not None:
                if callable(target):
                    target = target()
                return target.to_dataframe()
        except Exception as e:
            print(f"[{self.ticker}] Note: '{attribute_name}' failed to parse ({e}).")
        return None

    def load_data(self):
        print(f"[{self.ticker}] Ingesting financial filings...")

        # ── Filing date check + cache lookup ──────────────────────────────────
        # Always fetch filing metadata first (lightweight index call, no XBRL parse).
        # Only skip the expensive xbrl() call if cached filing_date matches live.
        filing    = None
        xbrl_data = None

        try:
            company = edgar.Company(self.ticker)
            try:
                filing = company.latest("10-K")
            except Exception:
                filing = company.latest("20-F")

            if "A" in filing.form or "/" in filing.form:
                raise ValueError(
                    f"Retrieved an amendment ({filing.form}) instead of base annual report."
                )

            live_date = str(filing.filing_date)

            if self._cache and self._cache.is_filing_current(self.ticker, live_date):
                hit, cached_financials, meta = self._cache.get_financials(self.ticker)
                if hit:
                    self.financials = cached_financials
                    try:
                        self.market_cap = yf.Ticker(self.ticker).info.get('marketCap', 0.0)
                    except Exception:
                        self.market_cap = 0.0
                    print(f"[{self.ticker}] Cache hit: {meta['form_type']} ({meta['filing_date']})")
                    return True
                # Parquet files missing — fall through to xbrl() below

            # New filing or cold cache — parse full XBRL
            xbrl_data = filing.xbrl()

        except Exception as e:
            print(f"⚠️  Primary method bypassed for {self.ticker} ({e}). Triggering fallback...")
            try:
                company = edgar.Company(self.ticker)
                filings = company.get_filings(form=["10-K", "20-F"], amendments=False)
                if not filings or len(filings) == 0:
                    print(f"[{self.ticker}] Fatal: No valid non-amended filings found.")
                    return False
                for f in filings:
                    if f.form in ["10-K", "20-F"]:
                        filing = f
                        break
                if filing is None or filing.form in ["10-K/A", "20-F/A"]:
                    return False
                print(f"Found clean filing: {filing.form} dated {filing.filing_date}")
                xbrl_data = filing.xbrl()
            except Exception as fallback_err:
                print(f"[{self.ticker}] Fallback aborted: {fallback_err}")
                return False

        # ── EDGAR fallback: cache miss with xbrl_data still None ──────────────
        if xbrl_data is None:
            return False

        try:
            statements = xbrl_data.statements
            raw_dfs = {
                "income_statement": self._extract_statement(statements, "income_statement"),
                "balance_sheet":    self._extract_statement(statements, "balance_sheet"),
                "cash_flow":        self._extract_statement(statements, "cashflow_statement"),
            }
            if raw_dfs["income_statement"] is None and raw_dfs["balance_sheet"] is None:
                print(f"[{self.ticker}] Fatal: core tables failed parsing.")
                return False

            self.financials = {
                "income_statement": self._get_primary_concept_line(raw_dfs["income_statement"], 6),
                "balance_sheet":    self._get_primary_concept_line(raw_dfs["balance_sheet"], 5),
                "cash_flow":        self._get_primary_concept_line(raw_dfs["cash_flow"], 6),
            }
        except Exception as e:
            print(f"[{self.ticker}] Table alignment error: {e}")
            return False

        try:
            self.market_cap = yf.Ticker(self.ticker).info.get('marketCap', 0.0)
        except Exception:
            self.market_cap = 0.0

        # ── Store to cache ────────────────────────────────────────────────────
        if self._cache:
            self._cache.store_financials(
                self.ticker,
                str(filing.filing_date),
                filing.form,
                self.financials,
            )

        print(f"[{self.ticker}] Ingestion verified: {filing.form} ({filing.filing_date})")
        return True

# ─────────────────────────────────────────────────────────────────────────────
# StatementProfile base class
#
# _get_vector() searches df['standard_concept'] for the given standard_tag
# strings in priority order. The standard_tag values here are taken directly
# from edgartools gaap_mappings.json and match exactly what appears in the
# dataframe column — no prefix, no transformation.
# ─────────────────────────────────────────────────────────────────────────────

class StatementProfile:
    def __init__(self, df: pd.DataFrame, periods: list, is_bs: bool = False):
        self._df = df
        self.periods = periods
        self.is_bs = is_bs

    def _get_vector(self, standard_tags: list) -> np.ndarray:
        """
        Search df['standard_concept'] for the first matching standard_tag
        (in priority order). Returns a zero array if none found.

        standard_tags: list of standard_tag strings as stored in the column,
        e.g. ['Revenue', 'GrossProfit']. Tried in order; first non-empty match wins.
        """
        if self._df is None or self._df.empty or not self.periods:
            return np.zeros(len(self.periods))

        row = None
        for tag in standard_tags:
            matched = self._df[self._df['standard_concept'] == tag]
            if not matched.empty:
                row = matched.iloc[0]
                break

        if row is None:
            return np.zeros(len(self.periods))

        vals = []
        for p in self.periods:
            dt = p[:10]   # "2025-09-27"
            val = 0.0
            if p in row.index and pd.notnull(row[p]):
                try:
                    val = float(row[p])
                except (ValueError, TypeError):
                    pass
            else:
                # Fuzzy match on date string (handles timestamp column names)
                for col in row.index:
                    if dt in str(col) and pd.notnull(row[col]):
                        try:
                            val = float(row[col])
                            break
                        except (ValueError, TypeError):
                            continue
            vals.append(val)
        return np.array(vals)

    def _get_vector_by_raw_concept(self, raw_concepts: list) -> np.ndarray:
        """
        Search df['concept'] for company-specific extension tags that edgartools
        does not map to any standard_concept (e.g. 'tsla:DepreciationAmortizationAndImpairment').

        Tries each concept name and common format variants (colon → underscore, no namespace).
        Falls back to zeros if nothing resolves — safe to call after _get_vector fails.
        """
        if self._df is None or self._df.empty or not self.periods:
            return np.zeros(len(self.periods))
        if 'concept' not in self._df.columns:
            return np.zeros(len(self.periods))

        row = None
        for concept in raw_concepts:
            # Try exact match, namespace-underscore variant, and bare local name
            variants = {
                concept,
                concept.replace(':', '_'),
                concept.split(':')[-1],
            }
            matched = self._df[self._df['concept'].isin(variants)]
            if not matched.empty:
                row = matched.iloc[0]
                break

        if row is None:
            return np.zeros(len(self.periods))

        vals = []
        for p in self.periods:
            dt = p[:10]
            val = 0.0
            if p in row.index and pd.notnull(row[p]):
                try:
                    val = float(row[p])
                except (ValueError, TypeError):
                    pass
            else:
                for col in row.index:
                    if dt in str(col) and pd.notnull(row[col]):
                        try:
                            val = float(row[col])
                            break
                        except (ValueError, TypeError):
                            continue
            vals.append(val)
        return np.array(vals)

    def _get_sum_vector(self, standard_tags: list) -> np.ndarray:
        """
        Sum ALL rows whose standard_concept matches any tag in the list.
        Use when edgartools maps multiple debt tranches (e.g. UnsecuredDebt,
        SeniorNotes) to the same standard_concept and the correct value is the
        total across all rows, not a single representative row.
        Example: DLR LongTermDebt = UnsecuredDebt ($440M) + SeniorNotes ($16.2B).
        _get_vector takes iloc[0] ($440M); _get_max_vector takes max ($16.2B);
        _get_sum_vector returns $16.6B — the correct consolidated figure.
        """
        if self._df is None or self._df.empty or not self.periods:
            return np.zeros(len(self.periods))
        mask = self._df['standard_concept'].isin(standard_tags)
        matched = self._df[mask]
        if matched.empty:
            return np.zeros(len(self.periods))
        result = np.zeros(len(self.periods))
        for _, row in matched.iterrows():
            for j, p in enumerate(self.periods):
                val = 0.0
                if p in row.index and pd.notnull(row[p]):
                    try: val = float(row[p])
                    except (ValueError, TypeError): pass
                else:
                    dt = p[:10]
                    for col in row.index:
                        if dt in str(col) and pd.notnull(row[col]):
                            try: val = float(row[col]); break
                            except (ValueError, TypeError): continue
                result[j] += abs(val)  # debt tags are sometimes negative
        return result

    def _get_min_vector(self, standard_tags: list) -> np.ndarray:
        """
        Like _get_max_vector but selects the row with the SMALLEST non-zero
        absolute value among all rows sharing the same standard_concept tag.

        Used for NetInterestIncome: both 'InterestIncomeExpenseNet' (true NII,
        ~$60B for BAC) and 'InterestAndFeeIncomeLoansAndLeases' (gross loan
        interest, ~$138B) are mapped to the NetInterestIncome standard_concept
        by edgartools. True NII is always the smaller figure; gross income lines
        are always larger. Taking the minimum picks the correct net figure.
        """
        if self._df is None or self._df.empty or not self.periods:
            return np.zeros(len(self.periods))

        best_row = None
        best_val = float('inf')

        for tag in standard_tags:
            matched = self._df[self._df['standard_concept'] == tag]
            for _, row in matched.iterrows():
                p0 = self.periods[0]
                dt = p0[:10]
                val = 0.0
                if p0 in row.index and pd.notnull(row[p0]):
                    try:
                        val = abs(float(row[p0]))
                    except (ValueError, TypeError):
                        pass
                else:
                    for col in row.index:
                        if dt in str(col) and pd.notnull(row[col]):
                            try:
                                val = abs(float(row[col]))
                                break
                            except (ValueError, TypeError):
                                continue
                if val > 0 and val < best_val:
                    best_val = val
                    best_row = row

        if best_row is None:
            return np.zeros(len(self.periods))

        vals = []
        for p in self.periods:
            dt = p[:10]
            val = 0.0
            if p in best_row.index and pd.notnull(best_row[p]):
                try:
                    val = float(best_row[p])
                except (ValueError, TypeError):
                    pass
            else:
                for col in best_row.index:
                    if dt in str(col) and pd.notnull(best_row[col]):
                        try:
                            val = float(best_row[col])
                            break
                        except (ValueError, TypeError):
                            continue
            vals.append(val)
        return np.array(vals)

    def _get_max_vector(self, standard_tags: list) -> np.ndarray:
        """
        Like _get_vector but when multiple rows share the same tag (e.g. segment
        subtotals alongside a consolidated total), returns the row whose first
        period value is the largest absolute value — i.e. the consolidated line.
        Used for balance sheet totals like Assets where segment rows appear first.
        """
        if self._df is None or self._df.empty or not self.periods:
            return np.zeros(len(self.periods))

        best_row = None
        best_val = -1.0

        for tag in standard_tags:
            matched = self._df[self._df['standard_concept'] == tag]
            for _, row in matched.iterrows():
                # Use first period column to rank
                p0 = self.periods[0]
                dt = p0[:10]
                val = 0.0
                if p0 in row.index and pd.notnull(row[p0]):
                    try:
                        val = abs(float(row[p0]))
                    except (ValueError, TypeError):
                        pass
                else:
                    for col in row.index:
                        if dt in str(col) and pd.notnull(row[col]):
                            try:
                                val = abs(float(row[col]))
                                break
                            except (ValueError, TypeError):
                                continue
                if val > best_val:
                    best_val = val
                    best_row = row

        if best_row is None:
            return np.zeros(len(self.periods))

        vals = []
        for p in self.periods:
            dt = p[:10]
            val = 0.0
            if p in best_row.index and pd.notnull(best_row[p]):
                try:
                    val = float(best_row[p])
                except (ValueError, TypeError):
                    pass
            else:
                for col in best_row.index:
                    if dt in str(col) and pd.notnull(best_row[col]):
                        try:
                            val = float(best_row[col])
                            break
                        except (ValueError, TypeError):
                            continue
            vals.append(val)
        return np.array(vals)


# ─────────────────────────────────────────────────────────────────────────────
# IncomeStatementProfile, BalanceSheetProfile, CashFlowProfile
#
# Tags sourced from edgartools gaap_mappings.json + S&P 500 concept harvest.
# Priority order within each _get_vector call = real-world company_count desc.
# ─────────────────────────────────────────────────────────────────────────────

# NEW IncomeStatementProfile — replaces existing class
class IncomeStatementProfile(StatementProfile):
    def __init__(self, df: pd.DataFrame, periods: list):
        super().__init__(df, periods, is_bs=False)

    # ── Core P&L ──────────────────────────────────────────────────────────────

    @property
    def revenue(self) -> np.ndarray:
        # Resolution order:
        #
        # _get_max_vector on 'Revenue': some filers (e.g. WELL) map both a
        # segment subtotal ("Resident fees and services") and the consolidated
        # total ("Total revenues") to the same standard_concept.  _get_vector
        # takes iloc[0] which lands on the partial figure.  _get_max_vector
        # picks the largest value = consolidated total.
        #
        # 'Revenues': alternative GAAP label used by some filers.
        #
        # REIT rental income concepts — pure rental REITs (EQR, AVB, etc.)
        # do not tag a generic Revenue line; rental income flows through:
        #   RentalAndLeasingRevenue  ← edgartools standard_concept
        #   us-gaap:OperatingLeaseLeaseIncome  ← raw XBRL (tried via raw lookup)
        #
        # Utility-sector fallbacks: regulated utilities file revenue under
        # sector-specific tags rather than the generic Revenue concept.
        result = self._get_max_vector([
            'Revenue',
            'Revenues',
            'RentalAndLeasingRevenue',          # REITs: rental income (EQR, WELL, AVB)
            'RegulatedAndUnregulatedOperatingRevenue',  # diversified utilities (NEE)
            'ElectricUtilityRevenue',            # pure electric utilities
            'RegulatedUtilityRevenue',
            'PublicUtilitiesRevenueRequirementNet',
            'UtilitiesRevenue',
        ])
        if result.any():
            return result

        # Raw XBRL fallback for REIT lease income not mapped to standard_concept
        return self._get_vector_by_raw_concept([
            'us-gaap:OperatingLeaseLeaseIncome',
            'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax',
        ])

    @property
    def financial_revenue(self) -> np.ndarray:
        """
        Total net revenue for financial institutions: NII + NonInterestIncome.

        Banks report revenue net of interest expense ('revenue net of interest
        expense' in press releases = NII + NonII). The 'Revenue' XBRL tag
        resolves to gross interest income for banks (~$120B for BAC), NOT the
        ~$113B net revenue figure — using it as a denominator produces wrong
        net margins and efficiency ratios.

        If both components are zero (tags not found), returns zeros so callers
        can fall back to N/A rather than using an incorrect gross figure.
        No Revenue fallback.
        """
        nii  = self.net_interest_income        # uses the extended property above
        niri = self._get_vector(['NonInterestIncome'])
        total = nii + niri
        return total

    @property
    def gross_profit(self) -> np.ndarray:
        # GrossProfit (3,629)
        return self._get_vector(['GrossProfit'])

    @property
    def cogs(self) -> np.ndarray:
        # CostOfGoodsAndServicesSold (6,935)
        # CostsSubtotal (192) — service companies that report total costs instead of COGS
        return self._get_vector(['CostOfGoodsAndServicesSold', 'CostsSubtotal'])

    @property
    def total_operating_costs(self) -> np.ndarray:
        """
        Total operating costs (CostsAndExpenses), independent of the COGS
        row. Unlike the `cogs` property, this doesn't fall through a
        priority list keyed off CostOfGoodsAndServicesSold -- _get_vector()
        matches on row *existence*, not on whether the row's values are
        non-null, so `cogs` can silently resolve to zeros (not a real
        CostsSubtotal fallback) whenever a CostOfGoodsAndServicesSold row
        exists but was suppressed upstream (see facts_processor._reconcile's
        COGS plausibility guard). Callers that specifically want the
        CostsSubtotal figure (e.g. freight-broker net-revenue-margin proxy)
        should read this property directly instead of relying on that
        fallback.
        """
        return self._get_vector(['CostsSubtotal'])

    @property
    def operating_income(self) -> np.ndarray:
        """
        Try OperatingIncomeLoss tag first (7,793 companies).
        If zero/missing, back-calculate EBIT from below-the-line items.

        Primary back-calc: EBIT = pretax + abs(interest_expense) - nonop_income
        Fallback back-calc: EBIT ≈ pretax - nonop_income
          Used for cash-rich companies (e.g. LLY, PFE, MRK) whose interest
          expense is zero or negative (interest income). Without this branch the
          condition `interest.any()` fails and operating income silently returns 0
          despite pretax income being available.
        """
        direct = self._get_vector(['OperatingIncomeLoss'])
        if direct.any():
            return direct

        # Back-calculate EBIT from below-the-line items
        pretax  = self._get_vector(['PretaxIncomeLoss'])
        interest = self._get_vector(['InterestExpense'])
        nonop    = self._get_vector(['NonoperatingIncomeExpense'])

        if pretax.any():
            result = pretax.copy().astype(float)
            if interest.any():
                result = result + np.abs(interest)   # add back interest cost
            if nonop.any():
                result = result - nonop              # strip non-operating items
            return result

        # REIT fallback: Total revenues - Total expenses (WELL, confirmed 2025-06-15)
        revenue   = self._get_max_vector(['Revenue', 'Revenues'])
        total_exp = self._get_vector(['CostsSubtotal'])
        if revenue.any() and total_exp.any():
            return revenue - total_exp

        return direct  # zeros

    @property
    def net_income(self) -> np.ndarray:
        # NetIncome (8,872) -> NetIncomeToCommonShareholders (2,013)
        # -> IncomeLossContinuingOperations (952) -> ProfitLoss (5,630, IFRS)
        return self._get_vector([
            'NetIncome', 'NetIncomeToCommonShareholders',
            'IncomeLossContinuingOperations', 'ProfitLoss',
        ])

    @property
    def net_income_continuing(self) -> np.ndarray:
        """
        Net income from continuing operations only — excludes discontinued ops
        gains/losses. Use for REITs and companies with large asset disposals
        (e.g. CCI small-cells sale FY24: NetIncome = -$3.9B due to disposal loss,
        but IncomeLossContinuingOperations = $1.2B reflects the ongoing business).
        Falls back to net_income when no continuing-ops tag is filed.
        """
        result = self._get_vector(['IncomeLossContinuingOperations'])
        if result.any():
            return result
        return self.net_income

    @property
    def pretax_income(self) -> np.ndarray:
        # PretaxIncomeLoss (7,760)
        return self._get_vector(['PretaxIncomeLoss'])

    @property
    def income_tax(self) -> np.ndarray:
        # IncomeTaxes (15,382)
        return self._get_vector(['IncomeTaxes'])

    # ── Below-the-line / non-operating ────────────────────────────────────────

    @property
    def interest_expense(self) -> np.ndarray:
        # InterestExpense (22,087)
        return self._get_vector(['InterestExpense'])

    @property
    def interest_income(self) -> np.ndarray:
        # InterestAndDividendIncome (104) -> InterestIncome (35)
        return self._get_vector(['InterestAndDividendIncome', 'InterestIncome'])

    @property
    def net_interest_income(self) -> np.ndarray:
        """
        Net interest income for banks.

        edgartools maps multiple raw XBRL tags to the NetInterestIncome
        standard_concept, mixing net and gross income lines:
          InterestIncomeExpenseNet          — true NII (~$60B BAC FY25)
          InterestAndDividendIncomeOperating — gross interest income (~$120B)
          InterestAndFeeIncomeLoansAndLeases — gross loan interest (~$138B)

        _get_vector() takes iloc[0]: for BAC/WFC the gross line appears first,
        returning ~$138B instead of $60B. This corrupts net margin and efficiency
        ratio because financial_revenue uses this as its denominator.

        Fix: _get_min_vector picks the smallest non-zero value among all rows
        tagged NetInterestIncome. True NII is always smaller than any gross
        income component, so the minimum is always the correct net figure.

        Fallback: if no NetInterestIncome row exists, derive from gross
        interest income minus interest expense.
        """
        result = self._get_min_vector(['NetInterestIncome'])
        if result.any():
            return result
        interest_income = self._get_vector(['InterestAndDividendIncome', 'InterestIncome'])
        interest_expense = self._get_vector(['InterestExpense'])
        if interest_income.any() and interest_expense.any():
            return interest_income - np.abs(interest_expense)
        return result  # zeros

    @property
    def nonoperating_income(self) -> np.ndarray:
        # NonoperatingIncomeExpense (433) — other income/expense below operating income
        return self._get_vector(['NonoperatingIncomeExpense'])

    @property
    def equity_method_income(self) -> np.ndarray:
        # EquityMethodInvestmentIncome (131)
        return self._get_vector(['EquityMethodInvestmentIncome'])

    @property
    def gain_loss_disposals(self) -> np.ndarray:
        # GainLossOnDispositions (46) — non-recurring asset disposal gains/losses
        return self._get_vector(['GainLossOnDispositions'])

    # ── Non-recurring / special items ─────────────────────────────────────────

    @property
    def impairment_charges(self) -> np.ndarray:
        # AssetImpairmentChargesIS (79) -> GoodwillWriteoffs (22)
        return self._get_vector(['AssetImpairmentChargesIS', 'GoodwillWriteoffs'])

    @property
    def restructuring_charges(self) -> np.ndarray:
        # RestructuringExpenseBenefit (135)
        return self._get_vector(['RestructuringExpenseBenefit'])

    @property
    def discontinued_ops(self) -> np.ndarray:
        # DiscontinuedOperationsIncome (69) — strip for normalised earnings
        return self._get_vector(['DiscontinuedOperationsIncome'])

    # ── Sector-specific ───────────────────────────────────────────────────────

    @property
    def insurance_benefits(self) -> np.ndarray:
        # PolicyBenefitsAndClaims (27) — replaces COGS for insurance companies
        return self._get_vector(['PolicyBenefitsAndClaims'])

    @property
    def minority_interest_expense(self) -> np.ndarray:
        # MinorityInterestIncomeExpense (267) — NCI deducted from consolidated net income
        return self._get_vector(['MinorityInterestIncomeExpense'])

    # ── Expense breakdown ─────────────────────────────────────────────────────

    @property
    def depreciation_amortization(self) -> np.ndarray:
        # DepreciationExpense (7,563) -> AmortizationOfIntangibles (82)
        return self._get_vector(['DepreciationExpense', 'AmortizationOfIntangibles'])

    @property
    def rd_expense(self) -> np.ndarray:
        # ResearchAndDevelopmentExpenses (3,375)
        return self._get_vector(['ResearchAndDevelopmentExpenses'])

    @property
    def sga_expense(self) -> np.ndarray:
        # SellingGeneralAndAdminExpenses (10,222)
        return self._get_vector(['SellingGeneralAndAdminExpenses'])

    @property
    def noninterest_expense(self) -> np.ndarray:
        # NonInterestExpense — bank total operating expenses (non-interest)
        # Fallback chain: bank-specific tag → generic operating expenses → SG&A
        return self._get_vector([
            'NonInterestExpense', 'OperatingExpenses',
            'SellingGeneralAndAdminExpenses',
        ])

    @property
    def stock_comp(self) -> np.ndarray:
        # StockBasedCompensationExpense (5,937)
        return self._get_vector(['StockBasedCompensationExpense'])

    @property
    def preferred_dividends(self) -> np.ndarray:
        # PreferredDividendExpense (60) — needed for EPS-to-common
        return self._get_vector(['PreferredDividendExpense'])

    @property
    def pension_expense(self) -> np.ndarray:
        # PensionExpense (30)
        return self._get_vector(['PensionExpense'])

    # ── Per-share / count ─────────────────────────────────────────────────────

    @property
    def diluted_shares(self) -> np.ndarray:
        # SharesFullyDilutedAverage (11,268) -> SharesAverage (7,035)
        # -> CommonSharesOutstanding (end-of-period proxy; some E&P filers don't
        #    tag a weighted-average diluted count as a separate IS line item)
        # -> SharesOutstanding (bare outstanding count as last resort)
        #
        # _get_max_vector: edgartools maps both basic and diluted weighted-average
        # share counts to the same standard_concept for some filers. _get_vector
        # takes iloc[0], which can land on the basic (smaller) count and inflate
        # EPS and deflate P/E. Diluted >= basic by definition, so max picks the
        # correct diluted figure.
        return self._get_max_vector([
            'SharesFullyDilutedAverage', 'SharesAverage',
            'CommonSharesOutstanding',  'SharesOutstanding',
        ])


# NEW BalanceSheetProfile — replaces existing class
class BalanceSheetProfile(StatementProfile):
    def __init__(self, df: pd.DataFrame, periods: list):
        super().__init__(df, periods, is_bs=True)

    # ── Assets ────────────────────────────────────────────────────────────────

    @property
    def total_assets(self) -> np.ndarray:
        # Assets (10,230)
        # Use _get_max_vector: banks file segment Assets rows before consolidated;
        # taking the largest value ensures we get the consolidated balance sheet total.
        return self._get_max_vector(['Assets'])

    @property
    def current_assets(self) -> np.ndarray:
        # CurrentAssetsTotal (8,421)
        return self._get_vector(['CurrentAssetsTotal'])

    @property
    def cash(self) -> np.ndarray:
        # CashAndMarketableSecurities (12,056) -> CashAndCashEquivalents (51)
        return self._get_vector(['CashAndMarketableSecurities', 'CashAndCashEquivalents'])

    @property
    def short_term_investments(self) -> np.ndarray:
        # ShortTermInvestments (132)
        return self._get_vector(['ShortTermInvestments'])

    @property
    def accounts_receivable(self) -> np.ndarray:
        # TradeReceivables (436) — needed for DSO / working capital
        return self._get_vector(['TradeReceivables'])

    @property
    def inventory(self) -> np.ndarray:
        # Inventories (4,149)
        return self._get_vector(['Inventories'])

    @property
    def ppe_net(self) -> np.ndarray:
        # PlantPropertyEquipmentNet (8,689)
        return self._get_vector(['PlantPropertyEquipmentNet'])

    @property
    def goodwill(self) -> np.ndarray:
        # Goodwill (3,960)
        # Use _get_max_vector: banks (e.g. JPM) file segment-level goodwill rows
        # before the consolidated total — same issue as Assets. Taking the largest
        # value ensures we capture the consolidated balance sheet figure.
        return self._get_max_vector(['Goodwill'])

    @property
    def intangible_assets(self) -> np.ndarray:
        # IntangibleAssets (4,928) -> GoodwillAndIntangiblesNet (264)
        return self._get_vector(['IntangibleAssets', 'GoodwillAndIntangiblesNet'])

    @property
    def goodwill_and_intangibles(self) -> np.ndarray:
        combined = self._get_vector(['GoodwillAndIntangiblesNet'])
        return np.where(combined != 0, combined, self.goodwill + self.intangible_assets)

    @property
    def accumulated_depreciation(self) -> np.ndarray:
        """
        Accumulated PP&E depreciation.

        Primary: standard XBRL tags (most filers).
        Fallback: parse from the label text of PlantPropertyEquipmentNet for filers
        (e.g. MSFT) that embed it as "net of accumulated depreciation of $X and $Y"
        instead of tagging it separately.  Values are in the same unit (raw dollars)
        as all other balance sheet properties.
        """
        result = self._get_max_vector([
            'AccumulatedDepreciation',
            'AccumulatedDepreciationAndAmortization',
            'AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment',
        ])
        if result.any():
            return result

        # Raw concept fallback: PCG uses combined PPE+finance-lease tag mapped to
        # PlantPropertyEquipmentNet by edgartools — invisible to _get_max_vector.
        # Filed as negative; abs() applied so BS delta stays positive. 2025-06-15.
        raw = self._get_vector_by_raw_concept([
            'us-gaap:PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAccumulatedDepreciationAndAmortization',
        ])
        if raw.any():
            return np.abs(raw)

        # Label-text fallback
        if self._df is None or self._df.empty:
            return np.zeros(len(self.periods))
        ppe_rows = self._df[self._df['standard_concept'] == 'PlantPropertyEquipmentNet']
        if ppe_rows.empty:
            return np.zeros(len(self.periods))

        import re
        label = str(ppe_rows.iloc[0].get('label', '') or '')
        # Match e.g. "$93,653" or "$76,421" — label amounts are in millions
        raw_amounts = re.findall(r'\$(\d{1,3}(?:,\d{3})*)', label)
        if not raw_amounts:
            return np.zeros(len(self.periods))

        vals = []
        for amt in raw_amounts[:len(self.periods)]:
            try:
                vals.append(float(amt.replace(',', '')) * 1_000_000)
            except ValueError:
                vals.append(0.0)
        while len(vals) < len(self.periods):
            vals.append(0.0)
        return np.array(vals[:len(self.periods)])

    @property
    def operating_lease_rou(self) -> np.ndarray:
        # OperatingLeaseRightOfUseAsset (231) — right-of-use asset
        return self._get_vector(['OperatingLeaseRightOfUseAsset'])

    @property
    def long_term_investments(self) -> np.ndarray:
        # LongtermInvestments (113)
        return self._get_vector(['LongtermInvestments'])

    def iea_components(self) -> dict:
        """
        Interest-earning assets breakdown for bank NIM denominator.

        Large US bank 10-Ks do not tag a single consolidated IEA line in XBRL
        balance sheets. IEA only appears in the supplemental rate/volume tables
        filed as part of the MD&A (not machine-readable via standard tags).

        We collect every IEA component that IS tagged and return:
          'total'    : np.ndarray — sum of all found components (zeros where none found)
          'found'    : list[str]  — labels of components that resolved to non-zero
          'missing'  : list[str]  — labels of components that returned zero

        The caller (FundamentalAgent) uses 'found'/'missing' to annotate the NIM
        display with what was included and flag what wasn't, so readers know the
        denominator is a partial count, not a certified IEA figure.

        Component map (tag → label shown in PDF):
          Direct total:
            InterestEarningAssets / TotalInterestEarningAssets → "IEA (direct)"
          Components:
            LoansAndLeasesReceivableNetOfAllowance / LoansNet → "Net loans"
            AvailableForSaleSecurities                        → "AFS securities"
            HeldToMaturitySecurities                          → "HTM securities"
            InterestBearingDepositsInBanks                    → "CB deposits"
            FederalFundsSoldAndSecuritiesPurchasedUnderAgreementsToResell → "Fed funds sold / repo"
            TradingAssets                                     → "Trading assets"
        """
        n = len(self.periods)
        zero = np.zeros(n)

        # ── Try direct total first ────────────────────────────────────────────
        direct = self._get_vector(['InterestEarningAssets', 'TotalInterestEarningAssets'])
        if direct.any():
            return {
                'total':   direct,
                'found':   ['IEA (direct tag)'],
                'missing': [],
            }

        # ── Collect individual components ─────────────────────────────────────
        components = [
            ('Net loans',            self._get_vector(['LoansAndLeasesReceivableNetOfAllowance', 'LoansNet'])),
            ('AFS securities',       self._get_vector(['AvailableForSaleSecurities'])),
            ('HTM securities',       self._get_vector(['HeldToMaturitySecurities'])),
            ('CB deposits',          self._get_vector(['InterestBearingDepositsInBanks'])),
            ('Fed funds sold/repo',  self._get_vector(['FederalFundsSoldAndSecuritiesPurchasedUnderAgreementsToResell'])),
            ('Trading assets',       self._get_vector(['TradingAssets'])),
        ]

        found   = []
        missing = []
        total   = np.zeros(n)

        for label, vec in components:
            if vec.any():
                total += vec
                found.append(label)
            else:
                missing.append(label)

        return {
            'total':   total,
            'found':   found,
            'missing': missing,
        }

    @property
    def non_current_assets(self) -> np.ndarray:
        # NonCurrentAssetsTotal (24)
        return self._get_vector(['NonCurrentAssetsTotal'])

    # ── Liabilities ───────────────────────────────────────────────────────────

    @property
    def current_liabilities(self) -> np.ndarray:
        # CurrentLiabilitiesTotal (9,058)
        return self._get_vector(['CurrentLiabilitiesTotal'])

    @property
    def accounts_payable(self) -> np.ndarray:
        # TradePayables (420) — needed for DPO / working capital
        return self._get_vector(['TradePayables'])

    @property
    def short_term_debt(self) -> np.ndarray:
        # ShortTermDebt (7,813) -> CurrentPortionOfLongTermDebt (213)
        return self._get_vector(['ShortTermDebt', 'CurrentPortionOfLongTermDebt'])

    @property
    def long_term_debt(self) -> np.ndarray:
        # LongTermDebt (8,366)
        # _get_sum_vector: some REITs (e.g. DLR) map multiple debt tranches
        # (UnsecuredDebt, SeniorNotes) to the same LongTermDebt standard_concept.
        # _get_vector takes iloc[0] (smallest tranche); _get_sum_vector aggregates
        # all tranches for the correct consolidated figure. Confirmed DLR 2025-06-15.
        result = self._get_sum_vector(['LongTermDebt'])
        if result.any():
            return result
        # Raw concept fallback: SecuredDebt maps to Liabilities standard_concept
        # for some REIT filers (DLR), making it invisible to standard tag lookups.
        secured = self._get_vector_by_raw_concept(['us-gaap:SecuredDebt'])
        return secured

    @property
    def total_debt(self) -> np.ndarray:
        # Sum all debt components: LTD (all tranches) + STD + SecuredDebt
        # SecuredDebt is included via long_term_debt raw fallback when not
        # captured under LongTermDebt standard_concept (e.g. DLR).
        return self.long_term_debt + self.short_term_debt

    @property
    def operating_lease_liability(self) -> np.ndarray:
        # OperatingLeaseNonCurrentDebtEquivalent (209) — for adjusted net debt
        return self._get_vector(['OperatingLeaseNonCurrentDebtEquivalent'])

    @property
    def total_liabilities(self) -> np.ndarray:
        # Liabilities (364) — direct tag; fallback: total_assets - equity
        direct = self._get_vector(['Liabilities'])
        return np.where(
            direct != 0, direct,
            self.total_assets - self.equity
        )

    @property
    def non_current_liabilities(self) -> np.ndarray:
        # NonCurrentLiabilitiesTotal (58)
        return self._get_vector(['NonCurrentLiabilitiesTotal'])

    # ── Equity ────────────────────────────────────────────────────────────────

    @property
    def equity(self) -> np.ndarray:
        # AllEquityBalance (8,569) -> CommonEquity (498)
        # -> AllEquityBalanceIncludingMinorityInterest (3,697)
        # Use _get_max_vector: some filers (e.g. UNH) tag par value or a small
        # equity component BEFORE the consolidated total, so _get_vector picks
        # up ~$9M par value instead of ~$56B total equity.  _get_max_vector
        # picks the largest row — always the consolidated figure.
        # Note: for companies with negative equity (MCD, HD) the largest value
        # is the least-negative component; this is acceptable since negative
        # equity companies are flagged separately in the risk section.
        return self._get_max_vector([
            'AllEquityBalance', 'CommonEquity',
            'AllEquityBalanceIncludingMinorityInterest',
        ])

    @property
    def preferred_stock(self) -> np.ndarray:
        # PreferredStock (5,084)
        return self._get_vector(['PreferredStock'])

    @property
    def retained_earnings(self) -> np.ndarray:
        # RetainedEarnings (8,502)
        return self._get_vector(['RetainedEarnings'])

    @property
    def additional_paid_in_capital(self) -> np.ndarray:
        # AdditionalPaidInCapital (443)
        return self._get_vector(['AdditionalPaidInCapital'])

    @property
    def treasury_stock(self) -> np.ndarray:
        # TreasuryShares (132)
        return self._get_vector(['TreasuryShares'])

    @property
    def aoci(self) -> np.ndarray:
        # AccumulatedOtherComprehensiveIncome (487)
        return self._get_vector(['AccumulatedOtherComprehensiveIncome'])

    @property
    def minority_interest(self) -> np.ndarray:
        # MinorityInterestBalance (276)
        return self._get_vector(['MinorityInterestBalance'])

    # ── Derived ───────────────────────────────────────────────────────────────

    @property
    def net_cash(self) -> np.ndarray:
        """Net cash = Cash + Short-term investments - Total debt"""
        return self.cash + self.short_term_investments - self.total_debt

    @property
    def tangible_book_value(self) -> np.ndarray:
        """TBV = Equity - Goodwill - Intangibles"""
        return self.equity - self.goodwill_and_intangibles

    @property
    def working_capital(self) -> np.ndarray:
        """Working capital = Current assets - Current liabilities"""
        return self.current_assets - self.current_liabilities

    # ── Regulatory capital (banks — Basel III) ────────────────────────────────
    # These XBRL tags are reported as ratios (e.g. 0.153 = 15.3%), not dollar values.
    # Most large US bank 10-Ks tag these directly; smaller banks may omit them.
    # If a tag is missing, the vector returns zeros — callers check for zero.

    @property
    def cet1_ratio(self) -> np.ndarray:
        # CommonEquityTierOneCapitalRatio — CET1 / RWA
        # Primary Basel III capital adequacy metric; minimum 4.5% + 2.5% buffer = 7%
        return self._get_vector(['CommonEquityTierOneCapitalRatio'])

    @property
    def tier1_capital_ratio(self) -> np.ndarray:
        # TierOneCapitalRatio — Tier 1 capital / RWA; minimum 6% (+2.5% buffer = 8.5%)
        return self._get_vector(['TierOneCapitalRatio'])

    @property
    def total_capital_ratio(self) -> np.ndarray:
        # TotalCapitalRatioRiskBased — Total capital / RWA; minimum 8% (+2.5% = 10.5%)
        return self._get_vector(['TotalCapitalRatioRiskBased'])

    @property
    def tier1_leverage_ratio(self) -> np.ndarray:
        # TierOneLeverageRatio — Tier 1 capital / average total assets; minimum 3-4%
        # Falls back to proxy: (equity - goodwill) / total_assets if XBRL tag missing
        direct = self._get_vector(['TierOneLeverageRatio'])
        if direct.any():
            return direct
        ta = self.total_assets
        return np.where(ta != 0, (self.equity - self.goodwill_and_intangibles) / ta, 0.0)


# NEW CashFlowProfile — replaces existing class
class CashFlowProfile(StatementProfile):
    def __init__(self, df: pd.DataFrame, periods: list):
        super().__init__(df, periods, is_bs=False)

    # ── Operating ─────────────────────────────────────────────────────────────

    @property
    def operating_cash_flow(self) -> np.ndarray:
        # NetCashFromOperatingActivities (44,563)
        # Use _get_max_vector: companies with discontinued operations (e.g. OXY)
        # file a continuing-operations subtotal BEFORE the consolidated total —
        # _get_vector would pick the subtotal; _get_max_vector picks the largest
        # value, which is always the consolidated figure.
        return self._get_max_vector(['NetCashFromOperatingActivities'])

    @property
    def depreciation_amortization(self) -> np.ndarray:
        # Tag resolution order (edgartools standard_concept name → filer coverage):
        #   DepreciationAmortizationCF          — FFIEC/bank CF filers
        #   DepreciationExpense                 — general IS/CF line
        #   DepreciationAndAmortization         — common CF add-back label
        #   DepreciationDepletionAndAmortization— energy/mining filers
        #   DepreciationAmortizationAndAccretionNet — tech filers (MSFT, GOOGL)
        #   OtherDepreciationAndAmortization    — catch-all variant
        #   Depreciation                        — bare depreciation-only filers
        # _get_max_vector picks the largest-valued matching row, which is the
        # consolidated D&A figure when sub-categories are also tagged.
        result = self._get_max_vector([
            'DepreciationAmortizationCF',
            'DepreciationExpense',
            'DepreciationAndAmortization',
            'DepreciationDepletionAndAmortization',
            'DepreciationAmortizationAndAccretionNet',
            'OtherDepreciationAndAmortization',
            'Depreciation',
        ])
        if result.any():
            return result

        # Raw-concept fallback for company-specific extension tags that edgartools
        # does not map to any standard_concept.
        # tsla:DepreciationAmortizationAndImpairment — Tesla's custom XBRL tag;
        # confirmed via SEC EDGAR interactive viewer (not in US GAAP taxonomy).
        return self._get_vector_by_raw_concept([
            'tsla:DepreciationAmortizationAndImpairment',
        ])

    @property
    def stock_comp_cf(self) -> np.ndarray:
        # StockBasedCompensationExpense (404 in CF) — non-cash add-back
        return self._get_vector(['StockBasedCompensationExpense'])

    @property
    def change_in_working_capital(self) -> np.ndarray:
        # ChangeInOtherWorkingCapital (297) — combined WC change
        # Falls back to ChangeInReceivables or ChangeInPayables if available
        return self._get_vector([
            'ChangeInOtherWorkingCapital', 'ChangeInReceivables', 'ChangeInPayables',
        ])

    # ── Investing ─────────────────────────────────────────────────────────────

    @property
    def capital_expenditures(self) -> np.ndarray:
        # CapitalExpenses (8,467) — primary tag used by most filers
        # PropertyPlantAndEquipmentAdditions — alternative filed by some E&P companies
        #   where CapitalExpenses resolves to a broader investing-activity subtotal
        #   rather than the pure property/plant additions line.
        # NOTE: if FCF is still understated after this change, run diagnose.py on
        #   the ticker's cash flow statement to identify the exact XBRL concept used
        #   and add it here. Root cause: E&P filers sometimes embed segment-level
        #   capex lines that sum to more than the consolidated net capex definition.
        return self._get_vector([
            'CapitalExpenses',
            'PropertyPlantAndEquipmentAdditions',
        ])

    @property
    def interest_paid(self) -> np.ndarray:
        """
        Cash paid for interest, sourced from the CF statement.

        Used as fallback for companies (e.g. NEE, regulated utilities) that do not
        tag interest expense as a separate income statement line — they bury it inside
        a combined NonoperatingIncomeExpense total.  The CF 'InterestExpense' concept
        maps to the supplemental cash-paid-for-interest disclosure, which may be net
        of amounts capitalized (common in utilities with large construction programmes).
        """
        return self._get_vector(['InterestExpense'])

    @property
    def investing_cash_flow(self) -> np.ndarray:
        # NetCashFromInvestingActivities (501)
        return self._get_vector(['NetCashFromInvestingActivities'])

    @property
    def acquisitions(self) -> np.ndarray:
        # AcquisitionsNet (309) — M&A spend net of cash acquired
        return self._get_vector(['AcquisitionsNet'])

    @property
    def asset_sale_proceeds(self) -> np.ndarray:
        # ProceedsFromSaleOfPPE (154)
        return self._get_vector(['ProceedsFromSaleOfPPE'])

    # ── Financing ─────────────────────────────────────────────────────────────

    @property
    def financing_cash_flow(self) -> np.ndarray:
        # NetCashFromFinancingActivities (501)
        return self._get_vector(['NetCashFromFinancingActivities'])

    @property
    def debt_repayments(self) -> np.ndarray:
        # DebtRepayments (431)
        return self._get_vector(['DebtRepayments'])

    @property
    def debt_proceeds(self) -> np.ndarray:
        # DebtProceeds (381)
        return self._get_vector(['DebtProceeds'])

    @property
    def share_buybacks(self) -> np.ndarray:
        # EquityExpenseIncome(BuybackIssued) (446) -> StockIssuanceProceeds (253)
        # Note: buybacks negative, issuances positive — net equity activity
        return self._get_vector([
            'EquityExpenseIncome(BuybackIssued)', 'StockIssuanceProceeds',
        ])

    # ── Totals ────────────────────────────────────────────────────────────────

    @property
    def net_change_in_cash(self) -> np.ndarray:
        # NetChangeInCash (498)
        return self._get_vector(['NetChangeInCash'])

    # ── Derived ───────────────────────────────────────────────────────────────

    @property
    def free_cash_flow(self) -> np.ndarray:
        """FCF = Operating CF - |CapEx|"""
        return self.operating_cash_flow - np.abs(self.capital_expenditures)

    @property
    def fcf_ex_acquisitions(self) -> np.ndarray:
        """FCF ex-M&A = FCF - |Acquisitions|"""
        return self.free_cash_flow - np.abs(self.acquisitions)



# ─────────────────────────────────────────────────────────────────────────────
# CompanyFinancialProfile  (ratio engine — keeps original interface)
# ─────────────────────────────────────────────────────────────────────────────

_META_COLS = {
    'concept', 'label', 'standard_concept', 'unit', 'balance', 'weight',
    'preferred_sign', 'parent_concept', 'parent_abstract_concept',
}

class CompanyFinancialProfile:
    """
    Assembles statement profiles and exposes the ratio engine.
    Interface is backward-compatible with the existing orchestrator and agents.
    """

    def __init__(self, ticker: str, sector: str, market_cap: float,
                 financials_payload: dict):
        self.ticker     = ticker.upper()
        self.sector     = sector
        self.market_cap = market_cap

        is_df = financials_payload.get("income_statement")
        if is_df is not None and not is_df.empty:
            date_cols = [c for c in is_df.columns if c not in _META_COLS]
            # self.periods = date_cols[:3] if len(date_cols) >= 3 else date_cols  # ← old: capped at 3 yrs
            self.periods = date_cols  # facts_processor provides up to max_years (default 5)
        else:
            self.periods = []

        self.income_statement = IncomeStatementProfile(
            financials_payload.get("income_statement"), self.periods
        )
        self.balance_sheet = BalanceSheetProfile(
            financials_payload.get("balance_sheet"), self.periods
        )
        self.cash_flow = CashFlowProfile(
            financials_payload.get("cash_flow"), self.periods
        )

    def get_comprehensive_cfa_ratios(self, historical_prices: list = None) -> dict:
        """
        Legacy ratio engine. Returns the same dict structure as the original
        so existing callers are unaffected.
        """
        if not self.periods:
            return {}

        inc = self.income_statement
        bal = self.balance_sheet

        ratios = {
            'Profitability': {'Gross Margin': {}, 'Net Margin': {}, 'ROE': {}, 'ROA': {}},
            'Liquidity':     {'Current Ratio': {}, 'Quick Ratio': {}},
            'Solvency':      {'Debt-to-Equity': {}, 'Interest Coverage': {}},
            'Activity':      {'Asset Turnover': {}, 'Inventory Turnover': {}},
            'Valuation':     {'P/E': {}, 'P/B': {}, 'P/TBV': {}},
        }

        for i, y in enumerate(self.periods):
            rev   = inc.revenue[i]
            gp    = inc.gross_profit[i]
            cogs  = inc.cogs[i]
            ni    = inc.net_income[i]
            oi    = inc.operating_income[i]
            ie    = inc.interest_expense[i]
            ta    = bal.total_assets[i]
            eq    = bal.equity[i]
            ca    = bal.current_assets[i]
            cl    = bal.current_liabilities[i]
            inv   = bal.inventory[i]
            debt  = bal.total_debt[i]
            pref  = bal.preferred_stock[i]
            tbv   = bal.tangible_book_value[i]
            shares = inc.diluted_shares[i]

            # Gross profit fallback
            if gp == 0 and cogs != 0 and rev != 0:
                gp = rev - cogs

            # ── Profitability ──
            if rev:
                ratios['Profitability']['Gross Margin'][y] = (
                    f"{(gp / rev) * 100:.2f}%" if gp else "N/A (COGS missing)"
                )
                ratios['Profitability']['Net Margin'][y]   = f"{(ni / rev) * 100:.2f}%"
                ratios['Activity']['Asset Turnover'][y]    = (
                    f"{rev / ta:.2f}x" if ta else "N/A"
                )

            if eq > 0:
                ratios['Profitability']['ROE'][y]     = f"{(ni / eq) * 100:.2f}%"
                ratios['Solvency']['Debt-to-Equity'][y] = f"{debt / eq:.2f}x"
            else:
                ratios['Profitability']['ROE'][y]       = "N/A (Negative Equity)"
                ratios['Solvency']['Debt-to-Equity'][y] = "N/A (Negative Equity)"

            if ta:
                ratios['Profitability']['ROA'][y] = f"{(ni / ta) * 100:.2f}%"

            # ── Liquidity ──
            if cl:
                ratios['Liquidity']['Current Ratio'][y] = f"{ca / cl:.2f}x"
                ratios['Liquidity']['Quick Ratio'][y]   = (
                    f"{(ca - inv) / cl:.2f}x"
                )

            # ── Solvency ──
            if ie != 0:
                ratios['Solvency']['Interest Coverage'][y] = (
                    f"{oi / abs(ie):.2f}x"
                )
            else:
                ratios['Solvency']['Interest Coverage'][y] = "No Interest Exp"

            # ── Activity ──
            if inv and cogs:
                ratios['Activity']['Inventory Turnover'][y] = f"{cogs / inv:.2f}x"
            else:
                ratios['Activity']['Inventory Turnover'][y] = "N/A"

            # ── Valuation ──
            eps  = ni / shares if shares else 0
            bvps = (eq - pref) / shares if (shares and eq > pref) else 0

            if historical_prices and i < len(historical_prices) and historical_prices[i]:
                price = historical_prices[i]
                ratios['Valuation']['P/E'][y] = (
                    f"{price / eps:.2f}x" if eps > 0 else "Negative EPS"
                )
                ratios['Valuation']['P/B'][y] = (
                    f"{price / bvps:.2f}x" if bvps > 0 else "N/A"
                )
                if tbv > 0 and shares > 0:
                    ratios['Valuation']['P/TBV'][y] = f"{price / (tbv / shares):.2f}x"
                elif tbv <= 0:
                    ratios['Valuation']['P/TBV'][y] = "N/A (Asset-Light/High Intangibles)"
                else:
                    ratios['Valuation']['P/TBV'][y] = "N/A"
            else:
                ratios['Valuation']['P/E'][y]   = f"EPS: ${eps:.2f}"
                ratios['Valuation']['P/B'][y]   = f"BVPS: ${bvps:.2f}"
                ratios['Valuation']['P/TBV'][y] = "N/A (no price)"

        return ratios