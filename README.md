# Equity Analyst Pipeline

Automated equity research pipeline that generates institutional-style PDF briefs from SEC EDGAR filings and live market data. Parses XBRL financial statements through five specialist agents — fundamental analysis, risk & solvency, valuation, trend commentary, and management guidance — with sector-specific ratio filtering for banks, E&P, and general industries.

**Validated across 150 tickers, 9 sectors, 0 runtime failures.**

---

## Table of Contents

1. [Overview](#overview)
2. [What It Produces](#what-it-produces)
3. [Quick Start](#quick-start)
4. [File Structure](#file-structure)
5. [Pipeline Architecture](#pipeline-architecture)
6. [Sector Routing](#sector-routing)
7. [Agents](#agents)
8. [Output — PDF Brief](#output--pdf-brief)
9. [Red Flag Format](#red-flag-format)
10. [Configuration](#configuration)
11. [Data Sources](#data-sources)
12. [Required Manual Setup](#required-manual-setup)
13. [Batch Runner](#batch-runner)
14. [Diagnostics](#diagnostics)
15. [Validation Results](#validation-results)
16. [Project Principles](#project-principles)
17. [Known Limitations](#known-limitations)
18. [Roadmap](#roadmap)
19. [Dependencies](#dependencies)

---

## Overview

Given a ticker, the pipeline:

1. Fetches the company's latest 10-K from SEC EDGAR via XBRL
2. Parses income statement, balance sheet, and cash flow statement into typed profiles (5 years of data)
3. Routes data through five analyst agents that compute sector-appropriate ratios
4. Fetches live market data, price context, analyst targets, and estimate revisions from Yahoo Finance
5. Fetches earnings call transcripts and extracts management guidance, tone, and credibility scoring
6. Fetches insider transactions (Form 4), institutional ownership (13-F), and short interest
7. Fetches debt maturity profile and computes WACC from live ERP, OAS spreads, and CAPM
8. Renders a structured multi-page PDF equity brief

No paid data terminals. Everything runs from public SEC filings, Yahoo Finance, FRED, and FFIEC bulk data.

---

## What It Produces

Each run outputs a multi-page PDF and/or a structured DataFrame row containing:

| Section | Content |
|---|---|
| Executive Summary | Key bullets: valuation, fundamentals, management, top risk, insider activity |
| Analyst Price Targets | Current price vs sell-side low / median / mean / high; upside to mean |
| Price Context | 52w range, SMA 30/90/200d, volume vs 90d avg |
| Return vs Peers & Benchmarks | 1m / 3m / 6m / 12m / 3y / 5y vs SPY, sector ETF, peer percentile rank |
| Estimate Revisions | EPS and revenue consensus trend over 30 / 60 / 90 days |
| 1 · Fundamental Analysis | Gross margin, operating margin, EBITDA margin, net margin, ROE, ROA, ROIC, asset turnover, inv. turnover, FCF margin, revenue CAGR, operating leverage (5 years) |
| 2 · Risk & Solvency | Current ratio, quick ratio, D/E ratio, net debt/EBITDA, interest coverage, Altman Z-Score, credit ratings, WACC components, debt maturity profile |
| 3 · Valuation | P/E (FY-end and current price), P/B, P/TBV, EV/EBITDA, EV/Sales; peer comparison table |
| Basel III (financials) | Tier 1 capital ratio, total capital ratio, T1 leverage ratio — sourced from FFIEC FR Y-9C bulk files |
| 4 · Red Flags | Structured flags with company value, reference threshold, and one-phrase consequence |
| 5 · Trend Commentary | Deterministic narrative generated from ratio deltas |
| 6 · Management Guidance & Tone | Transcript source, tone classification, risk factors, results, growth outlook |
| 7 · Management Track Record | Credibility score, guidance accuracy (40%), execution quality (40%), communication score (20%) |
| 8 · Insider & Institutional Information | 13-F institutional holders, Form 4 insider transactions, short interest |

---

## Quick Start

### Prerequisites

```bash
pip install -r requirements.txt
```

Python 3.10+ required.

### Run a single report — any ticker

```bash
# From project root
python run.py AAPL
python run.py JPM
python run.py TSM      # works for any SEC-registered company
```

### Run the default MAG7 batch

```bash
python run.py
```

PDFs are saved automatically to `~/Desktop/EquityReports/`. No path configuration needed.

> **SEC identity required.** Set `EDGAR_IDENTITY` in `core/config.py` as `"Your Name your@email.com"`. This is a courtesy requirement for SEC rate limiting, not authentication.

### DataFrame output (no PDF)

```python
from core.orchestrator import EquityAnalystOrchestrator

orch = EquityAnalystOrchestrator(edgar_identity="Your Name your@email.com")
result = orch.run_data_only("MSFT", sector="Technology")
```

---

## File Structure

```
EquityAnalystReport/
│
├── run.py                     # Single entry point — python run.py [TICKER]
│
├── core/                      # Pipeline brain
│   ├── orchestrator.py        # run() → PDF, run_data_only() → DataFrame
│   ├── agents.py              # Five analyst agents + sector routing + red flag logic
│   ├── data_layer.py          # XBRL ingestion, StatementProfile classes, fallback chains
│   ├── renderer.py            # ReportLab PDF renderer
│   └── config.py              # EDGAR identity, paths, global settings
│
├── ingestion/                 # Everything that fetches raw data
│   ├── edgar_transcript.py
│   ├── transcript_loader.py
│   ├── fool_transcript_db.py
│   ├── debt_note_fetcher.py
│   ├── xbrl_debt_fetcher.py
│   ├── ownership_loader.py
│   ├── insider_transactions.py
│   └── short_interest_loader.py
│
├── processing/                # Data cleaning & enrichment
│   ├── facts_processor.py
│   ├── filing_cache.py
│   ├── query_cache.py
│   ├── quarterly_processor.py
│   └── enrich_waterfall.py
│
├── market/                    # Live market data
│   ├── price_context.py       # 52w range, SMA, volume
│   ├── momentum.py            # Return vs peers and benchmarks
│   ├── analyst_targets.py     # Sell-side price targets
│   ├── estimate_revisions.py  # EPS and revenue estimate trends
│   ├── erp_fetcher.py         # Damodaran ERP (auto-downloaded)
│   ├── fred_client.py         # OAS credit spreads via FRED
│   └── peer_comparator.py     # Peer comparison table (auto-derived from yfinance)
│
├── universe/                  # Ticker universe builders
│   ├── ticker_universe.py
│   ├── ticker_universe_20f.py
│   ├── ticker_universe_sec.py
│   └── build_sec_universe.py
│
├── financials/                # Sector-specific financial modules
│   ├── fr_y9c.py              # FR Y-9C Basel III capital ratio fetcher
│   ├── company_overview.py
│   ├── export_metrics.py
│   └── guidance_tracker.py
│
├── utils/                     # Helpers & standalone tools
│   ├── finbert_label.py
│   ├── tune_tone.py
│   ├── query_ownership.py
│   ├── validate_pipeline.py
│   └── diagnose_tags.py       # XBRL tag diagnostic utility
│
├── runners/                   # Batch scripts
│   ├── main.py                # MAG7 batch runner
│   └── run_insider_validation_insider_transaction.py
│
├── data/                      # Local data files — see Required Manual Setup
│   ├── config/
│   │   ├── pipeline_config.csv        # Tunable thresholds (committed)
│   │   └── us_gaap_debt_tags.csv      # XBRL tag reference (committed)
│   ├── financials/
│   │   └── Basel_III/                 # FR Y-9C files — manual download required
│   └── cache/                         # Auto-generated at runtime — not committed
│
├── ratings_template.csv       # Copy to ratings.csv and fill before running
├── requirements.txt
└── README.md
```

---

## Pipeline Architecture

```
Ticker (any SEC-registered company)
      │
      ▼
┌──────────────────────────────────────────┐
│  RobustDataProcessor (core/data_layer)   │  ← SEC EDGAR 10-K ingestion
│  XBRL → 5-year statement DataFrames      │    IncomeStatement + BalanceSheet + CashFlow
└──────────────────┬───────────────────────┘
                   │
     ┌─────────────┼──────────────────────────────────┐
     ▼             ▼                                  ▼
  market/       ingestion/                       financials/
  price_context  transcripts                     fr_y9c (banks)
  momentum       insider_transactions            guidance_tracker
  analyst_targets ownership_loader
  estimate_revisions short_interest
  erp_fetcher
  fred_client
     │             │                                  │
     └─────────────┼──────────────────────────────────┘
                   ▼
    ┌──────────────────────────────────────────────────┐
    │            Five Agents (core/agents.py)           │
    │                                                  │
    │  FundamentalAgent    → margins, returns,         │
    │                        turnover, FCF, CAGR       │
    │  RiskAgent           → leverage, liquidity,      │
    │                        Altman Z, WACC            │
    │  ValuationAgent      → P/E, P/B, EV/EBITDA,     │
    │                        P/TBV, peer comps         │
    │  TrendCommentaryAgent → deterministic narrative, │
    │                         red flags                │
    │  GuidanceAgent       → transcript tone,          │
    │                         credibility scoring      │
    └──────────────────────┬───────────────────────────┘
                           │
                  ┌────────▼──────────┐
                  │ EquityBriefRenderer │  ← ReportLab PDF
                  │  (core/renderer)    │    multi-page output
                  └───────────────────┘
```

### Data layer — XBRL tag resolution

EDGAR XBRL data is inconsistent across filers. The data layer handles this through:

- **`_get_vector(tags)`** — tries each tag in order, returns the first non-zero match
- **`_get_max_vector(tags)`** — returns the row with the largest value across all matches. Used when companies file segment subtotals before the consolidated total (e.g. total assets, goodwill, operating cash flow, equity)
- **Fallback chains** — each property tries multiple standard concept names before returning zeros
- **5-year history** — balance sheet, income statement, and cash flow data retrieved across 5 fiscal years

### Key fallback chains

**D&A (for EBITDA computation)**
```
1. CashFlowProfile._get_max_vector([
       'DepreciationAmortizationCF', 'DepreciationExpense',
       'DepreciationAndAmortization', 'DepreciationDepletionAndAmortization',
       'DepreciationAmortizationAndAccretionNet',   ← MSFT
       'OtherDepreciationAndAmortization', 'Depreciation',
   ])
2. IncomeStatementProfile._get_vector(['DepreciationExpense', 'AmortizationOfIntangibles'])
3. Balance sheet delta — PP&E accumulated depreciation change + net intangibles decline
4. Raw XBRL extension tags — e.g. tsla:DepreciationAmortizationAndImpairment (Tesla custom tag)
5. → "N/A (D&A unresolvable)"
```

**Revenue**
```
1. 'Revenue'
2. 'Revenues'
3. 'RegulatedAndUnregulatedOperatingRevenue'   ← diversified utilities
4. 'ElectricUtilityRevenue'
5. 'RegulatedUtilityRevenue'
```

**Diluted shares (for EPS → P/E)**
```
_get_max_vector(['SharesFullyDilutedAverage', 'SharesAverage', ...])
Why max: some filers map both basic and diluted share counts to the same
standard_concept; max picks the diluted (larger) figure correctly.
Fallback 1: market_cap / current_price when XBRL tag absent
Fallback 2: ×1,000,000 unit correction when shares filed in millions
```

**Gross margin guards**
```
Collapse guard: round(gm_val, 4) == round(op_income / revenue, 4)
→ "N/A (not reported separately)"
Fires for franchise/fee models (MCD, SBUX) and aerospace (BA) where
GrossProfit resolves to the same value as OperatingIncomeLoss.

Overflow guard: gm_val > 1.0
→ "N/A (tag overflow)"
Fires for DIS-class where GrossProfit resolves to a segment subtotal
larger than consolidated revenue.
```

---

## Sector Routing

The pipeline auto-detects sector from SEC EDGAR. Four routing groups:

| Input sector | Routed to | Behaviour |
|---|---|---|
| `"Financials"`, `"Banking"`, `"Insurance"` | `financials` | NIM, Efficiency Ratio, ROTCE, Basel III table; suppresses Gross Margin, Altman Z, Interest Coverage |
| `"Energy"`, `"Oil"`, `"Gas"`, `"Mining"` | `energy` | Op Cost Ratio, EBITDA Margin, Debt/Capital, EV/EBITDA; suppresses Inv. Turnover, Altman Z |
| `"Utilities"`, `"Utility"` | `utilities` | Full general metric set; suppresses Altman Z; uses CF interest as fallback |
| Everything else | `general` | Complete ratio set: Gross Margin, DSI, FCF Margin, EV/EBITDA, Altman Z |

> **Note:** `"Real Estate"` routes to `general`. REITs receive the full ratio set. Altman Z will score low due to structural leverage — this is expected, not a data error.

---

## Agents

### FundamentalAgent

Computes profitability and efficiency ratios from the income statement and balance sheet across 5 fiscal years. Output is sector-filtered — rows not meaningful for the sector are suppressed rather than shown as N/A.

| Metric | general | financials | energy |
|---|---|---|---|
| Gross Margin | ✓ | NIM instead | Op Cost Ratio |
| Operating Margin | ✓ | suppressed | ✓ (back-calc) |
| EBITDA Margin | ✓ | suppressed | ✓ |
| Net Margin | ✓ | ✓ | ✓ |
| ROE / ROA | ✓ | ✓ | ✓ |
| ROIC | ✓ | suppressed | ✓ |
| Asset Turnover | ✓ | ✓ | ✓ |
| Inv. Turnover + DSI | ✓ | suppressed | suppressed |
| FCF Margin | ✓ | suppressed | ✓ |
| Revenue CAGR | ✓ | ✓ | ✓ |
| Operating Leverage | ✓ | suppressed | ✓ |
| Efficiency Ratio | — | ✓ | — |
| ROTCE | — | ✓ | — |
| Basel III (Tier 1, Total, Leverage) | — | ✓ | — |

DSI and Inv. Turnover are additionally suppressed when net inventory ≤ 0 (customer advance payments exceed gross inventory — common in aerospace) or COGS ≤ 0 (contract loss provisions).

### RiskAgent

Computes leverage, solvency, and cost-of-capital metrics.

- **Altman Z-Score** — shown for general sector only. Suppressed for financials (not calibrated for banks) and energy (asset-heavy E&P companies structurally score low without being distressed).
- **Debt/Capital** — shown for energy sector as a complement to D/E; more standard in E&P analysis.
- **Current Ratio, Quick Ratio, Interest Coverage** — suppressed for financials.
- **WACC** — computed from live inputs: CAPM cost of equity (Damodaran ERP + beta), rating-implied cost of debt (risk-free rate + OAS spread from FRED), capital structure weights.
- **Debt Maturity Profile** — sourced from SEC XBRL; shows year-by-year scheduled repayments and weighted average maturity.

### ValuationAgent

Computes multiples using both historical FY-end prices (fetched from Yahoo Finance) and current market price.

- **Peer Comparison** — auto-derived industry peers from yfinance; shows P/E, EV/EBITDA, P/B, ROE, Revenue Growth at subject vs peer median with percentile ranking.
- **EV/EBITDA** — shown for general and energy; suppressed for financials.
- **Shares fallback chain** — diluted shares → basic shares → market cap / price derivation → unit correction for shares filed in millions.

### TrendCommentaryAgent

Generates a deterministic narrative from period-over-period ratio deltas. Does not call any LLM — all output is rule-based. Also:

- Detects red flags against configurable thresholds in `pipeline_config.csv`
- Every red flag includes: company value, reference threshold, and a one-phrase consequence
- Operating leverage flagged as non-representative (not suppressed) when `|ratio| > 15×`
- Sector-aware commentary blocks — banks get NIM compression narrative, energy gets cost ratio commentary

### GuidanceAgent

Processes earnings call transcripts and 8-K filings.

- **Tone classification** — Confident / Cautious via FinBERT sentiment
- **Credibility scoring** — guidance vs actuals comparison across prior quarters (0–100 scale)
- **Management Track Record** — Guidance Accuracy (40%), Execution Quality (40%), Communication Quality (20%)
- **Attribution analysis** — % of miss attribution classified as external vs internal factors

---

## Output — PDF Brief

Multi-page PDF per ticker generated by ReportLab. All sections are dynamic — rows where all data cells are N/A are automatically suppressed.

**Cover & context** (always present):
- Cover header: ticker, sector, market cap, report date, author, DRAFT watermark
- Business summary
- Executive Summary — key bullets across all sections
- Analyst Price Targets — current price vs sell-side low / median / mean / high
- Price Context — 52w range, SMA 30/90/200d, volume vs 90d avg
- Return vs Peers & Benchmarks — 1m / 3m / 6m / 12m / 3y / 5y vs SPY, sector ETF, peer percentile
- Estimate Revisions — EPS and revenue consensus trend over 30 / 60 / 90 days

**Core analysis sections**:
- Section 1 — Fundamental Analysis (5 years)
- Section 2 — Risk & Solvency (5 years) including WACC and debt maturity profile
- Section 3 — Valuation (5 years) including peer comparison table
- Section 4 — Red Flags
- Section 5 — Trend Commentary
- Section 6 — Management Guidance & Tone
- Section 7 — Management Track Record
- Section 8 — Insider & Institutional Information (13-F, Form 4, short interest)

---

## Red Flag Format

Every flag includes the company's actual value, the reference threshold, and a one-phrase consequence:

```
■ [Metric] [company value] ([period]) [direction] [threshold] — [consequence]
```

Examples:

```
■ Current ratio 0.60x (FY25) below 1.0x threshold — current liabilities exceed current assets
■ D/E ratio increasing: 1.38x (FY25) vs 1.22x (FY24) — re-leveraging trend
■ Altman Z-Score 1.43 (FY25) in distress zone (below 1.81) — elevated default risk
■ Interest coverage 1.54x (FY25) below 2.0x threshold — EBIT barely covers debt service
■ Operating leverage non-representative: 36.33x — GAAP op. margin swung on a low base
■ Tone/fundamentals divergence — Confident tone vs FCF margin 2.09%
```

Operating leverage is flagged as non-representative (rather than suppressed) when `|ratio| > 15x`. The number remains visible in Section 1 with full context in the red flag.

---

## Configuration

All thresholds and benchmarks live in `data/config/pipeline_config.csv`. Edit the `value` column to adjust when flags fire — no code changes required.

```
key,value,description
de_ratio,3.0,D/E ratio above this triggers high leverage flag
gross_margin_drop_bps,500,YoY gross margin compression that triggers flag
current_ratio,1.0,Current ratio below this triggers liquidity flag
interest_coverage,2.0,EBIT / interest below this triggers debt service flag
op_leverage_distortion,15.0,Operating leverage above this flagged as non-representative
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
| SEC EDGAR (via edgartools) | 5-year XBRL financial statements | API — requires `edgar.set_identity()` |
| yfinance | Current price, FY-end historical prices, market cap, analyst targets, estimate revisions, peers, 13-F ownership | API — free |
| FRED (ICE BofA OAS) | Credit spreads by rating tier for cost-of-debt | API — free |
| Damodaran (NYU Stern) | Implied equity risk premium — auto-downloaded and cached | Public URL — auto-refreshed |
| Motley Fool / EDGAR 8-K | Earnings call transcripts | Scraped / SEC EDGAR |
| FFIEC FR Y-9C bulk files | Basel III capital ratios for US bank holding companies | Manual download from ffiec.gov |
| SEC EDGAR Form 4 | Insider transactions | API via edgartools |

---

## Required Manual Setup

### 1. ratings.csv (all sectors)

Credit ratings are used for cost-of-debt calculation (OAS spread selection) and the credit quality section.

```bash
cp ratings_template.csv ratings.csv
```

Edit `ratings.csv` — columns: `ticker, sp_rating, moodys_rating, fitch_rating, outlook, as_of_date`

Pipeline falls back to `N/A` per ticker if file or row is missing.

> **ratings.csv is committed to the repo** and manually maintained.
> Update it when ratings change (typically quarterly).
> The file is the source of truth for cost-of-debt calculations —
> an outdated rating will affect WACC and OAS spread selection.
> Pipeline falls back to N/A per ticker if a row is missing.

### 2. Basel III FR Y-9C files (financials sector only)

Required for Tier 1, Total Capital, and Tier 1 Leverage ratios for US bank holding companies (JPM, BAC, WFC, GS, etc.). These ratios are not available in SEC XBRL.

- Download `BHCF{YYYYMMDD}.txt` from: https://www.ffiec.gov/npw/FinancialReport/FinancialDataDownload
- Also download `XML_ATTRIBUTES_ACTIVE.XML` from the FFIEC NIC family (same page)
- Place under: `data/financials/Basel_III/{YYYY}/BHCF{YYYYMMDD}.txt`

Pipeline degrades gracefully to `N/A` if files are absent.

> **Note:** CET1 ratio is not available in FR Y-9C for G-SIBs (JPM, BAC, WFC, GS). They file it under FFIEC 101 — a separate form not currently ingested by this pipeline.

### 3. pipeline_config.csv (included in repo)

Already committed. Edit `data/config/pipeline_config.csv` to tune red-flag thresholds without touching code.

---

## Batch Runner

Default batch runs MAG7 tickers. Output goes to `~/Desktop/EquityReports/` automatically.

```bash
# MAG7 batch
python run.py

# Single ticker — any SEC-registered company
python run.py AAPL
python run.py JPM
python run.py TSM
```

A configurable delay between runs (default 8 seconds) prevents SEC rate limiting. Failed tickers are logged and skipped — the batch continues regardless.

---

## Diagnostics

`utils/diagnose_tags.py` inspects the raw XBRL DataFrames for a given ticker to identify what standard concept tags are actually filed. Use this when a metric shows N/A unexpectedly.

```bash
# Search for a concept by name fragment
python utils/diagnose_tags.py MSFT --search "depreciation"

# Find which tag resolves to a specific dollar value
python utils/diagnose_tags.py BAC --value 60096000000

# Show all rows for a standard concept
python utils/diagnose_tags.py BAC --concept NetInterestIncome

# Export all TSVs for a ticker
python utils/diagnose_tags.py TSLA --out ./diag_out
```

---

## Validation Results

Validated across 150 tickers, 9 sectors, 0 runtime failures.

| Metric | Result |
|---|---|
| Runtime failures | 0 / 150 |
| Structurally clean (no impossible values) | 148 / 150 |
| Net margin within ±2pp of public consensus | 72 / 142 (51%) |
| Net margin within ±4pp of public consensus | 102 / 142 (72%) |
| Mean absolute error (net margin) | 4.06pp |
| Sectors covered | 9 (Technology, Energy, Consumer Disc., Healthcare, Consumer Staples, Industrials, Communication Services, Materials, Utilities) |

The 40 tickers outside ±4pp fall into four categories — none are pipeline errors:

- **FY-end mismatch** — QCOM (Sep), MU (Aug), AVGO (Oct), SNOW (Jan); pipeline correctly reports the company's own fiscal year; benchmark was calendar-year
- **GAAP one-time items** — KHC, DLTR, NEM, APD and others; pipeline captures GAAP impairments and restructuring charges; consensus was on adjusted basis
- **NCI attribution** — PM, SRE, GE, WMB; noncontrolling interest structures create legitimate definitional differences
- **Unrealised gains** — UBER, GILD, PLTR, YUM, RCL; GAAP net income inflated by mark-to-market equity investments; pipeline is technically correct

---

## Project Principles

**1. Honest N/A over wrong numbers.** If a metric cannot be computed reliably, it shows `"N/A (reason)"` rather than a silently incorrect value. `EBITDA = Operating Margin` is a worse outcome than `EBITDA = N/A (D&A unresolvable)`.

**2. GAAP fidelity.** All figures are as-reported GAAP. No adjustments, no normalisation. Impairments, restructuring charges, and unrealised gains are included exactly as filed. The red flag system surfaces unusual swings so analysts know to investigate further.

**3. Ordered fallback chains, never silent failure.** Every metric attempts resolution through an ordered sequence of strategies. When all fail, the final state is an honest diagnostic string, not a zero or blank cell.

**4. Surgical code changes.** Changes touch only the minimum lines required to fix the identified issue. Adjacent working code is never refactored. Every changed line traces directly to a validated requirement.

**5. Externalised configuration.** Thresholds, benchmarks, and domain-tunable parameters live in `pipeline_config.csv`. An analyst can adjust flag thresholds without reading a single line of Python.

**6. Validated, not assumed.** Every change is verified against public consensus before being declared fixed. Issues are classified by root cause before being patched.

**7. Diagnose before guessing.** When a metric is wrong, the first step is always `diagnose_tags.py` rather than adding tag candidates blindly.

---

## Known Limitations

| Issue | Affected Tickers | Status |
|---|---|---|
| ROTCE and other financial industry specifics nuance not computable | Financial Industry | Deferred — financials agent swarm planned |
| Credit ratings stale risk | All tickers | ratings.csv is manually maintained — ratings are not fetched automatically. Update quarterly or after any rating action. |

> **Note:** Balance sheet history limitation has been resolved — pipeline retrieves up to 5 years of data. Financials sector edge cases are deferred to the next release.

---

## Roadmap

- [ ] Financials agent swarm (NIM, ROTCE, goodwill resolution, CET1 via FFIEC 101)
- [ ] Management quality module (guidance vs actuals multi-quarter trend, consistency scoring)
- [ ] GS / investment bank sector routing (gross vs net revenue detection)
- [ ] Sector-specific ROE / ROIC benchmarks in `pipeline_config.csv`
- [ ] DCF / NAV valuation agent
- [ ] Forensic agent (Beneish M-Score, accruals ratio)

---

## Dependencies

```
edgartools        # SEC EDGAR ingestion and XBRL parsing
yfinance          # Market prices, analyst targets, estimate revisions, peers, ownership
reportlab         # PDF generation
pandas
numpy
requests          # ERP and FRED data fetching
transformers      # FinBERT sentiment (GuidanceAgent)
torch             # FinBERT backend
```

See `requirements.txt` for pinned versions. Python 3.10+ required.

---

## Acknowledgements

This project was developed with [Claude](https://claude.ai) (Anthropic), which assisted with code implementation, debugging, and documentation throughout the development process.

---

*For informational and research purposes only. All output is clearly marked DRAFT and carries the disclaimer: "For informational purposes only. Not investment advice."*
