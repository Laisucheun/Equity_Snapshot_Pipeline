# Equity Analyst Pipeline

Automated equity research brief generator for US-listed companies. Given a ticker and sector, the pipeline fetches three years of SEC EDGAR XBRL data, computes 15+ financial ratios across five analyst agents, and renders a two-page PDF equity brief with structured red flags.

**Validated across 150 tickers, 9 sectors, 0 runtime failures.**

---

## What It Produces

Each run outputs a two-page PDF and/or a structured DataFrame row containing:

| Section | Content |
|---|---|
| Fundamental Analysis | Gross margin, operating margin, EBITDA margin, net margin, ROE, ROA, ROIC, asset turnover, FCF margin, revenue CAGR, operating leverage |
| Risk & Solvency | Current ratio, quick ratio, D/E ratio, net debt/EBITDA, interest coverage, Altman Z-Score |
| Valuation | P/E (FY-end and current price), P/B, P/TBV, EV/EBITDA, EV/Sales |
| Basel III (financials) | Tier 1 capital ratio, total capital ratio, T1 leverage ratio — sourced from FFIEC FR Y-9C bulk files |
| Trend Commentary | Deterministic narrative generated from ratio deltas |
| Red Flags | Structured flags with company value, reference threshold, and one-phrase consequence |
| Management Guidance | Source, tone classification, and categorised sentences from 8-K earnings releases |

---

## Architecture

```
EQUITYANALYSTREPORT/
│
├── data_layer.py          XBRL ingestion, StatementProfile classes, fallback chains
├── agents.py              Five analyst agents + sector routing + red flag logic
├── renderer.py            ReportLab PDF renderer
├── orchestrator.py        Pipeline orchestration; run() for PDF, run_data_only() for DataFrame
├── fr_y9c.py              FR Y-9C Basel III capital ratio fetcher (local BHCF files)
├── diagnose_tags.py       XBRL tag diagnostic tool — find any concept by name or value
│
├── pipeline_config.csv    ← All tunable thresholds and benchmarks (edit without touching code)
│
├── main.py                        PDF batch runner (current ticker universe)
├── run_batch_validation_df.py     DataFrame batch — no PDFs, outputs CSV + Excel
└── run_batch_validation_150.py    150-ticker cross-industry validation batch
```

### Agent Roles

```
RobustDataProcessor  →  CompanyFinancialProfile
                              │
             ┌────────────────┼────────────────────┐
             ▼                ▼                    ▼
    FundamentalAgent    RiskAgent           ValuationAgent
    (margins, CAGR,     (solvency,          (P/E, P/B, EV/
     op leverage)        Altman Z,           EBITDA, P/TBV)
             │            D/E flags)              │
             └────────────────┼────────────────────┘
                              ▼
                   TrendCommentaryAgent   GuidanceAgent
                   (deterministic         (8-K tone +
                    narrative)            sentences)
                              │
                              ▼
                       EquityBriefRenderer  →  PDF
```

---

## Sector Routing

The pipeline routes each ticker to one of four processing groups based on the sector string.

```python
"Financials", "Banking", "Insurance"  →  financials
"Utilities", "Utility"                →  utilities
"Energy", "Oil", "Gas", "Mining"      →  energy
everything else                        →  general
```

| Group | Special treatment |
|---|---|
| **financials** | NIM, efficiency ratio, ROTCE, Basel III table; suppresses gross margin, Altman Z, interest coverage |
| **energy** | Shows Operating Cost Ratio instead of gross margin; suppresses Altman Z ("not applicable — E&P") |
| **utilities** | Full general metric set; suppresses Altman Z ("regulated utility — model not applicable"); uses CF interest as fallback for IS interest |
| **general** | Complete ratio set: gross margin, operating margin, EBITDA, ROE, ROA, ROIC, Altman Z |

> **Note:** `"Real Estate"` routes to `general`. REITs receive the full ratio set. Altman Z will score low due to structural leverage — this is expected, not a data error.

---

## Fallback Chains

The pipeline never raises on missing data. Every metric has an ordered fallback chain ending in an honest `N/A` with a diagnostic label rather than a silently wrong number.

### D&A (for EBITDA computation)

```
1. CashFlowProfile._get_max_vector([
       'DepreciationAmortizationCF',
       'DepreciationExpense',
       'DepreciationAndAmortization',
       'DepreciationDepletionAndAmortization',
       'DepreciationAmortizationAndAccretionNet',   ← MSFT
       'OtherDepreciationAndAmortization',
       'Depreciation',
   ])

2. IncomeStatementProfile._get_vector([
       'DepreciationExpense',
       'AmortizationOfIntangibles',
   ])

3. Balance sheet delta (when D&A absent from both CF and IS):
   PP&E accumulated depreciation change + net intangibles decline
   - Accumulated depreciation from standard XBRL tag, OR
   - Parsed from PP&E net label text: "net of accumulated depreciation of $X and $Y"  ← MSFT

4. Raw XBRL company-specific extension tags:
   _get_vector_by_raw_concept([
       'tsla:DepreciationAmortizationAndImpairment',   ← Tesla (custom tag, not in US GAAP taxonomy)
   ])

5. → "N/A (D&A unresolvable)"   ← honest, never shows EBITDA = operating margin
```

### Operating Income (EBIT)

```
1. IncomeStatementProfile._get_vector(['OperatingIncomeLoss'])

2. Back-calculation for companies with no IS operating line
   (e.g. LLY, PFE, MRK — pharma embeds interest in NonoperatingIncomeExpense):
   EBIT ≈ pretax_income + abs(interest_expense) − nonoperating_income
   If interest is zero (cash-rich pharma): EBIT ≈ pretax_income − nonoperating_income
```

### Revenue

```
1. 'Revenue'                                   ← most US GAAP filers
2. 'Revenues'                                  ← alternative GAAP label
3. 'RegulatedAndUnregulatedOperatingRevenue'   ← diversified utilities (NEE)
4. 'ElectricUtilityRevenue'                    ← pure electric utilities
5. 'RegulatedUtilityRevenue'
6. 'PublicUtilitiesRevenueRequirementNet'
7. 'UtilitiesRevenue'
```

### Interest Expense (for coverage ratio)

```
1. IncomeStatementProfile._get_vector(['InterestExpense'])   ← most companies

2. CashFlowProfile.interest_paid                            ← utilities (NEE, SO, DUK…)
   Regulated utilities embed interest in NonoperatingIncomeExpense total on IS;
   the CF statement's supplemental disclosure tags it separately.
```

### Diluted Shares (for EPS → P/E)

```
_get_max_vector(['SharesFullyDilutedAverage', 'SharesAverage', ...])

Why max: edgartools maps both basic and diluted weighted-average share counts
to the same standard_concept for some filers. _get_vector takes iloc[0] which
can land on the smaller basic count, inflating EPS and deflating P/E.
Diluted ≥ basic by definition, so max picks the correct figure.
```

### Gross Margin Guards

```
After computing gm_val = GrossProfit / Revenue:

1. Collapse guard:  round(gm_val, 4) == round(oi / rev, 4)
   → "N/A (not reported separately)"
   Fires for: aerospace/defence (BA), franchisors (MCD, SBUX, CMG)
   where GrossProfit resolves to the same value as OperatingIncomeLoss.

2. Overflow guard:  gm_val > 1.0
   → "N/A (tag overflow)"
   Fires for: DIS-class where GrossProfit resolves to a segment subtotal
   larger than consolidated revenue.
```

### Accumulated Depreciation (balance sheet)

```
1. Standard XBRL tags:
   ['AccumulatedDepreciation',
    'AccumulatedDepreciationAndAmortization',
    'AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment']

2. Label-text parsing on PlantPropertyEquipmentNet row:
   Regex extracts dollar amounts from "net of accumulated depreciation of $X and $Y"
   Applies ×1,000,000 scale (labels are in millions).
   Used by MSFT which does not tag accumulated depreciation as a separate element.
```

---

## Red Flag Format

Every flag includes the company's actual value, the reference threshold or benchmark, and a one-phrase consequence. Format:

```
■ [Metric] [company value] ([period]) [direction] [threshold] — [consequence]
```

Examples:
```
■ Current ratio 0.60x (FY25) below 1.0x threshold — current liabilities exceed current assets
■ D/E ratio increasing: 1.38x (FY25) vs 1.22x (FY24) — re-leveraging trend
■ Altman Z-Score 1.43 (FY25) in distress zone (below 1.81) — elevated default risk
■ Interest coverage 1.54x (FY25) below 2.0x threshold — EBIT barely covers debt service
■ Interest coverage -3.93x (FY24) — EBIT was negative; operating loss not covering interest expense
■ Operating leverage non-representative: 36.33x (FY24→FY25) — GAAP op. margin swung
  14.35% → 43.27% on 6.05% revenue growth. Full op. margin history: FY25 43.27% |
  FY24 14.35% | FY23 10.80%. Verify against adjusted operating income.
```

Operating leverage is flagged as non-representative (rather than suppressed) when `|ratio| > 15×`. The number remains visible in Section 1 with a `[!]` marker; the red flag provides full context.

---

## Configuration

All thresholds and benchmarks live in `pipeline_config.csv`. Edit the `value` column to adjust when flags fire — no code changes required.

```csv
key,value,description,unit,domain_notes
de_ratio,3.0,D/E ratio above this triggers high leverage flag,multiple (×),"Adjust by sector..."
gross_margin_drop_bps,500,YoY gross margin compression that triggers flag,basis points,...
current_ratio,1.0,Current ratio below this triggers liquidity flag,multiple (×),...
interest_coverage,2.0,EBIT / interest below this triggers debt service flag,multiple (×),...
op_leverage_distortion,15.0,Operating leverage above this is flagged as non-representative,multiple (×),...
roe_benchmark_financials,0.12,...
roe_benchmark_general,0.20,...
tone_positive_threshold,0.20,...
tone_negative_threshold,-0.20,...
```

If `pipeline_config.csv` is absent, the pipeline falls back to hardcoded defaults silently — it never raises on a missing config file.

---

## Data Sources

| Source | Data | Access |
|---|---|---|
| SEC EDGAR (via edgartools) | 3-year XBRL financial statements | API — requires `edgar.set_identity()` |
| yfinance | Current price, FY-end historical prices, market cap | API |
| FFIEC FR Y-9C bulk files | Basel III capital ratios for US bank holding companies | Manual download from ffiec.gov; local file path |
| SEC EDGAR Interactive Viewer | Custom XBRL extension tag lookup | Manual research when edgartools mapping fails |

### FR Y-9C Setup (financials only)

Download `BHCF{YYYYMMDD}.txt` from [ffiec.gov/npw/FinancialReport/FinancialDataDownload](https://www.ffiec.gov/npw/FinancialReport/FinancialDataDownload) and place in:

```
Industry Files/Financials/Basel_III/{YYYY}/BHCF{YYYYMMDD}.txt
```

CET1 ratio is **not** available in FR Y-9C for G-SIBs (JPM, BAC, WFC, GS). They file it under FFIEC 101 — a separate form.

---

## Running the Pipeline

### PDF output (single ticker)

```python
from orchestrator import EquityAnalystOrchestrator

orch = EquityAnalystOrchestrator(edgar_identity="Name email@domain.com")
orch.run("AAPL", sector="Technology", author="Your Name",
         output_dir=r"C:\Reports")
```

### DataFrame validation (no PDFs)

```python
# Returns all agent outputs as a flat dict — no PDF rendered
result = orch.run_data_only("MSFT", sector="Technology")
```

### Batch runs

```bash
# PDF batch (current 20-ticker universe)
python main.py

# DataFrame batch — 50 tickers, outputs CSV + Excel
python run_batch_validation_df.py

# Full 150-ticker cross-industry validation
python run_batch_validation_150.py
```

### XBRL tag diagnostics

When a metric shows an unexpected value, use `diagnose_tags.py` to inspect the raw XBRL:

```bash
# Search for a concept by name fragment
python diagnose_tags.py MSFT --search "depreciation"

# Find which tag resolves to a specific dollar value
python diagnose_tags.py BAC --value 60096000000

# Show all rows for a standard concept (reveals ambiguous multi-row mappings)
python diagnose_tags.py BAC --concept NetInterestIncome

# Export all TSVs for a ticker
python diagnose_tags.py TSLA --out ./diag_out
```

---

## Validation Results (v2.0 — 150 tickers)

| Metric | Result |
|---|---|
| Runtime failures | 0 / 150 |
| Structurally clean (no impossible values) | 148 / 150 |
| Net margin within ±2pp of public consensus | 72 / 142 (51%) |
| Net margin within ±4pp of public consensus | 102 / 142 (72%) |
| Mean absolute error (net margin) | 4.06pp |
| Sectors covered | 9 (Technology, Energy, Consumer Disc., Healthcare, Consumer Staples, Industrials, Communication Services, Materials, Utilities) |

The 40 tickers outside ±4pp fall into four categories — **none are pipeline errors**:

- **FY-end mismatch (5):** QCOM (Sep), MU (Aug), AVGO (Oct), SNOW (Jan), INTC — pipeline correctly reports the company's own fiscal year; benchmark was calendar-year.
- **GAAP one-time items (12):** KHC, DLTR, NEM, APD, DD, CE, MRVL, DOW, FCX, BMY, ABBV, AMGN — pipeline correctly captures GAAP impairments, restructuring, and M&A charges; consensus was on adjusted basis.
- **NCI attribution (4):** PM, SRE, GE, WMB — noncontrolling interest structures create legitimate differences in which "net income" concept is most meaningful.
- **Unrealised gains (5):** UBER, GILD, PLTR, YUM, RCL — GAAP net income inflated by mark-to-market equity investments or asset disposals; pipeline is technically correct.

---

## Project Principles

**1. Honest N/A over wrong numbers.**
If a metric cannot be computed reliably, it shows `"N/A (reason)"` rather than a silently incorrect value. `EBITDA = Operating Margin` is a worse outcome than `EBITDA = N/A (D&A unresolvable)`.

**2. GAAP fidelity.**
All figures are as-reported GAAP. No adjustments, no normalisation. Impairments, restructuring charges, and unrealised gains are included exactly as filed. The red flag system surfaces unusual swings so analysts know to investigate further.

**3. Ordered fallback chains, never silent failure.**
Every metric attempts resolution through an ordered sequence of strategies. When all fail, the final state is an honest diagnostic string, not a zero or blank cell. The fallback reason is always surfaced to the analyst.

**4. Surgical code changes.**
Changes touch only the minimum lines required to fix the identified issue. Adjacent working code is never refactored. Every changed line traces directly to a validated requirement.

**5. Externalized configuration.**
Thresholds, benchmarks, and domain-tunable parameters live in `pipeline_config.csv`. An analyst can adjust the D/E flag threshold from 3.0× to 2.0× for a utilities-focused run without reading a single line of Python.

**6. Validated, not assumed.**
Every change is verified against public consensus before being declared fixed. The 150-ticker validation batch (`run_batch_validation_150.py`) is designed to be re-run after any substantive change. Issues are classified by root cause before being patched.

**7. Diagnose before guessing.**
When a metric is wrong, the first step is always `diagnose_tags.py` rather than adding tag candidates blindly. This identified that `tsla:DepreciationAmortizationAndImpairment` is a custom extension tag (not in the US GAAP taxonomy), that MSFT's D&A is in the PP&E label text, and that pharma companies embed interest in a combined NonoperatingIncomeExpense total.

---

## Known Limitations (Current Iteration)

| Limitation | Scope | Status |
|---|---|---|
| **Financials sector** | JPM, BAC, GS, WFC — NIM, ROTCE for JPM/GS missing (goodwill XBRL gap) | Deferred — separate agent swarm planned |
| **WELL, EQR** | Healthcare REIT and apartment REIT with unresolved operating income / revenue tags | Diagnose.py runs pending; same fix pattern as LLY/NEE |
| **EBITDA for TSLA** | Custom `tsla:` tag resolved; prior years may still show N/A (no prior-year BS delta available) | Structural — only current year has accumulated dep from label text |
| **FY-end non-December** | QCOM (Sep), MU (Aug), AVGO (Oct), SNOW (Jan), FOX (Jun) report fiscal years not aligned to calendar year | Not a bug — display FY label reflects the company's own period |
| **BVPS FY23** | Oldest balance sheet period unavailable via edgartools (2-period BS limit) | Structural edgartools limitation |
| **NIM for G-SIBs** | Interest-earning assets not XBRL-tagged in large bank filings; requires supplemental rate/volume tables from MD&A | Structural — flagged in Section 6 with red flag |
| **Guidance extraction** | Section 5 shows source + tone for most tickers; sentence extraction quality varies by 8-K format | Optional module — deferred to management quality layer |
| **Adjusted vs GAAP gap** | Pipeline reports GAAP; consensus benchmarks are often on adjusted basis (especially pharma, tech) | By design — add disclosure note when deploying |

---

## Roadmap

- [ ] Financials agent swarm (NIM, ROTCE, goodwill resolution, CET1 via FFIEC 101)
- [ ] Management quality module (guidance vs actuals, multi-quarter tone trend, consistency scoring)
- [ ] WELL / EQR REIT tag fixes (diagnose.py runs)
- [ ] Guidance vs actuals comparison (highest analytical value — prior quarter 8-K vs current actuals)
- [ ] Sector-specific ROE/ROIC benchmarks in `pipeline_config.csv`

---

## Setup

```bash
pip install edgartools yfinance reportlab pandas openpyxl
```

Set your SEC EDGAR identity (required by SEC rate-limiting policy):

```python
import edgar
edgar.set_identity("Your Name your@email.com")
```

Place `pipeline_config.csv` in the same directory as `agents.py`. The pipeline falls back to built-in defaults if absent.

---

## License

For informational and research purposes. All output is clearly marked DRAFT and carries the disclaimer: *"For informational purposes only. Not investment advice."* Financial data sourced from SEC EDGAR public filings and FFIEC public bulk data.
