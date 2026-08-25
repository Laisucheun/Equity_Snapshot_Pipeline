# Equity Research Pipeline — Architecture & Operations Guide

## What This Pipeline Does

This pipeline automatically generates institutional-grade equity research PDF reports for US-listed public companies. Given a stock ticker, it pulls financial data from SEC EDGAR, market data from yfinance, earnings transcripts from Motley Fool and EDGAR exhibits, and macroeconomic data from FRED — then runs a series of analytical "agents" to produce fundamentals tables, risk assessments, valuations, management guidance analysis, and trend commentary. The final output is a structured, multi-page PDF brief.

The system also includes a validation harness that batch-tests the pipeline against thousands of tickers, reconciling its XBRL-derived financials against yfinance as a ground truth, and a universe builder that maintains the list of tickers eligible for analysis.

---

## System Architecture (High Level)

The pipeline has four major layers:

```
┌─────────────────────────────────────────────────────────────────┐
│  1. UNIVERSE LAYER                                              │
│     build_sec_universe.py → ticker_universe.py                  │
│     Which companies to analyze, and what sector they belong to  │
├─────────────────────────────────────────────────────────────────┤
│  2. DATA LAYER                                                  │
│     facts_processor.py (primary)                                │
│     + ~15 auxiliary loaders (ownership, insider, short interest, │
│       transcripts, price context, analyst targets, FRED, etc.)  │
│     Raw SEC/market data → standardized financial DataFrames     │
├─────────────────────────────────────────────────────────────────┤
│  3. ANALYSIS LAYER                                              │
│     orchestrator.py → agents.py                                 │
│     FundamentalAgent, RiskAgent, ValuationAgent,                │
│     TrendCommentaryAgent, GuidanceAgent, GuidanceTracker        │
│     DataFrames → analytical conclusions (dicts)                 │
├─────────────────────────────────────────────────────────────────┤
│  4. OUTPUT LAYER                                                │
│     renderer.py → PDF                                           │
│     validate_pipeline.py → SQLite + CSVs (batch QA)             │
│     Agent dicts → formatted equity brief or validation report   │
└─────────────────────────────────────────────────────────────────┘
```

---

## File-by-File Deep Dive

### 1. `build_sec_universe.py` — Universe Construction

**Purpose:** Build and maintain the master list of tickers the pipeline can analyze.

**How it works:**

1. Fetches all SEC-registered tickers from `company_tickers_exchange.json` (~13,000 entries).
2. Filters to US exchanges only (NYSE, NASDAQ, CBOE, BATS, NYSE American, NYSE Arca).
3. Filters out non-alpha tickers (warrants, units, preferred shares with special characters).
4. For each remaining candidate, fetches its SIC code and most recent annual filing form type from the SEC submissions API (`CIK{cik}.json`). This is one HTTP call per ticker, rate-limited to ~8 req/sec, taking roughly 17 minutes for the full universe.
5. Maps SIC codes to the pipeline's internal sector taxonomy (Technology, Healthcare, Financial Services, Real Estate, Energy, Utilities, Materials, Industrials, Consumer Discretionary, Consumer Staples, Communication Services, or "General" as fallback).
6. Applies exclusion filters via `_should_exclude()`:
   - SIC 6726 (crypto trusts, commodity ETFs, closed-end funds)
   - SIC 6770 (SPACs / blank check companies)
   - Warrant/unit/right ticker suffixes (e.g., SEATW, CSHRW, PMTW — regex requires ≥2 letter base + known suffix)
   - Foreign private issuers detected by their annual filing form being 20-F or 40-F
7. Merges new entries (tier 4) with existing manually-classified entries (tiers 1–3: S&P 500, Russell 1000, edge cases). Manual entries always win on sector assignment.
8. Writes `ticker_universe_sec.py` — a Python module containing a `UNIVERSE` list of `TickerEntry(ticker, sector, tier)` dataclasses, plus helper functions (`get_by_sector`, `get_by_tier`, `summary`).

**Key data structure — `_SIC_TO_SECTOR`:** A dict mapping Python `range` objects to sector strings. SIC codes fall into ranges (e.g., 3570–3579 = computer hardware → Technology). The mapping has ~60 range entries covering all major SIC groups. A recent session filled gaps: `range(6553,6700)` → Financial Services (covering mortgage bankers), `range(7380,7390)` → Industrials, `range(8711,8744)` → Industrials (engineering/consulting).

**Output:** `ticker_universe_sec.py` with ~8,000 classified tickers.

---

### 2. `facts_processor.py` — Financial Data Extraction (2,656 lines)

**Purpose:** The core data engine. Replaces an earlier `RobustDataProcessor` that used edgartools' single-filing API (limited to 3 years). This processor queries the SEC Company Facts API directly, returning 5–25 years of annual data in a single HTTP call.

**How it works:**

**Step 1 — CIK Resolution:** Converts a ticker (e.g., "AAPL") to a 10-digit CIK number using SEC's `company_tickers.json`. Cached in `_cik_cache`.

**Step 2 — Facts Fetch:** Hits `data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`, which returns every XBRL fact the company has ever filed. This blob contains all us-gaap (and dei) tagged values across all filings. Result cached in `_facts_cache`.

**Step 3 — Period Discovery:** Scans anchor concepts (Revenues, RevenueFromContractWithCustomer, NetIncomeLoss, Assets) to find all annual period-end dates. Returns the most recent N periods (default 5), sorted newest-first.

**Step 4 — Waterfall Resolution:** This is the heart of the system. For each financial line item (e.g., "Revenue"), the processor tries a prioritized list of raw XBRL concept names until one resolves:

```
Revenue waterfall:
  1. RevenueFromContractWithCustomerExcludingAssessedTax  (most common, ASC 606)
  2. Revenues                                              (older standard)
  3. RevenueFromContractWithCustomerIncludingAssessedTax
  4. Revenue
  ... (20 fallbacks total)
```

The waterfall is defined in three large lists: `_IS_WATERFALL` (income statement, ~60 line items), `_BS_WATERFALL` (balance sheet, ~70 line items), and `_CF_WATERFALL` (cash flow, ~40 line items). Each entry is a tuple: `(standard_label, [raw_concepts_in_priority_order], unit)`.

**Concept extraction (`_extract_annual`):** For each raw concept, this function:
- Filters to 10-K / 10-K/A / 20-F / 20-F/A forms only
- For duration concepts (income statement, cash flow): applies a day-count filter (≥250 days) or accepts `fp='FY'` entries to exclude quarterly data that can appear under annual filings
- For instant concepts (balance sheet): no duration filter needed
- Deduplicates by period-end date, keeping the most recently filed value (amendments supersede originals)

**Phase 1 — Sector & Company Overrides:** Before resolving, the waterfall is augmented:
1. **Company-specific patches** (`_COMPANY_MAPPINGS`): loaded from edgartools' `company_mappings/*.json` files. These handle companies that use proprietary XBRL extension tags (e.g., Tesla, Microsoft).
2. **Sector overrides** (`_SECTOR_OVERRIDES`): built from edgartools' `gaap_mappings.json` industry overrides. For example, Real Estate gets `OperatingLeaseLeaseIncome` prepended before ASC 606 revenue concepts, because REITs report rental income differently. Financial Services gets `InterestIncomeExpenseNet` prepended because banks' "revenue" is net interest income, not ASC 606 contract revenue.
3. **Hard-coded sector overrides**: Real Estate and Financial Services have manually specified concept priority lists that are prepended to ensure correct revenue resolution.

**Step 5 — Reconciliation (`_reconcile`):** Post-hoc quality checks:
- **Bank revenue sum:** For Financial Services, if `FinancialServicesRevenue` (NII) + `NonInterestIncome` > resolved Revenue, overwrites Revenue with the sum. Has special handling for insurers (Premiums + Investment Income) and brokers (Revenues fallback).
- **Commodity cross-check:** If `Revenues` > `Revenue` × 1.5, substitutes Revenues (handles commodity traders like ADM/BG where ASC 606 captures net, not gross).
- **Equity fallback:** If `AllEquityBalance` is 0 but `AllEquityBalanceIncludingMinorityInterest` has a value, copies it.
- **Gross profit back-calc:** If GrossProfit is missing but Revenue and COGS exist, computes Revenue − COGS.
- **Debt sanity check:** Flags when InterestExpense > $1M but LongTermDebt = 0.
- **CF interest alias:** Adds an InterestExpense row to the cash flow DataFrame for compatibility with existing profile classes.

**Output:** `FactsDataProcessor.financials` — a dict with three DataFrames (`income_statement`, `balance_sheet`, `cash_flow`), each having columns `[standard_concept, concept, <period_date_1>, <period_date_2>, ...]`. This feeds into `CompanyFinancialProfile` (see data_layer.py below).

---

### 2b. `data_layer.py` — Data Model & Vector Extraction (1,365 lines)

**Purpose:** Defines the data model that all agents read from. Converts raw DataFrames into typed property accessors with sector-aware fallbacks.

**Class hierarchy:**

```
StatementProfile (base)
  ├── IncomeStatementProfile    — revenue, margins, EPS, sector-specific line items
  ├── BalanceSheetProfile       — assets, liabilities, equity, Basel III ratios
  └── CashFlowProfile          — OCF, capex, FCF, financing activities

CompanyFinancialProfile         — assembles all three profiles + period alignment
```

**`StatementProfile._get_vector(standard_tags)`** — the core extraction method. Searches `df['standard_concept']` for the first matching tag in priority order, then extracts values across all period columns with fuzzy date matching (handles timestamp column names from edgartools). Returns a numpy array of floats, one per fiscal period.

Four vector variants handle edge cases in XBRL data:
- `_get_max_vector` — when multiple rows share the same tag (e.g., segment subtotals + consolidated total), picks the row with the largest absolute value. Used for `Assets`, `equity`, `diluted_shares`, `operating_cash_flow`
- `_get_min_vector` — picks the smallest non-zero value. Used for `net_interest_income` where edgartools maps both true NII (~$60B for BAC) and gross loan interest (~$138B) to the same tag
- `_get_sum_vector` — sums all matching rows. Used for `LongTermDebt` where REITs (e.g., DLR) map multiple tranches (UnsecuredDebt $440M + SeniorNotes $16.2B) to the same tag
- `_get_vector_by_raw_concept` — searches `df['concept']` for company-specific extension tags not mapped by edgartools (e.g., `tsla:DepreciationAmortizationAndImpairment`)

**Key properties with non-trivial resolution logic:**

- `IncomeStatementProfile.revenue` — tries standard tags first, then REIT rental income (`RentalAndLeasingRevenue`), then utility-specific tags (`RegulatedAndUnregulatedOperatingRevenue`), then raw XBRL fallback (`OperatingLeaseLeaseIncome`)
- `IncomeStatementProfile.operating_income` — tries direct tag, then back-calculates EBIT from pretax + interest − nonop income, then REIT fallback (revenue − CostsSubtotal)
- `IncomeStatementProfile.financial_revenue` — bank-specific: NII + NonInterestIncome (never falls back to gross Revenue tag)
- `BalanceSheetProfile.accumulated_depreciation` — tries standard tags, then raw XBRL fallback for PCG-style combined PPE+lease tags, then parses from the label text of the PPE row (handles MSFT embedding depreciation as "net of accumulated depreciation of $X" in the label)
- `BalanceSheetProfile.iea_components()` — builds the NIM denominator for banks by collecting every tagged interest-earning asset component (loans, AFS/HTM securities, CB deposits, Fed funds, trading assets), returning both the total and lists of found/missing components for annotation
- `CashFlowProfile.depreciation_amortization` — tries 7 standard tag variants plus a raw-concept fallback for Tesla's custom XBRL tag

**`CompanyFinancialProfile`** — assembles the three statement profiles from the financials payload dict. Detects period columns by excluding metadata columns (`_META_COLS`). Exposes `profile.periods`, `profile.income_statement`, `profile.balance_sheet`, `profile.cash_flow`, plus a legacy `get_comprehensive_cfa_ratios()` method.

**`RobustDataProcessor`** — the original SEC data loader (pre-`FactsDataProcessor`). Uses edgartools' single-filing API — limited to 3 years per 10-K. Still present in the codebase but commented out in the orchestrator. Its cache integration with `FilingCache` is more complete than `FactsDataProcessor` (which uses in-memory caching only): it checks `is_filing_current()` before parsing XBRL, and stores/loads Parquet files via `FilingCache`.

---

### 2c. `config.py` — Configuration & Static Data

**Purpose:** Centralised API key loading and credit rating lookup.

- Loads `FRED_API_KEY` and `ANTHROPIC_API_KEY` from `.env` via python-dotenv
- `_load_ratings()` reads `ratings.csv` (columns: ticker, sp_rating, moodys_rating, fitch_rating, outlook, as_of_date) into a dict at import time
- `get_rating(ticker)` returns the rating dict for a ticker, logging a warning when missing — this warning is the prompt to add the ticker to `ratings.csv` for credit quality coverage

---

### 2d. `transcript_loader.py` — Motley Fool Transcript Fetcher

**Purpose:** Fetches earnings call transcripts from Motley Fool using a pre-built SQLite index (`fool_transcripts.db`).

**How it works:**

1. `_lookup_url(ticker)` queries `fool_transcripts.db` for the most recent transcript URL
2. `fetch(ticker)` GETs the URL with browser-like headers, then calls `_extract_guidance_text(html, ticker)` to extract structured content
3. **HTML extraction pipeline** in `_extract_guidance_text`:
   - Strips Next.js hydration JSON (`self.__next_f.push(...)`) and `<script>`/`<style>` blocks
   - Inserts newlines at block boundaries before stripping HTML tags
   - Decodes HTML entities (multiple passes for `&amp;amp;` chains)
   - Removes JS/JSON artifact lines
   - Extracts two editorial sections:
     - **TAKEAWAYS** — bullet-formatted key points, output as `TAKEAWAYS:\n<bullets>`
     - **SUMMARY** — prose narrative + forward-looking bullets, split into `SUMMARY_PROSE:\n<paragraph>` and `SUMMARY_BULLETS:\n<bullets>`
   - The prose/bullet split uses a heuristic: after sentence-splitting compact HTML, the first block of continuous narrative sentences is prose, and lines starting with proper nouns or "The company"/"Management" are editorial bullets

4. `get_history(ticker)` returns all transcript records (date, quarter, fiscal_year, url) for GuidanceTracker's multi-quarter analysis

**Caching:** In-memory dict (`self._cache`) keyed by ticker — one fetch per ticker per process. The SQLite DB itself is built offline by `fool_transcript_db.py --build` and updated monthly by `--update` or the orchestrator's `_db_maybe_update()` call.

---

### 2e. `quarterly_processor.py` — Standalone Quarter Actuals

**Purpose:** Extracts standalone quarterly financial figures from 10-Q filings for GuidanceTracker, replacing annual actuals that caused period mismatches (e.g., CRM BEAT +304% from comparing quarterly EPS guidance against full-year EPS).

**How it works:**

1. Fetches up to N recent 10-Q filings via edgartools
2. For each filing, parses the XBRL into IS/BS/CF DataFrames via `filing.xbrl().statements`
3. Extracts the current-period standalone column (edgartools returns standalone quarter, not YTD cumulative — no subtraction needed)
4. For each quarter, extracts: revenue, gross_profit, net_income, operating_income, diluted_shares, EPS (direct tag or NI/shares), gross_margin, total_assets, equity, total_debt, current_ratio
5. Only includes a quarter if at least revenue or EPS resolved

**Sector gate:** Skips Financial Services companies entirely (bank revenue tags not applicable to quarterly extraction).

**Output:** `dict[str, dict]` — quarter_end_date → actuals dict. Fed directly to `GuidanceTracker.analyse()` as the `quarterly_actuals` parameter.

---

### 2f. `pipeline_config.csv` — Externalised Thresholds

All flag-firing thresholds used by `agents.py` are defined in this CSV (columns: key, value, description, unit, domain_notes). This allows tuning when flags fire without code changes:

| Key | Default | Unit | What it controls |
|-----|---------|------|-----------------|
| `de_ratio` | 3.0 | × | D/E above this → high leverage flag |
| `gross_margin_drop_bps` | 500 | bps | YoY GM compression → compression flag |
| `current_ratio` | 1.0 | × | Below this → liquidity flag |
| `interest_coverage` | 2.0 | × | Below this → debt service flag |
| `revenue_cagr_negative` | 0.0 | decimal | Below 0% → top-line contraction flag |
| `nim_floor` | 0.01 | decimal | Bank NIM below 1% → yield compression flag |
| `efficiency_ratio_high` | 0.75 | decimal | Bank efficiency >75% → elevated cost flag |
| `op_cost_ratio_high` | 0.85 | decimal | Energy cost ratio >85% → margin pressure flag |
| `op_leverage_distortion` | 15.0 | × | \|Operating leverage\| above this → non-representative flag |
| `roe_benchmark_financials` | 0.12 | decimal | Financial sector ROE floor (12%) |
| `roe_benchmark_general` | 0.20 | decimal | General sector ROE "strong returns" threshold (20%) |
| `tone_positive_threshold` | 0.20 | score | Tone score above this → "Confident" label |
| `tone_negative_threshold` | −0.20 | score | Tone score below this → "Cautious" label |
| `ev_ebitda_premium` | 30.0 | × | EV/EBITDA above this → premium valuation flag |
| `pe_current_premium` | 45.0 | × | P/E above this → premium valuation flag |

Each row includes `domain_notes` with sector-specific tuning guidance (e.g., "utilities ~2.0× for D/E; retailers structurally run below 1.0× current ratio").

---

### 3. `orchestrator.py` — Pipeline Controller (1,165 lines)

**Purpose:** The main entry point. Wires together all data loaders, agents, and the renderer. Two public methods: `run()` (full pipeline → PDF) and `run_data_only()` (data + agents → dict, no PDF).

**How `run(ticker, sector, author)` works:**

**Step 1 — Load financial data:** Instantiates `FactsDataProcessor`, calls `load_data(max_years=5)`, builds a `CompanyFinancialProfile`. Rejects 20-F filers (foreign private issuers) since they lack 10-Q/8-K coverage.

**Step 2 — Fetch historical FY-end prices:** Uses yfinance to get closing prices at each fiscal year-end date. Results cached via `FilingCache`.

**Step 3 — Fetch supplementary data:** This is the most expansive step, with many sub-steps:
- **3a:** Fetches 4 most recent 10-Qs + latest 10-K for MD&A guidance extraction
- **3b:** Basel III capital ratios from FR Y-9C (banks only — JPM, BAC, WFC, GS, etc.)
- **3c:** Latest 8-K earnings press release narrative (up to 25,000 chars)
- **3c2:** Three independent guidance sources: Motley Fool transcript, EDGAR Exhibit 99.1 (press release), Exhibit 99.2 (prepared remarks)
- **3d:** Institutional ownership (13-F / yfinance)
- **3e:** Insider transactions (SEC Form 4)
- **3f:** Short interest (yfinance / FINRA-sourced)
- **3g:** Peer comparison (yfinance industry-derived)
- **3h:** Company overview (business summary + experimental segment revenue)
- **3i:** Analyst price targets (yfinance)
- **3j:** Price context (52-week range, SMAs, volume)
- **3k:** Estimate revisions (yfinance earnings trend)
- **3l:** Momentum / relative strength vs S&P 500 and peers
- **Debt note:** 10-K debt schedule parsing for credit quality
- **FRED data:** 10-year UST risk-free rate + credit spread curves

**Step 4 — Run agents:** Five analytical agents (in `agents.py`, 3,376 lines) process the data. All agents are sector-aware — they route through `_sector_group(sector, ticker)` which maps sector strings to one of four groups: `financials`, `energy`, `utilities`, or `general`. Each group suppresses irrelevant metrics and applies sector-appropriate alternatives.

**Thresholds are externalised:** All flag-firing thresholds live in `pipeline_config.csv` (loaded at import time via `_load_thresholds()`), so they can be tuned without touching code. Defaults are hardcoded in `_CONFIG_DEFAULTS` as fallback.

1. **FundamentalAgent.analyze()** → profitability & activity metrics dict

   Computes per-period metrics across all fiscal years, with sector-aware routing:

   | Metric | General | Financials | Energy |
   |--------|---------|------------|--------|
   | Gross Margin | Revenue − COGS / Revenue | NIM (NII / interest-earning assets) | Operating Cost Ratio (COGS / Revenue) |
   | Operating Margin | OI / Revenue | Suppressed ("N/A") | OI / Revenue |
   | Net Margin | NI / Revenue | NI / financial_revenue (NII + NonInterestIncome) | NI / Revenue |
   | ROE | NI / avg equity (CFA convention) | Same | Same |
   | ROIC | NOPAT / invested capital | Suppressed (invested capital invalid for banks) | Same as general |
   | Efficiency Ratio | Suppressed | NonInterestExpense / financial_revenue | Suppressed |
   | ROTCE | Suppressed | NI / tangible common equity | Suppressed |
   | Basel III | N/A | CET1, Tier 1, Total Capital, Leverage (from FR Y-9C or XBRL) | N/A |

   Additional metrics computed for all sectors: EBITDA margin, FCF margin, SG&A %, R&D %, effective tax rate, DSO/DPO/CCC (cash conversion cycle), net debt, net debt/EBITDA, EBITDA interest coverage, ROCE, book value per share, FFO margin (REITs only), asset turnover, inventory turnover, DSI, operating leverage, revenue CAGR.

   **Flag generation:** Fires red flags based on configurable thresholds — gross margin compression >500bps, negative revenue CAGR, net loss, D/E >3x, NIM below 1%, efficiency ratio >75%, operating leverage distortion (|ratio| >15x triggers a non-representative marker with full margin trajectory), FCF margin >5pp decline from peak, operating margin >4pp YoY compression. Also fires combined signals: insider distribution (≥3 sellers, net negative) + rising short interest (>10% MoM), low institutional ownership (<20%), top-10 holder concentration (>40%).

2. **RiskAgent.assess()** → liquidity, solvency & credit risk dict

   Per-period metrics:
   - **Current ratio / Quick ratio** — suppressed for financials (meaningless for banks)
   - **Interest coverage** (EBIT / interest expense) — suppressed for financials (interest is cost of funds). Falls back to CF `interest_paid` when IS `InterestExpense` tag is missing. Fires a `[DATA ERROR]` flag when debt is >10% of assets but interest expense resolves to zero
   - **D/E ratio** — all sectors. Equity floor guard: skips if equity < 1% of total assets (catches UNH-style par-value accounting). Trend tracking across periods with re-leveraging flag
   - **Altman Z-Score** — suppressed for financials (model not calibrated), energy (asset-heavy E&P structurally scores low), and utilities (regulated companies with structural high leverage). For general sector: computes full 5-factor Z, classifies as Safe (>2.99), Grey zone (1.81–2.99), or Distress zone (<1.81)
   - **Credit quality** (`_compute_credit_quality`): looks up S&P/Moody's/Fitch ratings from `ratings.csv`, maps rating to FRED OAS tier, derives market-implied cost of debt (RF + OAS). Parses debt schedule from `DebtNoteFetcher` output (tranches, maturities, weighted average rate). Computes maturity wall (per-year % of total debt, weighted average maturity, lumpiness flags for >25% single-year concentration or >45% 3-year window). Falls back to IS-derived cost of debt (interest expense / average total debt) when tranche rates unavailable
   - **Interest coverage declining trend** — fires when coverage declines across all periods AND most recent value is below 4x

3. **ValuationAgent.value()** → multiples & WACC dict

   - **P/E** — historical (FY-end price / EPS) and current (live price / most recent EPS). Handles negative EPS, share count fallbacks (derives from market cap / price when XBRL tags missing), and unit correction (detects when shares are in millions vs actual count by comparing to implied count from market data)
   - **P/B** — historical and current, using common equity (total equity − preferred stock)
   - **P/TBV** — tangible book value (equity − goodwill − intangibles). Guards against untagged goodwill producing TCE = common equity (misleading ROTCE = ROE)
   - **EV/Sales** — suppressed for financials (revenue includes gross interest income on multi-trillion asset base)
   - **EV/EBITDA** — suppressed for financials. EV approximated as market cap + total debt
   - **Premium valuation flag** — fires when EV/EBITDA >30x or P/E >45x (configurable)
   - **WACC** (`_compute_wacc`): CAPM for cost of equity (RF + beta × ERP), rating-implied cost of debt (RF + OAS), market-value capital structure weights. Beta from yfinance, ERP from Damodaran (live-fetched via `erp_fetcher.py`, hardcoded fallback 4.31%). Suppressed for financials (debt is raw material, not financing). Tax rate from most recent effective tax rate, fallback to 21% US statutory

4. **TrendCommentaryAgent.narrate()** → narrative bullets + flags dict

   Generates deterministic prose commentary by comparing most recent vs prior period ratios:
   - Revenue growth/decline with dollar amounts
   - Margin expansion/compression with basis points
   - ROE vs sector-appropriate benchmarks (12% for financials, 20% for general), with a leverage guard (flags high ROE driven purely by D/E >3x)
   - Leverage trajectory, liquidity assessment, interest coverage commentary
   - NIM commentary for financials (widened/compressed bps)
   - Valuation context (current P/E vs FY-end P/E)
   - DSI outlier notes (>180 days threshold, with pharma-specific context)
   - Revenue CAGR summary
   - Management guidance extraction from 8-K earnings release (falls back to 10-K MD&A)
   - Aggregates and deduplicates all flags from FundamentalAgent + RiskAgent + ValuationAgent

5. **GuidanceAgent.analyse()** → forward guidance & tone dict

   Multi-source guidance extraction with priority cascade:
   - **Priority 1:** Motley Fool transcript (richest — full Q&A)
   - **Priority 2:** EDGAR Exhibit 99.2 (prepared remarks) / 99.1 (press release) — only if Fool unavailable or produced <3 forward sentences
   - **Priority 3:** SEC MD&A filings (10-Q Item 2 / 10-K Item 7) — last resort only if both above failed

   **Sentence extraction pipeline:**
   1. Text is split into sentences (handles both prose and bullet formats)
   2. Boilerplate filter removes exhibit metadata, officer titles, safe-harbor language, business description boilerplate
   3. Actuals filter removes reported-results sentences (comparisons to prior periods, historical figures)
   4. Forward-looking pattern matcher identifies guidance sentences
   5. For structured Fool transcripts: TAKEAWAYS bullets are classified as forward vs backward using two regex classifiers (`_BULLET_FORWARD_RE`, `_BULLET_BACKWARD_RE`), with a hard-backward override for unambiguous reported-results language and a "future number" tiebreaker when both signals fire

   **Categorisation:** Forward sentences are assigned to one of five categories (first-match-wins):
   - `financial_targets` — revenue/margin/EPS/capex guidance with concrete numbers
   - `capital_allocation` — dividends, buybacks, debt management, CET1 targets
   - `growth_outlook` — demand/supply dynamics, market share, SaaS metrics (ARR, RPO)
   - `risk_factors` — credit loss, tariffs, geopolitical, regulatory, competitive
   - `macro_view` — economy-wide conditions (GDP, inflation, Fed policy, industry cycles)

   **Tone scoring — dual system:**
   - **Primary (when Fool summary available):** FinBERT-tone model (`yiyanghkust/finbert-tone`) — a fine-tuned BERT for financial sentiment. Loaded once per process (`_finbert_bundle` singleton). Classifies Positive→Confident, Negative→Cautious, Neutral→Neutral with softmax confidence scores
   - **Fallback (regex):** `_score_tone()` counts matches against two curated lexicons (~30 terms each). Confident lexicon: record, strong, robust, momentum, accelerating, pleased, encouraged, profitability, etc. Cautious lexicon: uncertainty, headwind, challenging, volatile, deteriorating, pressure, tariff, recession, decline, compression, etc. Score = (confident_count − cautious_count) / total, mapped to label via configurable thresholds (±0.20). Lexicons were updated from FinBERT corpus analysis (854 transcripts, 21.3% mismatch rate)

Plus three composite scores computed from agent outputs:
- **Execution Quality** (0–100): Compares guided revenue/margin ranges to actuals. BEAT scores 100, MISS scores 0, averaged across available quarters.
- **Communication Quality** (0–100): Cross-references management tone (Confident/Neutral/Cautious) against their track record credibility. A matrix maps (tone × credibility_band) to a score and alignment narrative.
- **Management Quality** (0–100): Weighted composite — 40% Guidance Accuracy, 40% Execution Quality, 20% Communication. Weights renormalize if components are unavailable.

**Tone-credibility adjustment:** If management's tone is "Confident" but their credibility score is low, the tone label and score are penalized (downgraded toward Neutral or Cautious). If tone is Confident but key fundamentals are deteriorating (negative revenue CAGR, operating margin compression >4pp, FCF margin <5%), a contrast note is injected into the Trend Commentary.

**Step 5 — Render PDF:** Passes all agent outputs to `EquityBriefRenderer.render()`.

**Other orchestrator capabilities:**
- `run_data_only()`: Steps 1–4 without PDF rendering. Used by the validation batch.
- `stitch_pdfs()`: Merges multiple single-ticker PDFs into one file with bookmarks.
- `_fetch_latest_8k_narrative()`: Scans up to 5 recent 8-Ks for an earnings release (identified by quarter/financial keywords + minimum 3,000 char length), returning the first 25,000 chars.

---

### 4. `guidance_tracker.py` — Management Credibility Analysis (641 lines)

**Purpose:** Compares what management guided (from earnings call transcripts) to what actually happened (from XBRL financials), producing a credibility score and trend.

**How it works:**

1. **Sector gate:** Skips Financial Services companies entirely — banks guide on NII/efficiency ratio/CET1, which the current regex parsers don't handle. Returns an empty result with a note.

2. **Fetch transcript history:** Gets the last N quarters of earnings call URLs from `fool_transcripts.db` via the `TranscriptLoader`.

3. **Extract guidance figures:** For each transcript, three regex parsers run:
   - `_parse_rev_guidance`: Extracts revenue guidance ranges ("revenue of $X to $Y billion"). Pre-filters out full-year/annual guidance sentences to avoid matching annual totals against quarterly actuals.
   - `_parse_eps_guidance`: Extracts EPS ranges. Filters out dividend/distribution sentences to avoid false matches.
   - `_parse_gm_guidance`: Extracts gross margin guidance. Rejects implausible ranges (<5% or >95%, or spans >20pp).

4. **Load actuals:** Tries quarterly actuals first (via `QuarterlyDataProcessor` which derives standalone quarter figures from 10-Q filings). Falls back to annual actuals from the 10-K profile.

5. **Match guidance to actuals:** For each transcript, finds the fiscal period whose end date is closest to and strictly after the transcript date, bounded to 120 days forward. This prevents matching Q4 guidance against Q1 of the next year when Q4 actuals are missing.

6. **Score each comparison:** `_beat_miss()` classifies as BEAT (actual ≥ high end), MISS (actual ≤ low end), or INLINE. Includes a period-mismatch detector: if actual/guided midpoint ratio is >3x or <0.33x, flags as "PERIOD MISMATCH?" rather than a misleading BEAT/MISS.

7. **Attribution analysis:** Counts external-blame language (macro, FX, supply chain, tariffs, etc.) vs internal-blame language (execution, cost overrun, pricing error) in the transcript text.

8. **Credibility score:** 0–100. BEAT=100, INLINE=80, MISS=0 per metric, averaged across all scored comparisons. Default 50 if no data.

9. **Trend:** Compares credibility of the first half vs second half of scored quarters. Only computed when both halves have at least one scored comparison (recent bug fix — previously, unscored quarters returning the default 50 could create spurious "deteriorating" trends).

**Output dict:** `{available, quarters, credibility, trend, attribution, flags, summary}`

**Recent bug fix:** `_has_scored()` had a `KeyError` — `q[k]` referenced directly without a `.get()` guard. Fixed to `q.get(k, {}).get(...)`.

---

### 5. `insider_transactions.py` — SEC Form 4 Parsing (522 lines)

**Purpose:** Fetches and classifies insider buy/sell transactions from SEC Form 4 filings.

**How it works:**

1. **Fetch Form 4 filings:** Uses edgartools to get all Form 4 filings for the ticker within the lookback window (default 90 days, filtered by filing date).

2. **Parse each filing:** For each Form 4:
   - Extracts `insider_name` and `position` from the form metadata
   - Gets `market_trades` (a DataFrame of open-market transactions, excluding option exercises/RSU vesting/gifts)
   - Resolves footnotes: Form 4 footnotes are stored as ID references (e.g., "F4") in the trade row, with the actual text on the form's top-level `footnotes` object. A `_FootnoteGetWrapper` adapts the edgartools `Footnotes.get(id)` interface to dict-like `.get(key, default)`.
   - Parses each trade row: extracts date, shares, price, direction (Acquired=BUY, Disposed=SELL), 10b5-1 plan status (from footnote text), and position percentage (from `Remaining` column — pre-transaction holding = shares ± remaining, then pct = shares / pre_holding).

3. **Deduplication:** Keyed on (insider_name, date, shares, price) to handle amended filings.

4. **Transaction-date filter:** Re-filters on actual transaction date (not just filing date) to exclude stale transactions that were batched into a later filing.

5. **Signal computation:**
   - `buy_count`: Distinct insiders with discretionary (non-10b5-1) open-market buys
   - `sell_count`: All distinct sellers (no 10b5-1 filter — sell-side noise handled via higher count threshold)
   - `cluster_buying`: ≥3 distinct non-10b5-1 buyers
   - `large_position_change`: Any transaction ≥50% of the insider's prior holding

6. **SQLite cache:** Stores all parsed transactions in `insider_transactions.db` for historical tracking.

**Recent fix — DB migration:** The `transactions` table was originally created without `remaining` and `pct_of_position` columns. Since `CREATE TABLE IF NOT EXISTS` doesn't alter existing tables, added `PRAGMA table_info` introspection + `ALTER TABLE ADD COLUMN` migration on startup.

---

### 6. `renderer.py` — PDF Generation (2,547 lines)

**Purpose:** Takes all agent outputs and builds a formatted multi-page PDF using ReportLab Platypus.

**Layout:**

- **Page 1:** Header bar (ticker, sector, market cap, date) + three financial tables:
  - Section 1: Fundamentals (revenue, margins, growth, returns, leverage — 3–5 year columns)
  - Section 2: Risk & Solvency (debt ratios, interest coverage, credit rating, liquidity)
  - Section 3: Valuation (P/E, EV/EBITDA, P/B, FCF yield, historical price multiples)

- **Page 2+:** Qualitative sections:
  - Section 4: Red Flags (always rendered, even if empty — "no red flags" is informative)
  - Section 5: Trend Commentary (narrative bullets from TrendCommentaryAgent)
  - Section 6: Management Guidance & Tone (extracted guidance text, tone classification, forward-looking statements)
  - Section 7: Management Track Record (GuidanceTracker output — beat/miss/inline history, credibility score, trend)

Additional sections (rendered when data is available): company overview, peer comparison, ownership/insider activity, short interest, analyst targets, price context, momentum, debt schedule, estimate revisions.

**Styling:** Color palette with dark navy headers, accent blue section bars, alternating row shading, red/amber/green conditional formatting. Diagonal watermark ("DRAFT · AUTHOR · TICKER") on every page. Footer with disclaimer.

---

### 7. `validate_pipeline.py` — Batch Validation Harness (951 lines)

**Purpose:** Ground-truth testing of the data layer against the full ticker universe. Answers: "For which tickers does the pipeline produce correct financial data, and where does it break?"

**How it works:**

1. **Process each ticker:** For every entry in `ticker_universe.py`:
   - Fetches company facts from SEC
   - Runs waterfall resolution (same as `FactsDataProcessor`)
   - Applies sector-specific revenue adjustments (bank sums, insurer sums, commodity cross-checks)
   - Stores the resolution log (which raw concept resolved for each standard tag)
   - Records concept gaps (standard tags that couldn't resolve to any raw concept)

2. **Reconciliation against yfinance:** For the most recent period, compares:
   - Pipeline Revenue vs yfinance Total Revenue
   - Pipeline Net Income vs yfinance Net Income
   - Pipeline Total Assets vs yfinance Total Assets
   
   Thresholds: >5% delta = "warn", >20% = "error". Skips near-zero denominators (<$100M) and total_assets ratios >5x (currency denomination mismatches).

3. **Post-batch analysis:**
   - **Concept frequency table:** Across all processed tickers, how many file each raw XBRL concept? This is the input for improving waterfalls — if 200 tickers file `SomeNewConcept` but nobody's waterfall includes it, it's a candidate addition.
   - **Waterfall patch suggestions:** For each unresolved standard tag, finds raw concepts that the affected tickers actually file but that aren't in the current waterfall. Filtered by: ≥20% of gap tickers must file the concept, concept must not appear in >85% of all tickers (too generic), and a semantic blocklist excludes tax reconciliation items, lease maturity schedules, comp footnotes, etc.

4. **Output:** SQLite database (`validation.db`) with tables for ticker status, resolution logs, concept gaps, concept frequency, reconciliation results, and waterfall patches. CSV exports for easy inspection.

**CLI options:** `--tier`, `--sector`, `--ticker` for scoping; `--resume` to skip already-processed tickers; `--workers` for parallelism (capped at 4 due to SEC rate limits); `--delay` for throttling.

---

## Data Flow Diagram

```
Ticker (e.g. "AAPL")
    │
    ▼
build_sec_universe.py ──► ticker_universe.py (sector assignment)
    │
    ▼
facts_processor.py
    │  SEC Company Facts API → single HTTP call
    │  _extract_annual() per concept
    │  _resolve_waterfall() with sector/company overrides
    │  _reconcile() post-hoc checks
    │
    ▼
CompanyFinancialProfile (3 DataFrames: IS, BS, CF)
    │
    ├──► orchestrator.py fetches 15+ supplementary data sources
    │       yfinance (prices, ownership, peers, targets, momentum)
    │       edgartools (filings, Form 4, 8-K narrative)
    │       Motley Fool (transcripts)
    │       FRED (risk-free rate, credit spreads)
    │       FR Y-9C (bank capital ratios)
    │
    ▼
5 Analytical Agents (in agents.py)
    ├── FundamentalAgent.analyze()   → metrics dict
    ├── RiskAgent.assess()           → risk dict
    ├── ValuationAgent.value()       → valuation dict
    ├── TrendCommentaryAgent.narrate() → narrative dict
    └── GuidanceAgent.analyse()      → guidance dict
    │
    + GuidanceTracker.analyse()      → credibility dict
    + _compute_execution_quality()   → 0-100 score
    + _compute_communication_quality() → 0-100 score
    + _compute_management_quality()  → composite 0-100
    + _apply_tone_credibility_adjustment() → adjusted tone
    │
    ▼
renderer.py → PDF
    or
validate_pipeline.py → SQLite + CSVs
```

---

## Key Design Decisions

**Why a waterfall pattern?** Companies use hundreds of different XBRL concept names for the same economic quantity. "Revenue" alone has 20+ valid tags across US GAAP. The waterfall tries them in priority order (most common first, measured by company count across all filers) and stops at the first hit. This gives ~95% resolution rate without manual per-ticker configuration.

**Why sector overrides?** Universal waterfalls fail for sectors with fundamentally different accounting: banks don't have "revenue" in the ASC 606 sense (they have NII), REITs report rental income under lease accounting (ASC 842/840), and insurers report premiums. Sector overrides prepend the right concepts before the universal fallback.

**Why three guidance sources?** Motley Fool transcripts are the richest (full Q&A), but coverage is incomplete (not all companies, occasional truncation). EDGAR Exhibit 99.1 (press release) is always available but terse. Exhibit 99.2 (prepared remarks) bridges the gap. The GuidanceAgent merges all three.

**Why credibility-adjusted tone?** A management team saying "we're confident" means different things depending on their track record. If they've missed guidance 4 quarters in a row, "confident" should carry less weight. The tone-credibility adjustment systematically discounts confident tone when the data doesn't support it.

**Why a validation pipeline?** With ~8,000 tickers across all SIC codes, edge cases are everywhere. The validation harness catches broken waterfalls, mis-mapped sectors, and concept resolution failures at scale — it's the feedback loop that drives waterfall improvement.

---

## Caching Architecture

The pipeline uses two tiers of caching — in-memory dicts for hot data within a single process, and SQLite databases for persistent cross-session storage. Every external API call (SEC, yfinance, FRED, Motley Fool) is expensive and rate-limited, so the caching layer is what makes the system practical for batch runs.

### Tier 1 — In-Memory Caches (process-scoped, lost on restart)

These live in `facts_processor.py` as module-level dicts:

| Cache | Key | Value | Purpose |
|-------|-----|-------|---------|
| `_cik_cache` | ticker (e.g. "AAPL") | 10-digit CIK string | Avoids re-fetching SEC's `company_tickers.json` (~13K entries) on every ticker. Populated lazily on first `_get_cik()` call, then reused for the entire process. |
| `_facts_cache` | CIK string | Full company facts JSON blob | The SEC Company Facts API call returns the entire XBRL history for a company (~2–10MB). Caching this means the same ticker processed twice in one run (e.g., orchestrator + validation) doesn't hit SEC again. |

These caches are the reason `FactsDataProcessor` can be instantiated repeatedly for the same ticker within a validation batch without multiplying API calls.

### Tier 2 — SQLite Persistent Caches (survive restarts)

Seven SQLite databases live in the project root, each owned by a specific loader:

| Database | Owner | What's Cached | Cache Key | Invalidation |
|----------|-------|---------------|-----------|-------------|
| `filing_cache/` (dir-based) | `FilingCache` | Financials, prices, narratives | See below | Filing-date based |
| `fool_transcripts.db` | `TranscriptLoader` | Earnings call transcript URLs and metadata per ticker/quarter | (ticker, quarter, fiscal_year) | Monthly auto-update via `_db_maybe_update()` |
| `insider_transactions.db` | `InsiderTransactionLoader` | Parsed Form 4 transactions | (ticker, date, insider_name, shares, price) | INSERT OR REPLACE — new fetches overwrite stale entries |
| `ownership_history.db` | `OwnershipLoader` | 13-F institutional ownership snapshots | ticker | Not in uploaded files — likely time-based |
| `short_interest_history.db` | `ShortInterestLoader` | FINRA short interest data | ticker | Not in uploaded files — likely time-based |
| `peer_metrics_cache.db` | `PeerComparisonLoader` | Peer company financial metrics | ticker | Not in uploaded files — likely time-based |
| `validation.db` | `validate_pipeline.py` | Resolution logs, concept gaps, reconciliation results, waterfall patches | ticker + standard_tag | Overwritten on each validation run (`INSERT OR REPLACE`) |

### FilingCache — The Central Cross-Session Cache (`filing_cache.py`)

`FilingCache` stores data in two forms: a SQLite database (`filing_cache.db`) for metadata and lightweight values, and Parquet files on disk (`xbrl_cache/` directory) for the heavy financial DataFrames. This hybrid approach keeps the DB small while leveraging Parquet's columnar compression for multi-year financial tables.

**SQLite Schema — three tables:**

```sql
filings (ticker, filing_date)          -- PK: (ticker, filing_date)
    form_type TEXT                      -- "10-K" | "20-F"

narratives (ticker, filing_date)       -- PK: (ticker, filing_date)
    source TEXT                        -- "8-K" | "10-K"
    earnings_text TEXT                 -- up to 25K chars of press release
    filing_date_8k TEXT               -- ISO date of the 8-K itself
    fetched_date TEXT                  -- ISO date when this row was written

prices (ticker, fetch_date)            -- PK: (ticker, fetch_date)
    fy_prices_json TEXT               -- JSON list of floats/nulls
    periods_json TEXT                 -- JSON list of period date strings
```

**Parquet files** live at `xbrl_cache/{TICKER}_{filing_date}_{statement}.parquet` — three files per ticker (income_statement, balance_sheet, cash_flow). Each is the resolved waterfall DataFrame with columns `[standard_concept, concept, <period_dates...>]`.

**Domain 1 — Financials:** `get_financials(ticker) → (hit, financials_dict, meta_dict)`
- Queries the `filings` table for the most recent `filing_date` for this ticker
- Loads all three Parquet files for that (ticker, filing_date) pair
- Returns the meta dict (`{filing_date, form_type}`) — the orchestrator uses `form_type` to detect 20-F foreign filers without re-fetching from SEC
- **Invalidation:** `is_filing_current(ticker, live_filing_date)` compares the cached filing_date against the live one. If a new 10-K has been filed, the cached date won't match → cache miss → re-fetch and overwrite
- **Note:** The orchestrator currently uses `FactsDataProcessor` (which has its own in-memory cache) rather than `FilingCache` for financials — the financials cache domain is a holdover from the earlier `RobustDataProcessor` that was slower and benefited more from persistent caching

**Domain 2 — Narratives:** `get_narrative(ticker, current_filing_date) → (hit, text, 8k_date)`
- Keyed on (ticker, filing_date) where filing_date is the most recent 10-Q/10-K date — so when a new quarterly filing appears, the key changes and the old narrative is bypassed
- **TTL expiry:** `NARRATIVE_TTL_DAYS = 7`. Even if the filing_date key matches, the row expires after 7 days (`fetched_date` is checked). This catches newly filed 8-K earnings releases that appear between quarterly filings
- `store_narrative()` stamps today's date into `fetched_date` on every write, resetting the TTL clock

**Domain 3 — Prices:** `get_prices(ticker, periods) → (hit, prices_list)`
- Stores FY-end historical closing prices as a JSON list, alongside the periods list that produced them
- **TTL expiry:** `PRICE_TTL_DAYS = 1`. Historical FY-end prices don't change, but a 1-day TTL means same-day re-runs hit cache while next-day runs re-fetch (picking up any yfinance corrections)
- **Period alignment check:** On cache read, `periods_json` is compared against the requested periods list. If the fiscal year structure changed (e.g., a new FY ended), the lists won't match → cache miss → re-fetch. This is the primary invalidation mechanism, with TTL as a secondary safety net

**Cache management utilities:**
- `status()` — joins all three tables to produce a per-ticker summary (used by a `query_cache.py` script for diagnostics)
- `clear(ticker)` — deletes all DB rows and Parquet files for a ticker, forcing a full re-fetch on the next run

### Cache Flow in the Orchestrator

```
run("AAPL") begins
    │
    ├─ Step 1: FactsDataProcessor.load_data()
    │    └─ _get_cik("AAPL")  → _cik_cache (in-memory, process-scoped)
    │    └─ _get_facts(cik)   → _facts_cache (in-memory, process-scoped)
    │    └─ No persistent cache — SEC facts API is fast enough (~1s)
    │
    ├─ Step 1b: Foreign filer check
    │    └─ self._cache.get_financials("AAPL") → meta_dict
    │    └─ Reads form_type from cached filings row (no Parquet load needed)
    │    └─ If form_type == "20-F" → reject ticker without full processing
    │
    ├─ Step 2: Historical prices
    │    └─ self._cache.get_prices("AAPL", periods)
    │         HIT  → cached prices (JSON list), skip yfinance
    │         MISS → yfinance.history("10y") → store_prices()
    │         Miss triggers: TTL > 1 day, or periods list changed
    │
    ├─ Step 3c: 8-K narrative
    │    └─ self._cache.get_narrative("AAPL", filing_date_str)
    │         HIT  → cached earnings_text (up to 25K chars)
    │         MISS → _fetch_latest_8k_narrative() → store_narrative()
    │         Miss triggers: TTL > 7 days, filing_date changed, or no row
    │
    ├─ Step 3d-3l: Supplementary loaders (own SQLite DBs)
    │    └─ insider_transactions.db  ← Form 4 transactions
    │    └─ ownership_history.db    ← 13-F snapshots
    │    └─ short_interest_history.db ← FINRA data
    │    └─ peer_metrics_cache.db   ← peer financials
    │    └─ fool_transcripts.db     ← transcript URLs/metadata
    │
    └─ Step 4: Agents run on in-memory data (no caching at agent layer)
```

### Validation Pipeline Caching

`validate_pipeline.py` uses its own SQLite (`validation.db`) as both a cache and a results store. The `--resume` flag queries this DB:

```sql
SELECT ticker FROM tickers WHERE status='ok'
```

Any ticker already processed successfully is skipped on the next run. This is what makes it practical to run validation across 8,000 tickers — if the process crashes at ticker #3,000, `--resume` picks up at #3,001 without re-processing.

The validation pipeline does NOT use `FilingCache` — it calls `facts_processor` functions directly and hits SEC/yfinance on every ticker. This is intentional: validation needs fresh data to detect regressions, not cached values that might mask a broken waterfall.

### Transcript DB Auto-Update

The orchestrator calls `_db_maybe_update(verbose=True)` from `fool_transcript_db.py` at the start of every `run()`. This checks whether the current and previous month's transcripts are present in `fool_transcripts.db` and fetches missing months. This ensures guidance extraction always has recent transcript data without manual intervention.

### Cache Invalidation Summary

The pipeline uses a mix of TTL-based and key-based invalidation:

| Cache | Invalidation Method | Trigger |
|-------|---------------------|---------|
| `_cik_cache` (in-memory) | Process restart | Implicit — dict lost when process ends |
| `_facts_cache` (in-memory) | Process restart | Implicit — dict lost when process ends |
| FilingCache financials | Key mismatch | `is_filing_current()` — new 10-K filing_date ≠ cached filing_date |
| FilingCache narratives | TTL + key | 7-day expiry on `fetched_date`, plus filing_date key change on new 10-Q/10-K |
| FilingCache prices | TTL + alignment | 1-day expiry on `fetch_date`, plus periods list mismatch check |
| `insider_transactions.db` | INSERT OR REPLACE | New fetches overwrite stale rows with same natural key |
| `fool_transcripts.db` | Monthly auto-update | `_db_maybe_update()` checks for missing current/previous month |
| `validation.db` | `--resume` flag | Skips tickers with status='ok'; re-running without flag overwrites |

Things that are **never cached** (always live): `current_price`, `market_cap` — both fetched from yfinance on every run because they change intraday.

---

## Current State & Known Limitations

**Error landscape (from the last validation run, 303 total errors):**
- ~230 filterable by the universe rebuild (foreign filers, warrants, crypto ETFs) — handled by the `_should_exclude()` additions in `build_sec_universe.py`
- ~12 Financial Services companies misclassified as "General" (SIC gap fill addressed)
- ~11 Real Estate tickers using XBRL extension tags not in the waterfall
- ~4 definitional disagreements (IBKR, CG, SYF, WTM) where pipeline and yfinance define "revenue" differently
- ~47 miscellaneous

**Not yet supported:**
- Foreign private issuers (20-F filers) — no 10-Q/8-K guidance coverage
- Bank-specific GuidanceTracker metrics (NII guidance, efficiency ratio, CET1)
- IFRS filers (the waterfall is US GAAP only)
- Quarterly standalone financial extraction is available but the GuidanceTracker falls back to annual actuals for many tickers

---

## How to Run

**Generate a single report:**
```python
from orchestrator import EquityAnalystOrchestrator
orch = EquityAnalystOrchestrator(edgar_identity="Your Name you@email.com")
path = orch.run("AAPL", sector="Technology", author="Research Team")
```

**Rebuild the SEC universe:**
```bash
python build_sec_universe.py --identity "Your Name you@email.com"
```

**Run validation (full or partial):**
```bash
python validate_pipeline.py --identity "Your Name you@email.com" --resume
python validate_pipeline.py --tier 1 --delay 0.5
python validate_pipeline.py --ticker AAPL MSFT NVDA
```
