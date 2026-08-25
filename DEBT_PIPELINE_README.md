# Debt & Credit Quality Pipeline — README
_Last updated: 2026-06-30_

---

## Current State

**86% coverage** across S&P 500 + Nasdaq 100 + Dow 30 (519 tickers, 441 fully reportable).

| Method | Count | % |
|---|---|---|
| Tranche avg (debt note parser) | 204 | 40% |
| IS-derived (XBRL IE ÷ total debt) | 237 | 46% |
| No debt data (debt-free or foreign filer) | 41 | 8% |
| Debt present but no Kd | 23 | 4% |

Zero errors on last full run.

---

## Pipeline Status — 2026-06-30

All sections validated. Production-ready for buy-side self-use and light professional use.

| Section | Status | Notes |
|---|---|---|
| Fundamental Analysis | ✅ Production | 3-year margins, returns, turnover |
| Risk & Solvency | ✅ Production | Ratios, Altman Z, leverage trend |
| Credit Quality — ratings/WACC | ✅ Production | OAS live via FRED, ERP via Damodaran |
| Credit Quality — tranche table | ✅ Production | 9 formats, 40% universe coverage |
| Credit Quality — maturity profile | ✅ Production | WAM, lumpiness flags, heat circles |
| Valuation | ✅ Production | Historical + current multiples, WACC |
| Red Flags | ✅ Production | Margin compression, leverage, coverage |
| Management Guidance & Tone | ✅ Production | Transcript extraction, targets, actuals |
| Management Track Record | ✅ Production | Scoring, beat/miss, execution quality |
| Institutional Ownership | ✅ Production | Top-10 holders, 13-F sourced |
| Guidance period mismatch | ⚠️ Known gap | Annual guidance vs quarterly actual shows in table — suppress rows in next session |

**Financials excluded upstream** — banks, insurers, asset managers are filtered at the orchestrator level and never reach the debt pipeline. All debt metrics shown are for non-financial companies only.

---

## Files

| File | Purpose |
|---|---|
| `xbrl_debt_fetcher.py` | SEC XBRL API — total debt, maturity schedule, interest expense |
| `debt_note_fetcher.py` | 10-K text parser — tranche rates, formats A–N |
| `agents.py` | `_compute_credit_quality()`, `_compute_is_cost_of_debt()`, `_compute_wacc()` |
| `run_debt_validation.py` | Batch validation script — 519 tickers → `debt_summary.txt` |
| `renderer.py` | PDF renderer — Credit Quality section incl. maturity wall, heat circles, tranche table |
| `ratings.csv` | Manual credit ratings table — ticker, S&P, Moody's, Fitch |

---

## Domain Nuances — Future Development

These are structural issues that require debt market domain knowledge to fix correctly.
They are documented here rather than in code because the right solution is non-obvious
and getting it wrong risks introducing silent errors across the universe.

### 1. Total debt definition varies by provider

Our pipeline reads `LongTermDebt` from XBRL — this is the carrying value of notes payable,
net of discount and issuance costs, as reported on the balance sheet. Different providers
define "total debt" differently:

| What's included | Our pipeline | MacroTrends | Bloomberg |
|---|---|---|---|
| LT notes / bonds | ✓ | ✓ | ✓ |
| Current portion of LT debt | Sometimes | No | Yes |
| Commercial paper / CP | Sometimes | No | Yes |
| Finance lease liabilities | Sometimes (XBRL tag overlap) | No | Yes |
| Operating lease liabilities | No | No | No |

**Practical effect:** Our figures typically run 5–20% above MacroTrends for companies
with large CP programs (WMT, UNH) or finance leases (AMZN). Neither is wrong —
they answer different questions. For WACC the carrying value of interest-bearing
notes is the right denominator; for credit analysis total financial obligations
(including leases) is more conservative.

**Future fix:** Add a `debt_definition` field to the output (`notes_only` vs `including_leases`)
derived from which XBRL tag fired. Let the renderer display a footnote when leases
are likely included.

---

### 2. Commercial paper creates timing noise

CP is short-dated (typically 30–90 days) and balance varies day to day. XBRL captures
whatever was outstanding at the 10-K filing date, which may not reflect the average
or peak balance. Companies like CVX, XOM, LLY, WMT run large CP programs to fund
working capital — their CP balance on any given day can swing $5–15B.

**Current behaviour:** CP fires through `ShortTermBorrowings` in the waterfall.
For some tickers (CVX, LLY) the CP is tagged under a non-standard concept we haven't
mapped yet, causing the ~$2–8B understatement in those tickers.

**Future fix:** Add `CommercialPaper` and `UnsecuredDebt` to `_ST_BORROWINGS` waterfall.
Run the XBRL probe script (see Known Issues section) on CVX and LLY to confirm.

---

### 3. Carrying value vs face value

XBRL `LongTermDebt` reports the **carrying value** — face value minus unamortized
discount and debt issuance costs. For most investment-grade issuers the difference
is small (0.5–2%). For deep-discount or zero-coupon notes it can be material.

For cost of debt purposes the **stated coupon on face value** is correct (tranche avg).
For balance sheet leverage ratios the **carrying value** (what we report) is correct.

**No fix needed** — the pipeline is consistent. Just be aware that tranche-weighted
rate × total debt will not exactly reproduce interest expense from the income statement
because the rate applies to face value, not carrying value.

---

### 4. Weighted average cost of debt — equal-weighted vs notional-weighted

The current `wtd_avg_rate` is a simple average across tranches, not weighted by
notional amount. For issuers with a single dominant tranche (e.g. one $20B note
and five $500M notes) this overstates the influence of the small tranches.

**Example:** If a company has $20B at 3% and $1B at 8%, equal-weighted avg = 5.5%,
notional-weighted avg = 3.33%. The notional-weighted figure is more meaningful
for WACC purposes.

**Future fix:** In `debt_note_fetcher._extract_tranches`, parse the notional amount
alongside the rate (already partially done for Format J: `$X million X.XX%`).
Store as `face_amount_m` per tranche. In `wtd_avg_rate` calculation, use
`sum(rate × amount) / sum(amount)` when amounts are available, fall back to
equal-weighted when not.

---

### 5. Callable and step-up bonds

Some bonds have call schedules or step-up coupons where the stated rate changes
over time. The pipeline captures the initial stated rate only. For callable bonds
trading at a premium the yield-to-call is the more relevant figure for Kd.

This primarily affects utilities and telecoms with older legacy bonds (e.g. AEP, SO,
VZ) that were issued at high coupons and are now trading well above par.

**Future fix:** Flag tranches where the name contains "callable", "redeemable", or
a call date, and display a note in the renderer. Yield-to-call requires a price feed
which is out of scope for the current pipeline.

---

### 6. Foreign currency debt

Some US companies issue EUR, GBP, or JPY-denominated notes (TMO, JNJ, AMGN are
frequent EUR issuers). The XBRL balance is translated to USD at the filing date
exchange rate. The coupon rate is in the original currency.

**Current behaviour:** The USD balance is captured correctly via XBRL.
The coupon rate from the text parser is the foreign-currency stated rate —
not the USD-equivalent all-in cost after cross-currency swap (which is what
most treasury desks use for Kd).

**Practical effect:** For TMO, EUR-denominated tranches at 1–2% pull the tranche
average down significantly. The IS-derived rate (3.45%) is actually more reliable
for TMO than the tranche average because it captures the all-in USD cost.

**Future fix:** In `debt_note_fetcher`, detect foreign currency tranches
(look for "€", "EUR", "£", "GBP", "¥", "JPY" in name or surrounding text)
and tag them as `currency != "USD"`. Exclude from USD wtd_avg or apply a
cross-currency basis adjustment (requires a swap spread data feed).

---

### 7. IS-derived Kd limitations

`IS-derived Kd = InterestExpense / TotalDebt` is a useful fallback but has
several known distortions:

| Situation | Effect on IS-Kd | Examples |
|---|---|---|
| Capitalised interest (construction) | Understates — some IE goes to assets | utilities, homebuilders (LEN, DHI) |
| Finance subsidiary (captive) | Overstates — subsidiary IE on consumer loans included | GE, GM |
| Net interest presentation | Understates — IE offset by interest income | META, GOOGL |
| Debt retired/issued mid-year | Distorts — balance ≠ average balance | any active issuer |
| Commercial paper costs | Overstates IE relative to LT debt balance | WMT, CVX |

The `⚠ IE>20%` flag catches the most egregious cases. A secondary flag at
`IE/Debt > 15%` for non-financial, non-energy companies would catch more
(MPC 15.81%, PSX 16.48% are borderline).

**Future fix:** For the capitalised interest case, add
`InterestCostsCapitalized` back to `InterestExpense` in the XBRL waterfall
to get gross interest before capitalisation. This gives a more accurate
picture for utilities and capital-intensive industrials.

---

### 8. Maturity schedule — standalone sub-section with visualization

**Status: SHIPPED (2026-06-30)**

The Debt Maturity Profile is now a standalone sub-section in the Credit Quality section.
Implemented in `agents.py` (`_compute_credit_quality`) and `renderer.py`.

**What was built:**

- Year-by-year table: amount, % of total, heat-coded circle indicator
- Heat circle: rank-based, blue = smallest tranche, red = largest, black = intermediate. All rows including "thereafter" ranked together.
- Weighted average maturity (WAM) computed from maturity schedule offset from filing year
- Lumpiness flags: single year ≥25% of total debt gets ⚠ on year label; 3-year rolling ≥45% triggers a cluster warning
- Legend: ● Smallest maturity tranche  ● Largest maturity tranche
- Data source: `xbrl_debt_fetcher.maturities` — ~95% XBRL coverage

**Still deferred — refinancing cost impact (P5):**

For each maturing tranche, estimate incremental annual IE if refinanced at current OAS-adjusted rate vs the maturing coupon:
```
2027 Notes: $8,200M at 1.375% → refi at ~5.2% (RF + OAS)
Incremental annual IE: $8,200M × (5.2% - 1.375%) = ~$314M/year
After-tax EPS impact: ~$0.18/share
```
Requires tranche-level notional amounts (Format J has them, others don't yet)
and diluted shares (already in quarterly processor).

Confirmed 2026-06-30: still blocked on the same gap — only Format J tranches
carry a parsed `face_amount_m`. Most tickers (including NVDA, Format D) have
no per-tranche dollar amount, so a naive build would only work for a small
subset of the universe. Decision: don't build this until either (a) face-amount
parsing is extended to more tranche formats, or (b) it's explicitly scoped to
degrade to "N/A — tranche notionals unavailable" per-ticker rather than silently
omitting or fabricating a number. See "Future Pipeline Additions" below.



## Known Issues (deferred — need live XBRL probe to fix)

### Total debt understated

| Ticker | Reported | Expected | Delta | Root cause |
|---|---|---|---|---|
| LLY | $31,487M | ~$33,640M | ~$2.1B | Current portion not tagged under standard `LongTermDebt*Current`. The $41.9B figure was incorrect — LLY 10-K MD&A states $33.64B at Dec 31 2024. Probe needed for exact tag. |
| CVX | $30,922M | ~$39,781M | ~$8.9B | Same issue — current portion / commercial paper tagged under an unidentified concept. |

**How to fix:** Run this probe in your environment to find the right tag:
```python
from xbrl_debt_fetcher import _get_cik, _get_facts

for ticker in ["LLY", "CVX"]:
    cik   = _get_cik(ticker)
    facts = _get_facts(cik)
    us_gaap = facts["facts"]["us-gaap"]
    print(f"\n{ticker} — tags with value $8B–$12B in 2024 10-K:")
    for tag, data in us_gaap.items():
        if any(k in tag.lower() for k in ["debt", "borrow", "notes", "current"]):
            for entries in data.get("units", {}).values():
                for e in entries:
                    if e.get("form") == "10-K" and "2024" in e.get("end", ""):
                        val_m = e["val"] / 1e6
                        if 7_000 < val_m < 13_000:
                            print(f"  {tag}: ${val_m:,.0f}M  end={e['end']}")
```
Once the tag is identified, add it to `_LT_CURRENT` in `xbrl_debt_fetcher.py`.

---

### No debt data — investigated 2026-06-30, mostly NOT a code bug

Original hypothesis: XBRL tag mismatch or CIK lookup failure across ~17
tickers. Probed 30 candidates (the original 17 plus related names surfaced
from `debt_summary.txt`) with `probe_xbrl_debt.py`. Finding: the large
majority are **not bugs** — they reflect real corporate events (ticker
symbol changes, mergers, going-private transactions) that the pipeline's
ticker list / CIK fallback map hasn't caught up with.

**Confirmed fixable — ticker symbol changed, company still files (genuine
pipeline gap):**

| Old ticker | New ticker | Notes |
|---|---|---|
| BK  | BNY  | NYSE symbol change, effective 2026-05-21 |
| FI  | FISV | Switched from NYSE:FI to NASDAQ:FISV (2025/2026) |
| MMC | MRSH | NYSE symbol change, effective 2026-01-14 (brand change to Marsh) |

Fix: add the new ticker → CIK mapping (or an alias) so lookups under either
the old or new symbol resolve correctly. Old symbol may still appear in
older filings/transcripts, so an alias rather than a hard rename is safer.

**Confirmed NOT a bug — company delisted / no longer independently files:**

| Ticker | Reason |
|---|---|
| ANSS | Acquired by Synopsys (2025) |
| CTRA | Merged into Devon Energy, closed 2026-05 |
| HOLX | Taken private by Blackstone/TPG, closed 2026-04 |
| DAY  | Taken private by Thoma Bravo, closed early 2026 |
| CTLT | Taken private by Novo Holdings, delisted 2024-12 |
| DFS  | Merged into Capital One, closed 2025-05 |

These will permanently show N/A and should be removed from the validation
universe rather than "fixed."

**Unverified — not yet checked, deprioritized given the pattern above:**

`BCR, FFIV, FLT, HES, IPG, JNPR, K, MPWR, PTVE, TROW`

Given 6/12 checked so far turned out to be defunct/delisted and only 3/12
were genuine pipeline gaps, these are likely to skew the same way. Worth a
quick web search per ticker before re-probing if picked up later, rather
than assuming an XBRL/CIK code defect.

**Tickers from the original 30-ticker probe batch that returned real XBRL
debt data (i.e. were never actually N/A bugs — included in the batch by
the IE>$5M heuristic but turned out fine or only partially limited):**
AES, AKAM, ACGL, CDNS, PCAR, NOW, DDOG (rate/face/maturity coverage varies
per `probe_xbrl.txt` — AES and ACGL recovered total debt via face amount
only; PCAR and DDOG returned no usable debt concepts at all and remain
genuinely unresolved candidates for a future tag-waterfall investigation).

---

## Data Quality Flags

### ⚠ IE>20% flag
When IS-derived Kd exceeds 20%, the output appends `⚠ IE>20%` to the rate string.
This signals that XBRL `InterestExpense` may be capturing more than debt service
(e.g. inventory financing, intercompany interest, captive finance arms).
Use tranche rate as the primary figure when available; treat IS-derived as a sanity check only.

Highest clean IS-Kd in current universe: **CZR 19.38%** (highly leveraged gaming — plausible).

### TTWO tranche rates (15.43%)
Take Two Interactive's tranches show 16–20% stated rates. These are real —
TTWO issued convertible notes with high effective yields. Not a parser error.
IS-derived (8.11%) is the more conservative figure.

### META / GOOGL low IS-Kd (0.76% / 0.60%)
Both companies net interest income against interest expense in XBRL tagging,
making IS-derived Kd appear near-zero. Tranche rates (4.79% / 0.80%) are correct.
Pipeline correctly uses tranche avg as the primary method for both.

---

## Tranche Format Coverage

| Format | Pattern | Example tickers |
|---|---|---|
| A | `YYYY Notes   stated   eff` (wide spacing or markdown pipe table) | MU |
| B | `Notes due YYYY   X%–Y%` (rate range) | LLY |
| C | `Fixed-rate X%–Y% notes YYYY–YYYY` | AAPL |
| D | `X.XX% Senior Notes Due YYYY` | NVDA, JNJ |
| E | `August 2022 Notes   2027–2062   3.50%–4.65%` | META, AMZN |
| F | Instrument + maturity date + single rate | CRM, GILD |
| G | Year-prefixed / description+rate% due Month YYYY | ACGL, AME |
| J | `$X million X.XX% Senior Notes, due YYYY` | CNC, CI |
| N | `fixed rate of X.XX%, balloon/due YYYY` (prose) | AJG |

Floating-rate tranches (EURIBOR/SOFR/LIBOR) are excluded from weighted average
but still displayed in the tranche table.

---

## Running Validation

```bash
# Full 519-ticker run (~30–45 min cold cache)
python run_debt_validation.py

# Specific tickers
python run_debt_validation.py --tickers MU LLY CVX AAPL

# Resume interrupted run
python run_debt_validation.py --resume
```

Output: `debt_summary.txt`

---

## Dependencies

| Item | Location | Notes |
|---|---|---|
| FRED API key | `.env` → `FRED_API_KEY` | Required for OAS spreads |
| Anthropic API key | `.env` → `ANTHROPIC_API_KEY` | Required |
| EDGAR identity | `run_debt_validation.py` top — `IDENTITY` | "Name email" format |
| `ratings.csv` | Project root | Manually maintained |
| `erp_cache.xlsx` | Project root | Auto-created, delete to refresh |

---

## Future Pipeline Additions

Additions beyond the debt section, in rough priority order.

### High priority

**Interest coverage ratio** — already shipped, lives in Section 2 (Risk &
Solvency) per-period table, computed in `RiskSolvencyAgent.assess()`. Stays
there by design — it's a general solvency ratio alongside current ratio,
quick ratio, D/E, and Altman Z, not a credit-quality/debt-schedule metric.
Not duplicated into the Credit Quality summary table.

**Short interest + borrow cost** — natural complement to the existing institutional
ownership section (query_ownership.py). Days-to-cover, short % of float, borrow rate.
Fintel and FINRA both have structured data. High short interest + deteriorating
guidance is a very different situation from high short interest + strong fundamentals —
surfacing both together is genuinely differentiated vs most retail terminals.

**Insider transactions** — SEC Form 4 is structured XBRL on EDGAR. Cluster buying
by multiple insiders near a low is one of the most reliable signals in equity research.
EDGAR infrastructure already exists. Relatively low build cost.

### Medium priority

**Refinancing cost impact (P5)** — per maturing tranche, incremental annual
interest expense if refinanced at current RF + OAS vs the maturing coupon,
translated into after-tax EPS impact. See full spec under "Maturity schedule"
above. Blocked on tranche-level face amounts, which only Format J currently
parses. Either extend face-amount parsing to more formats first, or scope
this to show "N/A — tranche notionals unavailable" per-ticker so it degrades
honestly rather than silently working for a small, undocumented subset of
the universe.

**Comparable company multiples** — EV/EBITDA, EV/Revenue, P/FCF vs sector median.
WACC gives the discount rate but there's no market-derived valuation anchor currently.
Peer group definition could live in a CSV like ratings.csv. Puts the DCF output in
context for the reader.

**Covenant and liquidity analysis** — unused revolver capacity is usually disclosed
in the debt note ("unused committed bank credit facilities"). Adding available liquidity
alongside net debt gives a complete credit picture. LLY for example has $8.45B of
unused facilities against $31B debt — that context matters. Partially parseable from
the same debt note text already being fetched.

**52-week price context** — where the stock trades relative to its range, distance
from key moving averages. Not for trading signals — for framing whether the current
valuation is compressed or stretched vs recent history. One paragraph, low build cost.

### Operational

**Report change detection** — when the pipeline runs on the same ticker twice,
diff the key figures: debt up $X, guidance cut, insider selling started, short
interest spiked. A change summary at the top of the report turns it from a
snapshot tool into a monitoring tool.

**Batch scheduling and alerting** — run the full universe weekly, push alerts
when a ticker crosses a threshold (Kd > 8%, guidance cut > 10%, insider cluster
sell, maturity wall flagged). Low engineering cost, high operational value for
ongoing monitoring.

