"""
debt_note_fetcher.py — Fetches and parses the debt note from a 10-K filing

Uses edgartools TenK.notes to find the debt note by title, then extracts:
  - Tranche list (name, stated rate, effective rate) via multi-format regex
  - Maturity schedule (year → amount) from maturity table text
  - Summary metrics: total debt, weighted avg effective rate, nearest maturity

Format taxonomy (from 100-ticker scan):
  A: MU-style    — "YYYY Notes   stated   eff"  (two decimal rates, wide spacing)
  B: LLY-style   — "Notes due YYYY   X%-Y%"     (range rate on same line)
  C: AAPL-style  — "Fixed-rate X%-Y% notes   YYYY-YYYY   $amount"
  D: NVDA-style  — "X.XX% Notes Due YYYY"        (rate-prefixed name)
  E: META-style  — "August 2022 Notes   2027-2062   3.50%-4.65%"
  F: CRM-style   — "2028 Senior Notes   April 2018   April 2028   3.70"
  G: Bank-style  — maturity buckets, not individual tranches (skip)
  H: Aggregate   — "USD notes X%-Y% due through YYYY" (single summary line)
"""

import re
import logging

logger = logging.getLogger(__name__)

# ── Title matching ─────────────────────────────────────────────────────────────
# Titles to EXCLUDE — contain "debt" but refer to investment portfolios
# or brokerage operations, not corporate borrowings
_DEBT_TITLE_EXCLUDE_RE = re.compile(
    r"""
    (?:
        investments?\s+in\s+(?:debt|equity)       |  # investment portfolio
        available[- ]for[- ]sale\s+debt            |  # AFS securities
        afs\s+and\s+htm\s+debt                     |  # AFS/HTM securities
        marketable\s+debt\s+securities             |  # marketable securities
        securities\s+borrowing\s+and\s+lending     |  # brokerage
        valuation\s+of\s+debt\s+and\s+equity       |  # fair value note
        indexed\s+debt\s+securities                |  # ZENS / exotic instruments
        debt\s+and\s+equity\s+securities           |  # investment portfolio
        equity\s+securities\s+and\s+indexed           # investment portfolio
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_DEBT_TITLE_RE = re.compile(
    r"""^(?:
        # ── Combined short+long-term (various orderings) ─────────────────────
        (?:short[- ]term\s+(?:borrowings?\s+and\s+)?)?long[- ]term\s+debt(?:\s+and\s+[\w\s,]+)?
        | (?:long[- ]term\s+(?:debt\s+and\s+)?)?short[- ]term\s+(?:debt|borrowings?)(?:\s+and\s+[\w\s,]+)?
        | short[- ]?(?:and|&)\s*long[- ]?term\s+debt(?:\s+and\s+[\w\s,]+)?
        | short[- ]term\s+and\s+long[- ]term\s+debt(?:\s+and\s+[\w\s,]+)?
        | long[- ]term\s+and\s+short[- ]term\s+debt(?:\s+and\s+[\w\s,]+)?
        | short[- ]term\s+borrowings?\s+and\s+long[- ]term\s+debt(?:\s+and\s+[\w\s,]+)?
        # ── Standalone debt ───────────────────────────────────────────────────
        | (?:long[- ]term\s+)?debt(?:\s+and\s+[\w\s,&.()-]+)?
        | (?:long[- ]term\s+)?debt,\s+(?:credit|net|notes|financing|derivatives|obligations)[\w\s,&.()-]*
        | debt\s*\((?:notes?|text\s+block)\)
        | note\s+\d+\s*[.:-]\s*debt
        | total\s+debt\s*\(notes?\)
        # ── Borrowings ────────────────────────────────────────────────────────
        | (?:short[- ]term\s+)?borrowings?(?:\s+and\s+[\w\s,&()-]+)?
        | bank\s+borrowings?\s+and\s+long[- ]term\s+debt
        | borrowing\s+(?:arrangements?|facilities?)(?:\s+and\s+[\w\s]+)?
        # ── Notes payable ────────────────────────────────────────────────────
        | (?:senior\s+)?notes?\s+payable(?:\s+(?:and|,)\s+[\w\s,&()-]+)?
        | senior\s+notes?\s+payable(?:\s+and\s+[\w\s]+)?
        | mortgage\s+notes?\s+payable
        # ── Senior / unsecured / convertible notes ────────────────────────────
        | (?:convertible\s+)?senior\s+(?:unsecured\s+)?notes?
        | unsecured\s+debt(?:\s+and\s+[\w\s]+)?
        | secured\s+and\s+unsecured\s+(?:senior\s+)?debt(?:,\s+net)?
        | senior\s+notes?\s+and\s+long[- ]term\s+debt
        | homebuilding\s+senior\s+notes?\s+and\s+other\s+debts?\s+payable
        | senior\s+notes?,?\s+[\w\s,]+(?:loan|facilit)[\w\s]+
        # ── Debt obligations / instruments / facilities ───────────────────────
        | debt\s+(?:obligations?|instruments?|facilities?|securities|financing|commitments?)(?:\s+and\s+[\w\s,]+)?
        | debt\s+of\s+the\s+operating\s+partnership
        | (?:short[- ]term\s+borrowings?,?\s+)?long[- ]term\s+debt\s+and\s+(?:available\s+)?credit\s+facilities?(?:\s+and\s+[\w\s]+)?
        # ── Indebtedness ─────────────────────────────────────────────────────
        | indebtedness:?
        # ── Loans payable ────────────────────────────────────────────────────
        | loans?\s+payable(?:,\s+long[- ]term\s+debt[\w\s,]+)?
        # ── Commercial paper + LT debt ───────────────────────────────────────
        | commercial\s+paper\s+and\s+long[- ]term\s+debt
        # ── Long-term notes ──────────────────────────────────────────────────
        | long[- ]term\s+(?:debt,?\s+)?notes?(?:\s+and\s+[\w\s]+)?
        | long[- ]term\s+(?:obligations?|debt),?\s+net
        # ── Credit and debt agreements ───────────────────────────────────────
        | credit\s+(?:and\s+other\s+debt|facilities?(?:\s+and\s+[\w\s]+)?)(?:\s+[\w\s]+)?
        # ── Term debt ────────────────────────────────────────────────────────
        | term\s+debt
        # ── Financing arrangements ───────────────────────────────────────────
        | (?:long[- ]term\s+)?(?:debt\s+and\s+)?financing\s+(?:arrangements?|activities?)(?:\s+and\s+[\w\s]+)?
        # ── Remaining missed patterns ─────────────────────────────────────────
        | debt\s+financing\s+(?:arrangements?)?
        | notes?\s+payable,?\s*net
        | long[- ]term\s+(?:debt|obligations?)[\w\s]*\(notes?\)
        | notes?\s+payable,?\s+long[- ]term\s+debt[\w\s,&()-]*(?:\(notes?\))?
        | long[- ]term\s+obligations?\s+and\s+borrowing\s+arrangements?
        | senior\s+unsecured\s+notes?\s+and\s+secured\s+debt
        | note\s+\d+[.]\s*debt\s*\(notes?\)
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

# ── ANSI escape code stripper ──────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# ── Maturity schedule ──────────────────────────────────────────────────────────
# Matches: "  2028      542" or "  2031 and thereafter   7,400"
# Excludes year ranges like "2031 - 2040"
_MATURITY_ROW_RE = re.compile(
    r"(?<!\d)(20\d\d(?:\s+and\s+thereafter)?)(?!\s*[-–]\s*20\d\d)\s+\$?\s*([\d,]+)"
)
_UNAMORTIZED_RE = re.compile(
    r"[Uu]namortized[^\n]{0,80}\n[^\$]*\$\s*([\d,]+)",
    re.DOTALL,
)
_TOTAL_LAST_RE = re.compile(r"\$\s*([\d,]+)", re.MULTILINE)

# ── Tranche regexes ────────────────────────────────────────────────────────────

# Format A (MU): "  2028 Notes   5.375   5.52"
# Wide spacing between name and two decimal rates
_TRANCHE_RE_A = re.compile(
    r"^[ \t]+(20\d\d[A-Za-z0-9 .]+?)\s{5,}(\d{1,2}\.\d+)\s{5,}(\d{1,2}\.\d+)",
    re.MULTILINE,
)

# Format B (LLY/GOOGL): "  Notes due 2027   3.10% - 5.50%"
# Range rate on same line as name
_TRANCHE_RE_B = re.compile(
    r"^[ \t]+((?:Notes?|Bonds?|Debentures?)\s+due\s+20\d\d(?:\s*[-–]\s*20\d\d)?"
    r"|20\d\d[A-Za-z0-9 .-]+?)\s{3,}(\d{1,2}\.\d+)%?\s*[-–]\s*(\d{1,2}\.\d+)%?",
    re.MULTILINE | re.IGNORECASE,
)

# Format C (AAPL): "  Fixed-rate 0.000% – 4.850% notes   2025 – 2062   $86,781"
_TRANCHE_RE_C = re.compile(
    r"^[ \t]+((?:Fixed|Floating)[- ]rate\s+[\d.]+%?\s*[-–]\s*[\d.]+%?\s+\w+)"
    r"\s{2,}(20\d\d(?:\s*[-–]\s*20\d\d)?)\s+"
    r"\$?\s*([\d,]+)",
    re.MULTILINE | re.IGNORECASE,
)

# Format D (NVDA/SLB/JNJ/EOG/NKE/etc.):
# Format D (NVDA/SLB/JNJ/EOG/NKE/etc.):
# "  3.20% Notes Due 2026" or "  0.50% 2026 Convertible Notes due June 1, 2026"
# Rate-prefixed name (including 0.XX% for convertibles)
_TRANCHE_RE_D = re.compile(
    r"^[ \t]+(\d{1,2}\.\d+%?\s+(?:20\d\d\s+)?"
    r"(?:Senior\s+(?:\w+\s+){0,2})?(?:Notes?|Bonds?|Debentures?|Debt"
    r"|Guaranteed\s+Notes?|Convertible\s+(?:Senior\s+)?Notes?|Notes?\s+due)"
    r"[^\n]{0,80}?20\d\d[^\n]{0,20}?)"
    r"(?:\s{3,}\$?\s*(\d[\d,]+))?",
    re.MULTILINE | re.IGNORECASE,
)

# Format J (CNC/CI/ADBE): "$X million/billion X.XX% Senior Notes, due YYYY"
# Dollar-amount-prefixed name with embedded rate
_TRANCHE_RE_J = re.compile(
    r"^[ \t]+(\$[\d,. ]+(?:million|billion)[^\n]{0,20}?(\d{1,2}\.\d+)%"
    r"[^\n]{0,60}?20\d\d)",
    re.MULTILINE | re.IGNORECASE,
)

# Format N (AJG prose): "fixed rate of X.XX%, balloon/due YYYY"
# Year may appear on the next line after "December" etc.
_TRANCHE_RE_N = re.compile(
    r"[ \t]+([\w ,\.\-]+fixed\s+rate\s+of\s+(\d{1,2}\.\d+)%"
    r"[\w ,\.\-]+(?:due|balloon)[\w\s,\.\-]{0,40}?(20\d\d))",
    re.IGNORECASE,
)

# Format E (META/AMZN): "  August 2022 Notes   2027 - 2062   3.50% - 4.65%"
# Issuance group name + maturity range + rate range (fields on same line, wrapped)
_TRANCHE_RE_E = re.compile(
    r"^[ \t]+([A-Za-z]+\s+20\d\d\s+Notes?[^\n]{0,30}?)"
    r"\s{2,}(20\d\d\s*[-–]\s*20\d\d)\s+"
    r"(\d{1,2}\.\d+)%?\s*[-–]\s*(\d{1,2}\.\d+)%?",
    re.MULTILINE | re.IGNORECASE,
)

# Format F (CRM/GILD/TMO/AVGO):
# "  2028 Senior Notes   April 2018   April 2028   3.70   1,500"
# or "  4.200% notes due October 2030   4.34   $1,000"
_TRANCHE_RE_F = re.compile(
    r"^[ \t]+((?:20\d\d\s+\w[^\n]{3,40}?"
    r"|\d{1,2}\.\d+%?\s+\w[^\n]{5,40}?)(?:due|Due)\s+\w+\s+20\d\d[^\n]{0,20}?)"
    r"\s{2,}(\d{1,2}\.\d+)\s",
    re.MULTILINE,
)

# Format H (MO/CL): "  USD notes, 2.450% to 10.200%...due through 2061"
# Aggregate range — extract as single summary tranche (amount on next line ok)
_TRANCHE_RE_H = re.compile(
    r"^[ \t]+(\w[^\n]{5,120}?(?:\d{1,2}\.\d+%?\s+to\s+\d{1,2}\.\d+%)"
    r"[^\n]{0,80}?(?:due|maturing)[^\n]{0,30}20\d\d)",
    re.MULTILINE | re.IGNORECASE,
)


def fetch_debt_note(ticker: str, identity: str = "") -> dict | None:
    """
    Fetches and parses the debt note from the most recent 10-K.

    Returns dict with keys:
        tranches, maturities, total_debt_m, wtd_avg_rate,
        nearest_maturity, source
    Or None on failure.
    """
    try:
        import edgar
        if identity:
            edgar.set_identity(identity)

        company = edgar.Company(ticker.upper())
        filing  = company.latest("10-K")
        if filing is None:
            logger.warning("DebtNoteFetcher: no 10-K for %s", ticker)
            return None

        if "A" in str(filing.form) or "/" in str(filing.form):
            for f in company.get_filings(form=["10-K"], amendments=False):
                if f.form == "10-K":
                    filing = f
                    break

        doc = filing.obj()
        if doc is None:
            return None

        notes = getattr(doc, "notes", None)
        if not notes:
            return None

        # Find ALL matching debt notes — some filers split short-term and
        # long-term debt into separate notes (e.g. "Short-Term Borrowings"
        # and "Long-Term Debt"). Collect all matches and merge their content.
        debt_notes = []
        all_titles = []
        for note in notes:
            title = (getattr(note, "title", "") or "").strip()
            all_titles.append(title)
            if _DEBT_TITLE_RE.match(title) and not _DEBT_TITLE_EXCLUDE_RE.search(title):
                debt_notes.append(note)
                logger.info("DebtNoteFetcher: %s — debt note: '%s'", ticker, title)

        if not debt_notes:
            logger.warning(
                "DebtNoteFetcher: no debt note for %s. Titles: %s",
                ticker, all_titles
            )
            return None

        if len(debt_notes) > 1:
            logger.info(
                "DebtNoteFetcher: %s — %d debt notes merged: %s",
                ticker, len(debt_notes),
                [getattr(n, "title", "") for n in debt_notes]
            )

        # ── Filing year (needed for XBRL maturity offsets + text filter) ────────
        try:
            filing_year = int(str(filing.filing_date)[:4])
        except Exception:
            filing_year = 0

        # ── Maturities + total debt: XBRL first, text fallback ───────────────
        from xbrl_debt_fetcher import fetch_xbrl_debt
        xbrl = fetch_xbrl_debt(ticker, str(filing.filing_date))

        if xbrl and xbrl.get("maturities"):
            # XBRL path: structured, reliable, current
            maturities   = xbrl["maturities"]
            total_debt_m = xbrl.get("total_debt_m")
            logger.info("debt_note: %s using XBRL maturities (%d years)",
                        ticker, len(maturities))
        else:
            # Text fallback: parse maturity table from note text
            logger.info("debt_note: %s using text maturity parser (no XBRL)", ticker)
            maturities   = {}
            total_debt_m = None

            for _dn in debt_notes:
                for table in getattr(_dn, "tables", []) or []:
                    text = _get_table_text(table)
                    if not text:
                        continue
                    if not re.search(r"maturit|future.{0,20}principal|payment",
                                     text, re.IGNORECASE):
                        continue
                    for m in _MATURITY_ROW_RE.finditer(text):
                        yr  = m.group(1).strip()
                        amt = _parse_millions(m.group(2))
                        if amt is not None and amt > 0:
                            maturities[yr] = amt
                    tm = _UNAMORTIZED_RE.search(text)
                    if tm:
                        total_debt_m = _parse_millions(tm.group(1))
                    elif not total_debt_m:
                        all_amts = [_parse_millions(m.group(1))
                                    for m in _TOTAL_LAST_RE.finditer(text)]
                        valid = [a for a in all_amts if a and a > 1000]
                        if valid:
                            total_debt_m = max(valid)

            # Filter text maturities to future years
            future = {}
            for yr, amt in maturities.items():
                if "thereafter" in yr.lower():
                    future[yr] = amt; continue
                try:
                    if int(yr.strip()[:4]) > filing_year:
                        future[yr] = amt
                except ValueError:
                    pass
            maturities = future

        # Use XBRL total debt if text parser didn't find it
        if total_debt_m is None and xbrl and xbrl.get("total_debt_m"):
            total_debt_m = xbrl["total_debt_m"]

        nearest_maturity = None
        for yr in sorted(maturities.keys()):
            if "thereafter" in yr.lower():
                continue
            if int(yr) > (filing_year or 0):
                nearest_maturity = yr
                break

        # ── Extract text from dataframe label column (cleanest source) ────────
        # to_dataframe() row["label"] contains the table text without ANSI codes
        # or bound-method artifacts. Fall back to note.text if unavailable.
        text_parts = []
        for _dn in debt_notes:
            label_text = _extract_label_text(_dn)
            if label_text:
                text_parts.append(label_text)
            else:
                # Fallback: note.text (may have ANSI codes stripped by _ANSI_RE)
                raw = str(getattr(_dn, "text", "") or "")
                text_parts.append(_ANSI_RE.sub("", raw))

        note_text = " ".join(text_parts)
        tranches  = _extract_tranches(note_text, filing_year)

        # ── Tranche-derived maturities (last resort when XBRL + text both fail) ──
        # NOTE: tranches carry no per-instrument dollar amount in this code path,
        # so we cannot populate `maturities` with real amounts here. Previously
        # this set every year to $0, which rendered a misleading all-zero
        # maturity wall. We only recover `nearest_maturity` from tranche name
        # years; `maturities` stays empty so the renderer shows an explicit
        # "not available" message instead of fabricated data.
        if not maturities and tranches:
            _yr_re = re.compile(r"(20\d\d)")
            tranche_years = []
            for t in tranches:
                years = _yr_re.findall(t.get("name", ""))
                if years:
                    yr = years[-1]
                    if filing_year == 0 or int(yr) > filing_year:
                        tranche_years.append(yr)
            if tranche_years:
                nearest_maturity = sorted(tranche_years)[0]
            logger.info("debt_note: %s nearest maturity derived from tranches: %s "
                        "(amounts unavailable — maturity wall will show N/A)",
                        ticker, nearest_maturity)

        # ── Weighted average effective rate ───────────────────────────────────
        # Exclude floating-rate tranches: their spread (e.g. EURIBOR+0.28%) is not
        # the all-in rate and would severely understate Kd (TMO: 0.28% vs ~3.4%).
        # Also exclude any rate >20% which signals a parser misfire.
        _FLOAT_RE = re.compile(
            r'\b(?:float|variable|libor|euribor|sofr|prime\s+rate|base\s+rate|adjustable|step.up)\b',
            re.IGNORECASE,
        )
        wtd_avg_rate = None
        eff_rates = [
            t["effective_rate"] for t in tranches
            if t.get("effective_rate")
            and t["effective_rate"] <= 20.0
            and not _FLOAT_RE.search(t.get("name", ""))
        ]
        if eff_rates:
            wtd_avg_rate = sum(eff_rates) / len(eff_rates) / 100

        result = {
            "tranches":         tranches[:10],
            "maturities":       maturities,
            "total_debt_m":     total_debt_m,
            "wtd_avg_rate":     wtd_avg_rate,
            "nearest_maturity": nearest_maturity,
            "source":           f"10-K ({filing.filing_date})",
            "format":           tranches[0].get("_fmt", "none") if tranches else "none",
        }

        logger.info(
            "DebtNoteFetcher: %s — fmt=%s %d tranches, total=$%.0fM, nearest=%s",
            ticker, result["format"], len(tranches),
            total_debt_m or 0, nearest_maturity or "N/A",
        )
        return result

    except Exception as e:
        logger.warning("DebtNoteFetcher: failed for %s — %s", ticker, e)
        return None


def _extract_tranches(note_text: str, filing_year: int) -> list[dict]:
    """
    Try each format in priority order. Return first non-empty result.
    Each format returns list of {name, stated_rate, effective_rate, [rate_range], _fmt}
    """
    for fmt, fn in [
        ("A", _parse_fmt_a),
        ("D", _parse_fmt_d),
        ("J", _parse_fmt_j),
        ("B", _parse_fmt_b),
        ("F", _parse_fmt_f),
        ("G", _parse_fmt_g),
        ("N", _parse_fmt_n),
        ("E", _parse_fmt_e),
        ("C", _parse_fmt_c),
        ("H", _parse_fmt_h),
    ]:
        tranches = fn(note_text)
        if tranches:
            for t in tranches:
                t["_fmt"] = fmt
            # Filter to future maturities
            if filing_year > 0:
                tranches = _filter_future(tranches, filing_year)
            if tranches:
                return tranches
    return []


def _parse_fmt_a(text: str) -> list[dict]:
    """MU-style: name   stated_rate   eff_rate

    Two sub-patterns:
      A1 — original wide-spacing: '  2028 Notes     5.375     5.52'
      A2 — markdown pipe table:   '| 2028 Notes | 5.375 | 5.52 |'
           (produced by edgartools to_markdown() when label column has short names)
    """
    results = []
    seen    = set()

    # A1: wide-spacing (original)
    for m in _TRANCHE_RE_A.finditer(text):
        name = m.group(1).strip()
        if name not in seen:
            seen.add(name)
            results.append({
                "name":           name,
                "stated_rate":    float(m.group(2)),
                "effective_rate": float(m.group(3)),
            })

    # A2: markdown pipe table — edgartools to_markdown() renders the debt table
    # as '| 2028 Notes | 5.375 | 5.52 |' when the label column only has names.
    # Match rows where col-1 starts with a year, col-2/col-3 are decimal rates.
    # Sanity guard: real bond coupons are <20%. If either rate exceeds 20 it means
    # the wrong column is being read (e.g. ES eff rate col = 50.00). Skip the row.
    if not results:
        _FMT_A_MD = re.compile(
            r"\|\s*(20\d\d[A-Za-z0-9 .]+?)\s*\|\s*(\d{1,2}\.\d+)\s*\|\s*(\d{1,2}\.\d+)\s*\|",
            re.MULTILINE,
        )
        for m in _FMT_A_MD.finditer(text):
            stated = float(m.group(2))
            eff    = float(m.group(3))
            if stated > 20.0 or eff > 20.0:
                continue
            name = m.group(1).strip()
            if name not in seen:
                seen.add(name)
                results.append({
                    "name":           name,
                    "stated_rate":    stated,
                    "effective_rate": eff,
                })

    return results


def _parse_fmt_b(text: str) -> list[dict]:
    """LLY-style: Notes due YYYY   X%-Y%"""
    results = []
    for m in _TRANCHE_RE_B.finditer(text):
        lo   = float(m.group(2))
        hi   = float(m.group(3))
        mid  = (lo + hi) / 2
        results.append({
            "name":           m.group(1).strip(),
            "stated_rate":    mid,
            "effective_rate": mid,
            "rate_range":     f"{lo:.3f}%–{hi:.3f}%",
        })
    return results


def _parse_fmt_c(text: str) -> list[dict]:
    """AAPL-style: Fixed-rate X%-Y% notes   YYYY-YYYY   $amount"""
    results = []
    for m in _TRANCHE_RE_C.finditer(text):
        name     = m.group(1).strip()
        mat_rng  = m.group(2).strip()
        rate_m   = re.search(r"(\d{1,2}\.\d+)%?\s*[-–]\s*(\d{1,2}\.\d+)%?", name)
        if rate_m:
            lo  = float(rate_m.group(1))
            hi  = float(rate_m.group(2))
            mid = (lo + hi) / 2
        else:
            lo = hi = mid = 0.0
        results.append({
            "name":           f"{name} ({mat_rng})",
            "stated_rate":    mid,
            "effective_rate": mid,
            "rate_range":     f"{lo:.3f}%–{hi:.3f}%" if rate_m else "N/A",
        })
    return results


def _parse_fmt_d(text: str) -> list[dict]:
    """NVDA/SLB/JNJ/EOG-style: X.XX% Notes Due YYYY"""
    results = []
    seen    = set()
    for m in _TRANCHE_RE_D.finditer(text):
        name = m.group(1).strip().rstrip(")(,")
        if name in seen:
            continue
        seen.add(name)
        # Extract the leading rate from the name
        rate_m = re.match(r"(\d{1,2}\.\d+)%?", name)
        rate   = float(rate_m.group(1)) if rate_m else 0.0
        results.append({
            "name":           name,
            "stated_rate":    rate,
            "effective_rate": rate,
        })
    return results


def _parse_fmt_e(text: str) -> list[dict]:
    """META/AMZN-style: August 2022 Notes   2027-2062   3.50%-4.65%"""
    results = []
    for m in _TRANCHE_RE_E.finditer(text):
        lo  = float(m.group(3))
        hi  = float(m.group(4))
        mid = (lo + hi) / 2
        results.append({
            "name":           f"{m.group(1).strip()} ({m.group(2).strip()})",
            "stated_rate":    mid,
            "effective_rate": mid,
            "rate_range":     f"{lo:.2f}%–{hi:.2f}%",
        })
    return results


def _parse_fmt_f(text: str) -> list[dict]:
    """CRM/GILD/AVGO-style: instrument + dates + single rate"""
    results = []
    seen    = set()
    for m in _TRANCHE_RE_F.finditer(text):
        name = m.group(1).strip().rstrip(")(,")
        if name in seen:
            continue
        seen.add(name)
        rate = float(m.group(2))
        results.append({
            "name":           name,
            "stated_rate":    rate,
            "effective_rate": rate,
        })
    return results


def _parse_fmt_g(text: str) -> list[dict]:
    """
    ACGL/AME/ATO-style: various description patterns
    G1: year-prefixed name + bare decimal rate (no % sign)
    G2: description prefix + rate% embedded + due month? year
    G3: X.XX% due YYYY simple
    """
    results = []
    seen    = set()

    # G1: "  2034 notes (1)   7.350   $300"
    pat_g1 = re.compile(
        r"^[ \t]+(20\d\d\s+\w[^\n]{2,40}?)\s{3,}(\d{1,2}\.\d{3})\s{2,}\$?\s*[\d,]+",
        re.MULTILINE,
    )
    for m in pat_g1.finditer(text):
        name = m.group(1).strip()
        rate = float(m.group(2))
        if name not in seen:
            seen.add(name)
            results.append({"name": name, "stated_rate": rate, "effective_rate": rate})

    # G2: "Unsecured 3.00% Senior Notes, due June 2027"
    if not results:
        pat_g2 = re.compile(
            r"^[ \t]+(\w[^\n]{0,40}?(\d{1,2}\.\d+)%[^\n]{0,50}?(?:due|maturing)[^\n]{0,20}?20\d\d)",
            re.MULTILINE | re.IGNORECASE,
        )
        for m in pat_g2.finditer(text):
            name = m.group(1).strip().rstrip(")(,")
            rate = float(m.group(2))
            if name not in seen and len(name) < 120:
                seen.add(name)
                results.append({"name": name, "stated_rate": rate, "effective_rate": rate})

    # G3: "X.XX% [description] due [Month] YYYY" — amount not required on same line
    if not results:
        pat_g3 = re.compile(
            r"^[ \t]+(\d{1,2}\.\d+%?[\w ,.()\/\-]{0,60}?due\s+(?:\w+\s+)?20\d\d)",
            re.MULTILINE | re.IGNORECASE,
        )
        for m in pat_g3.finditer(text):
            name   = m.group(1).strip().rstrip(")(,")
            rate_m = re.match(r"(\d{1,2}\.\d+)", name)
            rate   = float(rate_m.group(1)) if rate_m else 0.0
            if name not in seen and len(name) < 120:
                seen.add(name)
                results.append({"name": name, "stated_rate": rate, "effective_rate": rate})

    return results


def _parse_fmt_j(text: str) -> list[dict]:
    """CNC/CI/ADBE-style: $X million X.XX% Senior Notes, due YYYY"""
    results = []
    seen    = set()
    for m in _TRANCHE_RE_J.finditer(text):
        name = m.group(1).strip().rstrip(")(,")
        rate = float(m.group(2))
        if name not in seen and len(name) < 130:
            seen.add(name)
            results.append({"name": name, "stated_rate": rate, "effective_rate": rate})
    return results


def _parse_fmt_n(text: str) -> list[dict]:
    """AJG prose-style: fixed rate of X.XX%, due YYYY"""
    results = []
    seen    = set()
    for m in _TRANCHE_RE_N.finditer(text):
        name = m.group(1).strip().rstrip(")(,")
        rate = float(m.group(2))
        if name not in seen and len(name) < 150:
            seen.add(name)
            results.append({"name": name, "stated_rate": rate, "effective_rate": rate})
    return results


def _parse_fmt_h(text: str) -> list[dict]:
    """MO/CL aggregate-style: USD notes X%-Y% due through YYYY"""
    results = []
    for m in _TRANCHE_RE_H.finditer(text):
        name   = m.group(1).strip()
        rate_m = re.search(r"(\d{1,2}\.\d+)%?\s*(?:to|-)\s*(\d{1,2}\.\d+)%?", name)
        if rate_m:
            lo  = float(rate_m.group(1))
            hi  = float(rate_m.group(2))
            mid = (lo + hi) / 2
        else:
            lo = hi = mid = 0.0
        results.append({
            "name":           name,
            "stated_rate":    mid,
            "effective_rate": mid,
            "rate_range":     f"{lo:.3f}%–{hi:.3f}%" if rate_m else "N/A",
        })
    return results


def _filter_future(tranches: list[dict], filing_year: int) -> list[dict]:
    """Keep tranches whose last year mention is > filing_year."""
    _yr_re   = re.compile(r"(20\d\d)")
    filtered = []
    for t in tranches:
        years = _yr_re.findall(t.get("name", ""))
        if not years:
            filtered.append(t)
            continue
        if int(years[-1]) > filing_year:
            filtered.append(t)
    return filtered


def _extract_label_text(note) -> str:
    """
    Extract debt table text using multiple strategies in priority order:

    1. to_dataframe() label column — cleanest, no ANSI codes
    2. to_markdown() — clean markdown text from table
    3. note.text with ANSI stripped — final fallback

    Returns combined text from all tables and note text.
    """
    parts = []

    for table in getattr(note, "tables", []) or []:
        # Strategy 1: dataframe label column
        # Only use this path when the MAJORITY of labels are substantive (> 50 chars),
        # meaning the table encodes full tranche descriptions with embedded rates.
        # For MU-style tables the label column has SHORT names ('2028 Notes', '2029 Notes')
        # with rates in separate value columns — one long prose label ('Unamortized
        # discount...') must not trigger early exit and block markdown fallback.
        # Threshold: >50% of non-empty labels must be long.
        try:
            df = table.to_dataframe()
            if df is not None and not df.empty and "label" in df.columns:
                all_labels = [str(row.get("label", "") or "") for _, row in df.iterrows()
                              if str(row.get("label", "") or "").strip()]
                long_labels = [l for l in all_labels if len(l) > 50]
                majority_long = len(all_labels) > 0 and len(long_labels) > len(all_labels) / 2
                if majority_long:
                    parts.extend(long_labels)
                    continue   # labels have full content — skip other strategies
                # Labels are short (names only) — fall through to markdown
        except Exception:
            pass

        # Strategy 2: to_markdown()
        try:
            if hasattr(table, "to_markdown") and callable(table.to_markdown):
                md = table.to_markdown() or ""
                md = _ANSI_RE.sub("", md)
                if len(md.strip()) > 50 and not md.strip().startswith("<bound"):
                    parts.append(md)
                    continue
        except Exception:
            pass

        # Strategy 3: text property
        try:
            if hasattr(table, "text"):
                t = table.text() if callable(table.text) else str(table.text)
                t = _ANSI_RE.sub("", t)
                if len(t.strip()) > 50 and not t.strip().startswith("<bound"):
                    parts.append(t)
        except Exception:
            pass

    # Final fallback: note.text (ANSI stripped)
    if not parts:
        raw = str(getattr(note, "text", "") or "")
        raw = _ANSI_RE.sub("", raw)
        if len(raw.strip()) > 50:
            parts.append(raw)

    return " ".join(parts)


def _get_table_text(table) -> str:
    """Extract plain text, stripping ANSI codes."""
    for attr in ["to_markdown", "text"]:
        if hasattr(table, attr):
            try:
                val = getattr(table, attr)
                t   = val() if callable(val) else str(val)
                t   = _ANSI_RE.sub("", t)
                if t.strip().startswith("<bound method"):
                    continue
                if t and len(t.strip()) > 20:
                    return t
            except Exception:
                pass
    if hasattr(table, "to_dataframe"):
        try:
            df = table.to_dataframe()
            if df is not None and not df.empty:
                return df.to_string()
        except Exception:
            pass
    return ""


def _parse_millions(s: str) -> float | None:
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


if __name__ == "__main__":
    import sys, logging
    logging.basicConfig(level=logging.INFO)
    ticker   = sys.argv[1] if len(sys.argv) > 1 else "MU"
    identity = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"\nFetching debt schedule for {ticker}...\n{'='*60}")
    result = fetch_debt_note(ticker, identity=identity)
    if result:
        print(f"Source:          {result['source']}")
        print(f"Format:          {result['format']}")
        print(f"Total debt:      ${result['total_debt_m']:,.0f}M" if result['total_debt_m'] else "Total debt: N/A")
        print(f"Wtd avg rate:    {result['wtd_avg_rate']*100:.2f}%" if result['wtd_avg_rate'] else "Wtd avg rate: N/A")
        print(f"Nearest maturity:{result['nearest_maturity'] or 'N/A'}")
        print(f"\nMaturities: {result['maturities']}")
        print(f"\nTranches ({len(result['tranches'])}):")
        for t in result["tranches"]:
            rng = f"  [{t['rate_range']}]" if t.get("rate_range") else ""
            print(f"  {t['name']:<55} {t['stated_rate']:.3f}%{rng}")
    else:
        print("No debt data found.")
