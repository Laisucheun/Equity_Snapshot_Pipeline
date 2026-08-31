"""
renderer.py — EquityBriefRenderer

Builds a 1–2 page equity brief PDF from agent outputs.
Uses reportlab Platypus for clean, structured layout.

Layout:
    Page 1:
        Header bar (ticker, sector, market cap, report date)
        Section 1: Fundamentals table (3-yr)
        Section 2: Risk & Solvency table (3-yr)
        Section 3: Valuation table (historical + current price)
    Page 2+:
        Section 4: Red Flags (always rendered)
        Section 5: Trend Commentary (narrative bullets)
        Section 6: Management Guidance & Tone (MD&A excerpts)
        Section 7: Management Track Record

Watermark: diagonal grey "CONFIDENTIAL / <author>" on every page.
"""

import os
import re
from datetime import date
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm, cm
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    KeepTogether, HRFlowable, PageBreak
)
from reportlab.platypus.flowables import Flowable
from reportlab.pdfgen import canvas as rl_canvas


# ─────────────────────────────────────────────
# Colour palette
# ─────────────────────────────────────────────

C_DARK    = colors.HexColor("#1A1A2E")   # header bg
C_ACCENT  = colors.HexColor("#0F3460")   # section title bar
C_LIGHT   = colors.HexColor("#E8EAF0")   # alternating row
C_WHITE   = colors.white
C_RED     = colors.HexColor("#C0392B")
C_AMBER   = colors.HexColor("#E67E22")
C_GREEN   = colors.HexColor("#27AE60")
C_GREY    = colors.HexColor("#7F8C8D")
C_FLAG_BG = colors.HexColor("#FFF3CD")
C_FLAG_BD = colors.HexColor("#FFCC02")


# ─────────────────────────────────────────────
# Watermark canvas callback
# ─────────────────────────────────────────────

def _make_watermark_callback(author: str, ticker: str):
    def _on_page(canv, doc):
        canv.saveState()
        canv.setFont("Helvetica", 38)
        canv.setFillColor(colors.HexColor("#D0D0D0"))
        canv.setFillAlpha(0.18)
        w, h = A4
        canv.translate(w / 2, h / 2)
        canv.rotate(45)
        text = f"DRAFT  ·  {author.upper()}  ·  {ticker}"
        canv.drawCentredString(0, 0, text)
        canv.restoreState()

        # Footer
        canv.saveState()
        canv.setFont("Helvetica", 7)
        canv.setFillColor(C_GREY)
        canv.drawString(20 * mm, 10 * mm,
                        f"Prepared by {author}  ·  {date.today().isoformat()}  ·  "
                        f"For informational purposes only. Not investment advice.")
        page_num = doc.page
        canv.drawRightString(w - 20 * mm, 10 * mm, f"Page {page_num}")
        canv.restoreState()

    return _on_page


# ─────────────────────────────────────────────
# Style definitions
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# Module-level cell styles for _ratio_table
# Created once at import; reused for every table cell.
# ─────────────────────────────────────────────

_CELL_STYLE = ParagraphStyle(
    "cell", fontName="Helvetica", fontSize=7.5,
    textColor=C_DARK, leading=9, wordWrap="CJK",
    alignment=TA_LEFT,
)
_CELL_STYLE_HEADER = ParagraphStyle(
    "cell_hdr", fontName="Helvetica-Bold", fontSize=7.5,
    textColor=C_WHITE, leading=9,
    alignment=TA_LEFT,
)
_CELL_STYLE_MUTED = ParagraphStyle(
    "cell_muted", fontName="Helvetica-Oblique", fontSize=7,
    textColor=C_GREY, leading=8.5, wordWrap="CJK",
    alignment=TA_CENTER,
)
# For atomic values that must never be split mid-token — dates, signed
# currency amounts. The default cell style uses wordWrap="CJK", which breaks
# at any character, so a date in a narrow column rendered as "2026-06-1 8".
_CELL_STYLE_NOWRAP = ParagraphStyle(
    "cell_nowrap", fontName="Helvetica", fontSize=7.5,
    textColor=C_DARK, leading=9, alignment=TA_LEFT,
    wordWrap=None, splitLongWords=0,
)


def _cell_nowrap(val: str) -> Paragraph:
    return Paragraph(str(val), _CELL_STYLE_NOWRAP)
# Sub-rows: the YoY Δ / CAGR annotations rendered beneath a metric row in
# Section 3. One point smaller than the metric row and indented, so they
# read as an annotation of the row above rather than a metric of their own.
_CELL_STYLE_SUB = ParagraphStyle(
    "cell_sub", fontName="Helvetica", fontSize=6.5,
    textColor=C_GREY, leading=8, wordWrap="CJK",
    alignment=TA_CENTER,
)
_CELL_STYLE_SUB_LABEL = ParagraphStyle(
    "cell_sub_label", fontName="Helvetica-Oblique", fontSize=6.5,
    textColor=C_GREY, leading=8, wordWrap="CJK",
    alignment=TA_LEFT, leftIndent=8,
)


# A bare "&" in a table label (PP&E, SG&A) is parsed by ReportLab as the
# start of an XML entity and renders as "PP&E;". Escape bare ampersands
# only — anything already written as a proper entity (&amp;, &#127;) is left
# alone so it isn't double-escaped into "&amp;amp;".
_BARE_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")


def _esc_amp(s) -> str:
    return _BARE_AMP_RE.sub("&amp;", str(s))


def _cell(val: str) -> Paragraph:
    """
    Wrap a table cell value in a Paragraph so ReportLab word-wraps it.
    Diagnostic N/A strings (start with 'N/A (') use the muted italic style
    so they're visually distinct from real computed values.
    Centre-align data columns; left-align label column (col 0) is handled
    via TableStyle ALIGN directives — cell alignment is overridden there.
    Em-dash placeholder "—" gets a dedicated centre-aligned muted style so
    it (a) renders centred within the column, and (b) PDF text extraction
    picks up the surrounding spaces rather than concatenating adjacent dashes.
    """
    s = _esc_amp(val)
    if s.strip() == "—":
        # Use thin spaces around the dash so adjacent placeholder cells don't
        # run together in PDF text extraction ("77.26x——" → "77.26x  —  —")
        return Paragraph(" — ", _CELL_STYLE_MUTED)
    if s.startswith("N/A (") or s == "N/A":
        return Paragraph(s, _CELL_STYLE_MUTED)
    return Paragraph(s, _CELL_STYLE)


def _build_styles():
    base = getSampleStyleSheet()
    styles = {}

    styles["h1"] = ParagraphStyle(
        "h1", fontName="Helvetica-Bold", fontSize=18,
        textColor=C_WHITE, spaceAfter=2,
    )
    styles["h2"] = ParagraphStyle(
        "h2", fontName="Helvetica-Bold", fontSize=10,
        textColor=C_WHITE, spaceAfter=2,
    )
    styles["section"] = ParagraphStyle(
        "section", fontName="Helvetica-Bold", fontSize=8,
        textColor=C_WHITE, spaceBefore=4, spaceAfter=2,
    )
    styles["body"] = ParagraphStyle(
        "body", fontName="Helvetica", fontSize=8,
        textColor=C_DARK, leading=11, spaceAfter=3,
    )
    styles["bullet"] = ParagraphStyle(
        "bullet", fontName="Helvetica", fontSize=8,
        textColor=C_DARK, leading=11, spaceAfter=2,
        leftIndent=10, bulletIndent=0,
    )
    styles["flag"] = ParagraphStyle(
        "flag", fontName="Helvetica-Bold", fontSize=8,
        textColor=C_RED, leading=11, spaceAfter=2,
        leftIndent=10,
    )
    styles["guidance"] = ParagraphStyle(
        "guidance", fontName="Helvetica-Oblique", fontSize=7.5,
        textColor=C_DARK, leading=11, spaceAfter=3,
        leftIndent=10,
    )
    styles["meta"] = ParagraphStyle(
        "meta", fontName="Helvetica", fontSize=8,
        textColor=C_GREY, spaceAfter=1,
    )
    return styles


# ─────────────────────────────────────────────
# Table helpers
# ─────────────────────────────────────────────

def _section_header(title: str, styles, anchor: str = None) -> list:
    """Returns a coloured section header block.
    If anchor is provided, embeds a named PDF bookmark so the TOC can
    hyperlink to this section.
    """
    # Embed anchor as a named destination inside the paragraph text
    anchor_tag = f'<a name="{anchor}"/>' if anchor else ""
    t = Table(
        [[Paragraph(f"{anchor_tag}{title}", styles["section"])]],
        colWidths=["100%"]
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [t, Spacer(1, 2)]


def _is_na_cell(val: str) -> bool:
    """
    Return True only for cells that carry NO useful data and should count
    toward row suppression. Distinguishes two categories:

    Structural suppression (row should be hidden when all cells match):
      "N/A", "N/A (sector)", "N/A (financials)", "N/A (neg. equity)",
      "N/A (data incomplete)", "N/A (not applicable ...)", "—", ""

    Diagnostic N/A (cell is informative — row must be kept):
      "N/A (IEA tags not found in XBRL)"
      "N/A (goodwill untagged in XBRL)"
      "N/A (COGS missing)"   … and any other N/A with a parenthetical note
      that describes a data-quality issue specific to this ticker.

    Rule: bare "N/A" and "N/A (<structural reason>)" are suppressible;
    "N/A (<diagnostic detail about the ticker>)" are not.
    """
    s = str(val).strip()
    if s in ("—", "", "None"):
        return True
    if s == "N/A":
        return True
    # Structural suppressions: short parentheticals that describe sector
    # routing or data-model limitations, not ticker-specific findings.
    _STRUCTURAL = (
        "N/A (sector)",
        "N/A (financials)",
        "N/A (neg. equity)",
        "N/A (not applicable",   # covers "N/A (not applicable — financials)" etc.
        "N/A (data incomplete)",
        "N/A (no price)",
        "N/A (neg. TBV)",
        "N/A (intangibles)",
        "N/A (net inventory negative",
        "N/A (negative COGS",
        "N/A (costs missing)",
        "N/A (COGS missing)",
    )
    return any(s.startswith(p) for p in _STRUCTURAL)


# Maps a FundamentalAgent "_METRIC_SUPPRESSION" metric name to the literal
# Section 1 row label it corresponds to. Several rows are already relabeled
# per-sector (e.g. "Gross Margin" becomes "Net Interest Margin" for banks,
# "Operating Cost Ratio" for energy) -- those relabeled rows are meaningful
# replacements, not omissions, so they deliberately do NOT match here and
# are never removed. "COGS / Revenue" has no corresponding row at all (not
# rendered for any sector), so it's intentionally absent from this map.
# Values may be a single row label or a tuple of them, for metrics whose
# suppression must carry dependent rows with it -- suppressing "FCF Margin"
# also suppresses the three SBC rows, which are derived from FCF and would
# otherwise survive for the very sectors (REITs, banks, insurers) where FCF
# was judged not meaningful. One decision, one place.
_SUPPRESSION_ROW_ALIASES = {
    "Gross Margin":            "Gross Margin",
    "Operating Margin":        "Operating Margin",
    "Inventory Turnover":      "Inv. Turnover",
    "Days Sales of Inventory": "Days Sales of Inv.",
    "FCF Margin":              ("FCF Margin", "SBC / Revenue",
                                "FCF after SBC", "FCF after SBC Mgn"),
    "ROIC":                    "ROIC (proxy)",
}


def _drop_all_na(rows: list) -> list:
    """
    Remove rows where every data cell (columns 1+) is an N/A variant.
    Keeps label column 0 out of the check.
    Used to suppress metrics that are entirely unavailable for a ticker
    (e.g. OXY Operating Cost Ratio when cost tags are missing).
    """
    return [r for r in rows if not all(_is_na_cell(c) for c in r[1:])]


def _ratio_table(header_row: list, data_rows: list,
                 col_widths=None, sub_rows: set = None) -> Table:
    """
    Builds a ratio table. Every cell is wrapped in a Paragraph so ReportLab
    word-wraps long strings (e.g. diagnostic N/A messages) instead of clipping.
    header_row: list of strings
    data_rows:  list of lists of strings
    sub_rows:   optional set of 0-based indices into data_rows to render in
                the smaller, indented annotation style (YoY Δ / CAGR rows).
    """
    sub_rows = sub_rows or set()
    # Wrap header cells
    wrapped_header = [Paragraph(_esc_amp(c), _CELL_STYLE_HEADER) for c in header_row]
    # Wrap data cells — col 0 is the label (left-aligned), rest are values
    wrapped_data = []
    for i, row in enumerate(data_rows):
        is_sub = i in sub_rows
        wrapped_row = []
        for j, c in enumerate(row):
            if is_sub:
                p = Paragraph(_esc_amp(c), _CELL_STYLE_SUB_LABEL if j == 0
                              else _CELL_STYLE_SUB)
            elif j == 0:
                # Label column: use normal (non-muted) style even for N/A labels
                p = Paragraph(_esc_amp(c), _CELL_STYLE)
            else:
                p = _cell(str(c))
            wrapped_row.append(p)
        wrapped_data.append(wrapped_row)

    all_rows = [wrapped_header] + wrapped_data
    n_cols = len(header_row)

    if col_widths is None:
        page_w = A4[0] - 40 * mm
        label_w = 55 * mm
        rest = (page_w - label_w) / max(n_cols - 1, 1)
        col_widths = [label_w] + [rest] * (n_cols - 1)

    t = Table(all_rows, colWidths=col_widths, repeatRows=1)

    style = [
        # Header row
        ("BACKGROUND",    (0, 0), (-1, 0),  C_DARK),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  C_WHITE),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("ALIGN",         (1, 0), (-1, -1), "CENTER"),
        ("ALIGN",         (0, 0), (0, -1),  "LEFT"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
    ]
    # Annotation sub-rows sit tight under their metric row (+1 for header)
    for i in sub_rows:
        if 0 <= i < len(wrapped_data):
            r = i + 1
            style.append(("TOPPADDING",    (0, r), (-1, r), 1))
            style.append(("BOTTOMPADDING", (0, r), (-1, r), 1))
    t.setStyle(TableStyle(style))
    return t


# Colours for the YoY Δ annotation rows — growth green, decline red.
_C_YOY_UP   = "#1E8449"
_C_YOY_DOWN = "#A93226"


def _fmt_signed_pct(v, decimals: int = 1) -> str:
    """'+13.8%' / '-52.1%', colour-coded by sign. '—' when not computable."""
    if not isinstance(v, (int, float)):
        return "—"
    color = _C_YOY_UP if v >= 0 else _C_YOY_DOWN
    return f'<font color="{color}">{v:+.{decimals}f}%</font>'


def _trend_subrows(trend: dict, periods: list) -> list:
    """
    YoY Δ and CAGR annotation rows for one Section 3 metric.

    Returns 0-2 rows shaped to the Section 3 table (label + "Current"
    column + one column per period). The "Current" column is a live-price
    figure rather than a fiscal period, so it never carries a YoY value.
    The oldest period has no prior year, so it shows "—" too.

    Rows are omitted entirely when nothing resolves: no YoY row when every
    period is None, no CAGR row when fewer than 3 periods have data.
    """
    if not trend or not periods:
        return []
    rows   = []
    yoy    = trend.get("yoy", {}) or {}
    cagr   = trend.get("cagr")
    n_yrs  = trend.get("n_years") or 0

    if any(isinstance(yoy.get(p), (int, float)) for p in periods):
        rows.append(["YoY Δ", "—"] + [_fmt_signed_pct(yoy.get(p)) for p in periods])

    if isinstance(cagr, (int, float)):
        label = f"CAGR ({n_yrs}yr)" if n_yrs else "CAGR"
        # Value sits under the most recent fiscal year — it is the compound
        # rate through that period, not a current-price figure.
        rows.append([label, "—", _fmt_signed_pct(cagr * 100)]
                    + [""] * (len(periods) - 1))

    return rows


# ─────────────────────────────────────────────
# Section 2B — Working Capital & Capital Intensity
# ─────────────────────────────────────────────

def _fmt_days(v) -> str:
    if isinstance(v, (int, float)):
        return f"{v:,.1f}"
    return "N/A"


def _fmt_pct1(v) -> str:
    if isinstance(v, (int, float)):
        return f"{v:.1f}%"
    return "N/A"


def _fmt_pct2(v) -> str:
    """Two-decimal percent; passes diagnostic strings (N/A (pre-split)) through."""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return f"{v:.2f}%"
    return "N/A"


def _fmt_signed_pct1(v) -> str:
    """Signed one-decimal percent, for the share-count change row."""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return f"{v:+.1f}%"
    return "N/A"


def _fmt_nwc(v) -> str:
    """Net working capital in $B / $M, signed."""
    if not isinstance(v, (int, float)):
        return "N/A"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e9:
        return f"{sign}${a/1e9:,.1f}B"
    if a >= 1e6:
        return f"{sign}${a/1e6:,.0f}M"
    return f"{sign}${a:,.0f}"


def _build_wc_rows(wc: dict, periods: list) -> list:
    """
    Section 2B rows for the given working-capital payload, already filtered
    by mode. Returns [] when the section shouldn't render at all — mode is
    "skip" (banks, insurers, REITs) or nothing resolved from XBRL.
    """
    if not wc or not periods:
        return []
    mode = wc.get("mode", "skip")
    if mode == "skip":
        return []

    def _row(label, key, fmt):
        return [label] + [fmt(wc.get(key, {}).get(p)) for p in periods]

    rows = [
        _row("DSO (days)", "dso", _fmt_days),
    ]
    if mode == "full":
        rows.append(_row("DIO (days)", "dio", _fmt_days))
    rows += [
        _row("DPO (days)", "dpo", _fmt_days),
        _row("CCC (days)", "ccc", _fmt_days),
        _row("NWC ($)",       "nwc",         _fmt_nwc),
        _row("NWC / Revenue", "nwc_pct_rev", _fmt_pct1),
        _row("AR / Revenue",  "ar_pct_rev",  _fmt_pct1),
        _row("AP / Revenue",  "ap_pct_rev",  _fmt_pct1),
    ]
    if mode == "full":
        rows += [
            _row("Inv / Revenue", "inv_pct_rev", _fmt_pct1),
            _row("Inv / Assets",  "inv_pct_ta",  _fmt_pct1),
        ]
    rows += [
        _row("PP&E / Assets",   "ppe_pct_ta",    _fmt_pct1),
        _row("CapEx / Revenue", "capex_pct_rev", _fmt_pct1),
        _row("CapEx / CFO",     "capex_pct_cfo", _fmt_pct1),
    ]
    return _drop_all_na(rows)


# ─────────────────────────────────────────────
# Glossary — drives the Appendix off rendered row labels
# ─────────────────────────────────────────────
#
# Keyed by the EXACT row label the pipeline emits, so the appendix can be
# built from the set of labels actually rendered rather than from
# section-by-section conditionals that drift as metrics are added. Value is
# (group, definition). A label with no entry here is reported as a
# GLOSSARY GAP at render time.
#
# _GLOSSARY_CORE terms are always emitted -- they appear in cell values,
# footnotes and column headers rather than as row labels, so no rendered
# label would ever pull them in.

_GLOSSARY: dict[str, tuple[str, str]] = {}


def _g(group: str, entries: dict):
    for term, defn in entries.items():
        _GLOSSARY[term] = (group, defn)


_g("General", {
    "GAAP": "Generally Accepted Accounting Principles (US).",
    "XBRL": "eXtensible Business Reporting Language — the tagged financial data "
            "filers submit to the SEC; the primary source for this report.",
    "SEC":  "US Securities and Exchange Commission.",
    "FY":   "Fiscal Year — the company's own reporting year, which may not align "
            "with the calendar year.",
    "TTM":  "Trailing Twelve Months — the most recent four quarters.",
    "bps":  "Basis points — one hundredth of a percentage point (100bps = 1.00%).",
    "pp":   "Percentage points — the arithmetic difference between two percentages.",
    "N/A":  "Not available or not applicable. Where a reason is given in "
            "parentheses, it states why the figure was not computed.",
    "YoY":  "Year over Year — change versus the prior fiscal year.",
    "CAGR": "Compound Annual Growth Rate — the constant annual rate that compounds "
            "the oldest value to the most recent over the period shown.",
})

_g("Fundamentals & Profitability", {
    "Gross Margin":        "Gross profit divided by revenue.",
    "Net Interest Margin": "Net interest income divided by interest-earning assets "
                           "(NIM). Replaces gross margin for banks.",
    "Operating Cost Ratio":"Total operating costs divided by revenue. Replaces gross "
                           "margin for E&P energy companies, where a lower value is better.",
    "Net Revenue Margin (≈ Operating Margin)":
                           "Revenue less total operating costs, divided by revenue. Used "
                           "for non-asset-based freight brokers, where purchased "
                           "transportation is not separately tagged.",
    "Operating Margin":    "Operating income (EBIT) divided by revenue.",
    "EBITDA Margin":       "EBITDA (earnings before interest, taxes, depreciation and "
                           "amortisation) divided by revenue.",
    "Net Margin":          "Net income divided by revenue.",
    "ROE":                 "Return on Equity — net income divided by average "
                           "shareholders' equity (opening and closing average).",
    "ROA":                 "Return on Assets — net income divided by total assets.",
    "ROIC (proxy)":        "Return on Invested Capital — after-tax operating profit "
                           "divided by equity plus debt. A proxy: NOPAT is estimated at "
                           "a flat 79% of operating income.",
    "ROCE":                "Return on Capital Employed — EBIT divided by total assets "
                           "less current liabilities.",
    "Efficiency Ratio":    "Non-interest expense divided by net revenue. A bank cost "
                           "measure where lower is better.",
    "ROTCE":               "Return on Tangible Common Equity — net income divided by "
                           "common equity excluding goodwill and intangibles.",
    "Asset Turnover":      "Revenue divided by total assets.",
    "Inv. Turnover":       "Inventory Turnover — cost of goods sold divided by inventory.",
    "Days Sales of Inv.":  "Days Sales of Inventory (DSI) — average days inventory is "
                           "held before sale.",
    "FCF Margin":          "Free Cash Flow (cash from operations less capital "
                           "expenditure) divided by revenue.",
    "SBC / Revenue":       "Stock-Based Compensation as a share of revenue. SBC is "
                           "non-cash equity compensation expense.",
    "FCF after SBC":       "Free cash flow less stock-based compensation, treating SBC "
                           "as a real economic cost. GAAP FCF excludes it.",
    "FCF after SBC Mgn":   "FCF after SBC divided by revenue.",
    "FFO":                 "Funds From Operations — net income plus real-estate "
                           "depreciation, less gains on property sales, plus impairments "
                           "(NAREIT definition). The REIT sector's standard earnings measure.",
    "FFO Margin":          "FFO divided by revenue.",
    "AFFO":                "Adjusted Funds From Operations — FFO less straight-line rent, "
                           "lease amortisation and maintenance capital expenditure, plus "
                           "non-cash stock compensation and interest. The usual basis for "
                           "assessing REIT distribution coverage.",
    "AFFO Margin":         "AFFO divided by revenue.",
})

_g("Shareholder Returns", {
    "Dividends Paid":        "Cash dividends paid during the year (financing activities).",
    "Net Buybacks":          "Share repurchases less proceeds from share issuance "
                             "(employee plans, option exercises).",
    "Dividend Yield":        "Dividends paid divided by fiscal-year-end market "
                             "capitalisation.",
    "Buyback Yield":         "Net buybacks divided by fiscal-year-end market capitalisation.",
    "Total Shareholder Yld": "Dividend yield plus buyback yield — total capital returned "
                             "as a share of market value.",
    "Payout (% of FCF)":     "Dividends paid as a share of free cash flow.",
    "Payout (% of AFFO)":    "Dividends paid as a share of AFFO — the REIT distribution "
                             "base, used because GAAP FCF is distorted by real-estate "
                             "depreciation.",
    "Payout (% of Earnings)":"Dividends paid as a share of net income.",
    "Diluted Share Δ YoY":   "Year-over-year change in the diluted share count. Negative "
                             "means net buybacks shrank the count.",
})

_g("Working Capital & Capital Intensity", {
    "DSO (days)":     "Days Sales Outstanding — accounts receivable divided by revenue, "
                      "times 365. Average days to collect.",
    "DIO (days)":     "Days Inventory Outstanding — inventory divided by COGS, times 365.",
    "DPO (days)":     "Days Payable Outstanding — accounts payable divided by COGS "
                      "(revenue where no cost-of-sales line is filed), times 365.",
    "CCC (days)":     "Cash Conversion Cycle — DIO plus DSO less DPO. Days of cash tied "
                      "up in the operating cycle; negative means suppliers fund it.",
    "NWC ($)":        "Net Working Capital — current assets less current liabilities.",
    "NWC / Revenue":  "Net working capital as a share of revenue.",
    "AR / Revenue":   "Accounts receivable as a share of revenue.",
    "AP / Revenue":   "Accounts payable as a share of revenue.",
    "Inv / Revenue":  "Inventory as a share of revenue.",
    "Inv / Assets":   "Inventory as a share of total assets.",
    # Keys are the RAW row label as emitted; escaping happens at render time.
    "PP&E / Assets":  "Property, plant and equipment (net) as a share of total assets.",
    "CapEx / Revenue":"Capital expenditure as a share of revenue, on magnitude.",
    "CapEx / CFO":    "Capital expenditure as a share of operating cash flow. Above 100% "
                      "means capex exceeds the cash the business generates.",
})

_g("Risk & Solvency", {
    "Current Ratio":    "Current assets divided by current liabilities.",
    "Quick Ratio":      "Current assets less inventory, divided by current liabilities.",
    "D/E Ratio":        "Total debt divided by shareholders' equity.",
    "Debt/Capital":     "Total debt divided by debt plus equity.",
    "Net Debt/EBITDA":  "Debt less cash, divided by EBITDA — leverage in years of earnings.",
    "Interest Coverage":"EBIT divided by interest expense — how many times earnings cover "
                        "debt service.",
    "Altman Z-Score":   "A bankruptcy-risk score. Above 2.99 safe, 1.81–2.99 grey, below "
                        "1.81 distress. Calibrated on manufacturers, so it is suppressed "
                        "for financials, energy and regulated utilities.",
})

_g("Capital Adequacy (Banks)", {
    "Tier 1 Capital":          "Tier 1 capital as a share of risk-weighted assets (RWA). "
                               "Basel III minimum 6%, 8.5% including the conservation buffer.",
    "Total Capital":           "Total regulatory capital as a share of RWA. Minimum 8%, "
                               "10.5% including the buffer.",
    "T1 Leverage/avg assets":  "Tier 1 capital divided by average total assets. Differs "
                               "from the Supplementary Leverage Ratio (SLR), which uses a "
                               "broader exposure denominator.",
    "CET1":                    "Common Equity Tier 1 ratio — the primary Basel III capital "
                               "adequacy measure.",
})

_g("Credit Quality", {
    "Risk-Free Rate (10Y UST)":  "Yield on the 10-year US Treasury note (source: FRED).",
    "OAS Credit Spread":         "Option-Adjusted Spread — the credit spread over the "
                                 "risk-free curve for the issuer's rating tier "
                                 "(source: ICE BofA via FRED).",
    "Implied Cost of Debt":      "Risk-free rate plus the OAS credit spread.",
    "Equity Risk Premium (ERP)": "The excess return investors demand over the risk-free "
                                 "rate (Damodaran US implied).",
    "Country Risk Premium (CRP)":"Additional premium for country-specific risk; zero for "
                                 "US-domiciled issuers.",
    "Wtd Avg Effective Rate":    "Weighted average effective interest rate across the "
                                 "filer's debt tranches.",
    "Total Debt Outstanding":    "Total debt per the filing's debt note.",
    "Largest Maturity":          "The single largest year of scheduled debt maturities, "
                                 "shown against enterprise value and total debt.",
    "Nearest Maturity":          "The earliest year in which debt comes due.",
})

_g("Valuation", {
    "Diluted EPS":       "Diluted Earnings Per Share — net income divided by the diluted "
                         "share count.",
    "BVPS":              "Book Value Per Share — common equity divided by the diluted "
                         "share count.",
    "P/E":               "Price to Earnings — share price divided by diluted EPS.",
    "P/B":               "Price to Book — share price divided by book value per share.",
    "P/TBV":             "Price to Tangible Book Value — book value excluding goodwill "
                         "and intangibles.",
    "EV/EBITDA":         "Enterprise Value divided by EBITDA.",
    "EV/Sales":          "Enterprise Value divided by revenue.",
    "EV/FCF":            "Enterprise Value divided by free cash flow — years of cash flow "
                         "to pay for the enterprise.",
    "EV/CFO":            "Enterprise Value divided by operating cash flow.",
    "FCF/EV":            "Free cash flow as a percentage of enterprise value — a yield.",
    "CFO/EV":            "Operating cash flow as a percentage of enterprise value.",
    "CFO/Share":         "Operating cash flow divided by the diluted share count.",
    "EV/FFO":            "Enterprise Value divided by FFO — the REIT equivalent of EV/EBITDA.",
    "FFO/EV":            "FFO as a percentage of enterprise value.",
    "EV/AFFO":           "Enterprise Value divided by AFFO.",
    "AFFO/EV":           "AFFO as a percentage of enterprise value.",
    "Diluted Shares":    "Weighted-average diluted share count for the period.",
    "Shares Outstanding":"Shares outstanding at the latest live snapshot. No historical "
                         "series is available, so only the Current column is populated.",
    "YoY Δ":             "Year-over-year change in the row above. Suppressed where the "
                         "prior value is zero or of the opposite sign, which would make "
                         "the percentage meaningless.",
    "EV":                "EV = Market Cap + Total Debt + Operating Lease Liabilities + "
                         "Finance Lease Liabilities − Cash (IFRS 16 / ASC 842 standard "
                         "analyst treatment). Third-party EV figures may differ if they "
                         "exclude lease liabilities or use different debt definitions. "
                         "Computed at fiscal-year-end price × diluted shares.",
})

_g("Cost of Capital", {
    "Beta (yfinance)":        "The stock's sensitivity to market moves, used in CAPM.",
    "Cost of Equity":         "Risk-free rate plus beta times the equity risk premium (CAPM).",
    "Cost of Debt":           "Risk-free rate plus the rating-implied credit spread.",
    "Effective Tax Rate":     "Income tax expense divided by pre-tax income.",
    "After-tax Cost of Debt": "Cost of debt multiplied by one minus the tax rate.",
    "Equity Weight":          "Market capitalisation as a share of total capital.",
    "Debt Weight":            "Total debt as a share of total capital.",
    "WACC":                   "Weighted Average Cost of Capital — the blended required "
                              "return across equity and debt.",
})

# Always emitted: these appear in cell values, footnotes and headers rather
# than as row labels, so no rendered label would pull them in.
_GLOSSARY_CORE = ["GAAP", "XBRL", "SEC", "FY", "TTM", "bps", "pp", "N/A",
                  "YoY", "CAGR"]

# Group display order in the appendix.
_GLOSSARY_GROUP_ORDER = [
    "General", "Fundamentals & Profitability", "Shareholder Returns",
    "Working Capital & Capital Intensity", "Risk & Solvency",
    "Capital Adequacy (Banks)", "Credit Quality", "Valuation",
    "Cost of Capital",
]

# Row labels that carry no definable term -- spacers, sub-row markers and
# labels whose meaning is the value beside them. Excluded from the gap check
# rather than given filler definitions.
_GLOSSARY_IGNORE = {"", "Metric", "—"}


def _normalise_label(label: str) -> str:
    """
    Reduce a rendered row label to its glossary key.

    Tries the label verbatim first, since most are exact keys. Falls back to
    stripping the trailing qualifier the renderer appends for context -- a
    parenthetical ("(min 8.5%)", "(4yr)", "(proxy)"), a bracketed formula
    ("[RF + β×ERP]"), or the AFFO proxy dagger.
    """
    s = str(label).replace("†", "").strip()
    if s in _GLOSSARY:
        return s
    s = re.sub(r"\s*\[[^\]]*\]\s*$", "", s).strip()
    if s in _GLOSSARY:
        return s
    s2 = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    if s2 in _GLOSSARY:
        return s2
    return s


def _glossary_for_labels(labels: set, ticker: str = "") -> tuple[dict, list]:
    """
    Resolve rendered row labels to glossary entries.

    Returns ({group: [(term, definition)]}, [labels with no entry]). The
    caller prints the gaps so a metric added without a definition surfaces
    immediately instead of going silently undefined.
    """
    matched: dict = {}
    gaps: list = []

    def _add(term):
        group, defn = _GLOSSARY[term]
        matched.setdefault(group, {})[term] = defn

    for term in _GLOSSARY_CORE:
        if term in _GLOSSARY:
            _add(term)

    for raw in sorted(labels):
        if str(raw).strip() in _GLOSSARY_IGNORE:
            continue
        key = _normalise_label(raw)
        if key in _GLOSSARY:
            _add(key)
        else:
            gaps.append(str(raw))

    ordered = {}
    for group in _GLOSSARY_GROUP_ORDER:
        if group in matched:
            ordered[group] = sorted(matched[group].items())
    for group in matched:                      # any group not in the order list
        if group not in ordered:
            ordered[group] = sorted(matched[group].items())
    return ordered, gaps


def _abbreviations_appendix(styles, rendered_labels: set,
                            ticker: str = "") -> list:
    """
    Build the appendix from the row labels this report actually rendered.

    No sector conditionals: a bank's report never emits an FFO row, so FFO
    never enters the label set and never reaches the appendix. Adding a
    metric row automatically adds its definition, and forgetting the
    definition is reported as a GLOSSARY GAP rather than silently omitted.
    """
    groups, gaps = _glossary_for_labels(rendered_labels, ticker)
    for label in gaps:
        print(f"[{ticker}] GLOSSARY GAP: no definition for row '{label}'")

    _term_style = ParagraphStyle(
        "abbrev_term", fontName="Helvetica-Bold", fontSize=7.5,
        textColor=C_DARK, leading=9.5, alignment=TA_LEFT,
    )
    _def_style = ParagraphStyle(
        "abbrev_def", fontName="Helvetica", fontSize=7.5,
        textColor=C_DARK, leading=9.5, alignment=TA_LEFT,
    )

    page_w = A4[0] - 40 * mm
    term_w = 34 * mm
    elems = []
    for title, entries in groups.items():
        if not entries:
            continue
        elems.append(Paragraph(f"<b>{title}</b>", styles["body"]))
        elems.append(Spacer(1, 1))
        rows = [[Paragraph(_esc_amp(term), _term_style),
                 Paragraph(_esc_amp(defn), _def_style)]
                for term, defn in entries]
        t = Table(rows, colWidths=[term_w, page_w - term_w])
        t.setStyle(TableStyle([
            # Definitions are prose, so the whole table is left-aligned --
            # _ratio_table centres its data columns, which reads badly here.
            ("ALIGN",         (0, 0), (-1, -1), "LEFT"),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",   (0, 0), (-1, -1), 4),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
            ("LINEBELOW",     (0, 0), (-1, -2), 0.25, colors.HexColor("#E0E0E0")),
            ("ROWBACKGROUNDS",(0, 0), (-1, -1), [C_WHITE, C_LIGHT]),
        ]))
        elems.append(t)
        elems.append(Spacer(1, 6))
    return elems


def _heat_color(rank: int, n_rows: int) -> object:
    """
    rank    : 1-based rank by pct ascending (1 = lowest, n_rows = highest)
    n_rows  : total non-thereafter rows
    rank 1 = blue, rank n_rows = red, all others = black.
    """
    if rank == n_rows:
        return colors.HexColor("#C0392B")   # highest — red
    if rank == 1:
        return colors.HexColor("#2471A3")   # lowest — blue
    return colors.HexColor("#1A1A1A")       # all others — black


class _HeatCircle(Flowable):
    """A filled circle using heat colour — placed in the Concentration table cell."""
    def __init__(self, hex_color: str, radius: float = 5):
        super().__init__()
        self._color  = colors.HexColor(hex_color) if isinstance(hex_color, str) else hex_color
        self._radius = radius
        self.width   = radius * 2
        self.height  = radius * 2

    def draw(self):
        self.canv.setFillColor(self._color)
        self.canv.setStrokeColor(self._color)
        self.canv.circle(self._radius, self._radius, self._radius, fill=1, stroke=0)


def _flag_box(flags: list, styles) -> list:
    """Returns a yellow-highlighted flag box if flags exist."""
    if not flags:
        return []
    elems = []
    box_rows = []
    for f in flags:
        box_rows.append([Paragraph(f"⚑  {f}", styles["flag"])])

    t = Table(box_rows, colWidths=["100%"])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_FLAG_BG),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.5, C_FLAG_BD),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ]))
    elems.append(t)
    elems.append(Spacer(1, 4))
    return elems


def _fmt_market_cap(mc: float) -> str:
    if not mc:
        return "N/A"
    if mc >= 1e12:
        return f"${mc / 1e12:.2f}T"
    if mc >= 1e9:
        return f"${mc / 1e9:.1f}B"
    return f"${mc / 1e6:.1f}M"


# ─────────────────────────────────────────────
# Executive Summary builder (deterministic template)
# ─────────────────────────────────────────────

# Keywords that mark a flag as high-severity — used to surface the most
# material risk in Slot 4 rather than blindly taking flags[0].
_HIGH_SEV_KEYWORDS = (
    "net loss", "net margin", "revenue", "contraction", "negative",
    "interest coverage", "combined signal", "distress", "impairment",
    "data error",
)


def _parse_rendered_number(v):
    """
    Parse a value as the tables render it ("42.81x", "23.73%", "$5.18") back
    to a float, so a summary figure can be checked against its table
    counterpart. Returns None for any suppression sentinel ("N/A (pre-split)",
    "N/A (financials)", "—") rather than a number, so the summary inherits
    the table's suppression instead of reaching past it.
    """
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or s.startswith("N/A") or s.startswith("—") or s.startswith("DEFINITION"):
        return None
    m = re.match(r"^\s*-?\$?\s*(-?[\d,]+(?:\.\d+)?)\s*[x%]?", s)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return -val if s.lstrip().startswith("-") and val > 0 else val


def _build_exec_summary(ticker, fundamental, valuation, commentary,
                        management_quality, insider_activity, analyst_targets,
                        peer_comparison, periods, track_analysis=None,
                        risk=None) -> tuple[list, list]:
    """
    Returns (bullets, checks).

    bullets : plain-text strings (no leading '•'). Fixed slot order:
              valuation → fundamentals → management → risk → insider →
              watch. Slots are omitted when data is unavailable rather than
              emitting a placeholder.
    checks  : [(name, summary_value, table_value)] for every figure that has
              a table counterpart, so the caller can assert the two agree.

    Every numeric figure here is read from the SAME agent output dict the
    corresponding table renders from. The summary previously quoted the
    yfinance peer-table P/E for the subject while Section 3 showed the
    XBRL-derived one, so the two disagreed by a wide margin on the page a
    reader looks at first.
    """
    bullets = []
    checks: list = []

    # ── Slot 1: Valuation posture ──────────────────────────────────────────
    try:
        pc = (peer_comparison or {}).get("metrics", {})
        pe_pct    = (pc.get("pe_trailing") or {}).get("percentile")
        ev_pct    = (pc.get("ev_ebitda")   or {}).get("percentile")
        pe_subj   = (pc.get("pe_trailing") or {}).get("subject")
        pe_med    = (pc.get("pe_trailing") or {}).get("peer_median")
        industry  = (peer_comparison or {}).get("industry", "peers")

        # Average peer percentile across P/E and EV/EBITDA (both are
        # lower-is-better metrics where low percentile = expensive)
        pcts = [p for p in [pe_pct, ev_pct] if p is not None]
        avg_pct = sum(pcts) / len(pcts) if pcts else None

        # Subject P/E read from the same dicts Section 3 renders, so the
        # summary cannot quote a different number than the table. The peer
        # median stays on its yfinance basis (that is what the peer table
        # is), so both bases are named explicitly where they meet.
        _mr        = periods[0] if periods else None
        _pe_cur_t  = (valuation or {}).get("pe_current", {}) or {}
        _pe_hist_t = (valuation or {}).get("pe_historical", {}) or {}
        pe_cur_xbrl = _parse_rendered_number(_pe_cur_t.get(_mr))
        pe_fy_xbrl  = _parse_rendered_number(_pe_hist_t.get(_mr))
        if pe_cur_xbrl is not None:
            checks.append(("exec.pe_current", pe_cur_xbrl,
                           _parse_rendered_number(_pe_cur_t.get(_mr))))
        if pe_fy_xbrl is not None:
            checks.append(("exec.pe_fy_end", pe_fy_xbrl,
                           _parse_rendered_number(_pe_hist_t.get(_mr))))

        if avg_pct is not None:
            if avg_pct <= 33:
                valuation_stance = f"trades at a premium to {industry} peers"
            elif avg_pct >= 67:
                valuation_stance = f"trades at a discount to {industry} peers"
            else:
                valuation_stance = f"trades broadly in line with {industry} peers"

            if pe_cur_xbrl is not None and pe_med:
                pe_str = (f"P/E {pe_cur_xbrl:.1f}x (XBRL basis) vs. peer median "
                          f"{pe_med:.1f}x (yfinance basis) — see Section 3")
            elif pe_med:
                # Subject P/E unavailable or suppressed on the XBRL basis —
                # report the peer median alone rather than substituting the
                # yfinance subject figure the table doesn't show.
                pe_str = f"peer median P/E {pe_med:.1f}x (yfinance basis)"
            else:
                pe_str = ""
        else:
            # Fallback: current vs. FY-end P/E, both on the XBRL basis.
            # (This path previously read valuation["pe_fy_end"], a key that
            # does not exist, and treated pe_current as a scalar when it is
            # keyed by period — so it could never fire.)
            if pe_cur_xbrl is not None and pe_fy_xbrl is not None:
                if pe_cur_xbrl < pe_fy_xbrl * 0.85:
                    valuation_stance = "trades below recent historical multiples"
                elif pe_cur_xbrl > pe_fy_xbrl * 1.15:
                    valuation_stance = "trades above recent historical multiples"
                else:
                    valuation_stance = "trades in line with recent historical multiples"
                pe_str = (f"current P/E {pe_cur_xbrl:.1f}x vs. FY-end "
                          f"{pe_fy_xbrl:.1f}x (both XBRL basis)")
            else:
                valuation_stance = pe_str = None

        at = analyst_targets or {}
        upside = at.get("upside_pct")
        mean   = at.get("mean")
        upside_str = ""
        if upside is not None and mean is not None:
            direction = "upside" if upside > 0 else "downside"
            upside_str = (f"; sell-side mean target ${mean:,.2f} implies "
                         f"{abs(upside*100):.1f}% {direction}")

        if valuation_stance:
            parts = [f"Valuation — {ticker} {valuation_stance}"]
            if pe_str:
                parts.append(f"({pe_str})")
            if upside_str:
                parts.append(upside_str)
            bullets.append(" ".join(parts) + ".")

    except Exception:
        pass

    # ── Slot 2: Fundamental trajectory ────────────────────────────────────
    try:
        narr = commentary.get("narrative", [])
        cagr_str = fundamental.get("revenue_cagr", "")

        # Pull the most recent gross margin direction from narrative bullets
        gm_bullet = next((n for n in narr if "Gross Margin" in n), None)
        fcf_bullet = next((n for n in narr if "FCF" in n or "cash" in n.lower()), None)

        parts = []
        if cagr_str and cagr_str != "N/A":
            # Span was hardcoded as "2-yr" while the Section 1 note reads
            # "{len(periods)-1}-yr" off the same value — 4-yr for a 5-period
            # report. Same figure, contradictory label.
            _yrs = max(len(periods) - 1, 1) if periods else 1
            parts.append(f"revenue {_yrs}-yr CAGR {cagr_str}")
            checks.append(("exec.revenue_cagr",
                           _parse_rendered_number(cagr_str),
                           _parse_rendered_number(fundamental.get("revenue_cagr"))))

        if gm_bullet and "Gross Margin" not in set(
                fundamental.get("suppressed_metrics", []) or []):
            # Strip the bullet prefix to get a clean clause
            gm_clean = gm_bullet.lstrip("•").strip()
            parts.append(gm_clean[0].lower() + gm_clean[1:].rstrip("."))

        # FCF: flag thin or deteriorating margin
        # Same dict the FCF Margin row renders from. _parse_rendered_number
        # returns None for the sector-suppression sentinels ("N/A
        # (financials)"), so the summary inherits the table's suppression
        # instead of quoting a figure the table deliberately withholds.
        # Respect the table's sector suppression. FundamentalAgent still
        # computes fcf_margin for REITs (only financials get an explicit
        # "N/A (financials)" sentinel), but Section 1 removes the row for
        # any industry whose _METRIC_SUPPRESSION list names it — REITs show
        # FFO/AFFO margin instead. Without this gate the summary quoted a
        # figure the table deliberately withholds.
        _suppressed = set(fundamental.get("suppressed_metrics", []) or [])
        fcf_vals = fundamental.get("fcf_margin", {})
        if fcf_vals and periods and "FCF Margin" not in _suppressed:
            latest_fcf = _parse_rendered_number(fcf_vals.get(periods[0]))
            if latest_fcf is not None:
                if latest_fcf < 5:
                    parts.append(f"FCF margin thin at {latest_fcf:.1f}%")
                elif latest_fcf > 15:
                    parts.append(f"strong FCF margin {latest_fcf:.1f}%")
                checks.append(("exec.fcf_margin", latest_fcf,
                               _parse_rendered_number(fcf_vals.get(periods[0]))))

        if parts:
            bullets.append("Fundamentals — " + "; ".join(parts) + ".")

    except Exception:
        pass

    # ── Slot 3: Management credibility ────────────────────────────────────
    try:
        mq    = management_quality or {}
        score = mq.get("score")
        cred  = (track_analysis or {}).get("credibility")

        if score is not None:
            if score >= 80:
                track = "strong management track record"
            elif score >= 60:
                track = "adequate management track record"
            else:
                track = "limited or unestablished management track record"

            cred_clause = ""
            if cred is not None:
                cred_clause = f" (guidance credibility {cred:.0f}/100)"

            tone_clause = ""
            tone_flags = [f for f in commentary.get("flags", [])
                         if "divergence" in f.lower() or "tone" in f.lower()]
            if tone_flags:
                tone_clause = "; tone/delivery divergence flagged"

            bullets.append(
                f"Management — {track}{cred_clause}{tone_clause}."
            )

    except Exception:
        pass

    # ── Slot 4: Primary risk ───────────────────────────────────────────────
    try:
        all_flags = commentary.get("flags", [])
        if all_flags:
            # Priority: high-severity keywords first
            primary = None
            for f in all_flags:
                fl = f.lower()
                if any(kw in fl for kw in _HIGH_SEV_KEYWORDS):
                    primary = f
                    break
            if primary is None:
                primary = all_flags[0]

            # Clean up renderer markup (■ prefix, excess whitespace)
            clean = primary.lstrip("■").strip()
            # Truncate if very long
            if len(clean) > 220:
                clean = clean[:217] + "..."
            bullets.append(f"Key risk — {clean}")

    except Exception:
        pass

    # ── Slot 5: Insider signal ─────────────────────────────────────────────
    try:
        ia = insider_activity or {}
        cluster    = ia.get("cluster_buying")
        buy_count  = ia.get("buy_count", 0) or 0
        sell_count = ia.get("sell_count", 0) or 0
        net_value  = ia.get("net_value")
        lookback   = ia.get("lookback_days", 90)

        if cluster and buy_count >= 3:
            bullets.append(
                f"Insider activity — cluster buying signal: {buy_count} distinct "
                f"insiders bought in the past {lookback} days."
            )
        elif net_value is not None and net_value < -10_000_000:
            # Check if it's a controlling-family situation
            sellers_names = [t.get("insider_name","") for t in ia.get("transactions",[])
                            if t.get("transaction") == "SELL"]
            family_trust = any("trust" in n.lower() or "family" in n.lower()
                               or "foundation" in n.lower() for n in sellers_names)
            if family_trust:
                bullets.append(
                    f"Insider activity — ${abs(net_value)/1e6:,.0f}M net selling, "
                    f"primarily from a controlling family trust; routine liquidity "
                    f"vs. sentiment signal — treat with context."
                )
            else:
                bullets.append(
                    f"Insider activity — ${abs(net_value)/1e6:,.0f}M net selling "
                    f"by {sell_count} insider{'s' if sell_count!=1 else ''} "
                    f"over the past {lookback} days."
                )
        # No activity → slot omitted

    except Exception:
        pass

    # ── Slot 6: Watch item (conditional) ──────────────────────────────────
    try:
        watch_items = []
        cq = (risk or {}).get("credit_quality") or {}
        all_flags = commentary.get("flags", [])

        # Refinancing watch — only fire if BOTH conditions hold:
        # 1. nearest maturity is within ~12 months (this year or next)
        # 2. there's a large maturity concentration flag (>=25% single year
        #    or >=45% 3-year window, as computed by agents.py) OR a coverage/
        #    liquidity red flag already fired in Section 2 (current ratio or
        #    interest coverage below threshold), meaning the company may not
        #    be able to absorb the maturity trivially.
        nearest = cq.get("nearest_maturity")
        if nearest:
            try:
                import datetime
                nm_year = int(str(nearest)[:4])
                near_term = nm_year <= datetime.date.today().year + 1
                maturity_flags = cq.get("maturity_flags", [])
                coverage_flag = any(
                    "current ratio" in f.lower() or "interest coverage" in f.lower()
                    for f in all_flags
                )
                if near_term and (maturity_flags or coverage_flag):
                    watch_items.append(
                        f"debt maturity in {nearest} — refinancing watch "
                        f"({'large concentration' if maturity_flags else 'coverage flag in Section 2'})"
                    )
            except Exception:
                pass

        # Guidance credibility unestablished + Confident tone
        cred = (track_analysis or {}).get("credibility")
        tone_label = (commentary.get("tone") or {}).get("label", "")
        if cred is not None and cred == 50 and "confident" in tone_label.lower():
            watch_items.append(
                "management credibility unestablished with Confident tone — "
                "next guidance print is a key data point"
            )

        if watch_items:
            bullets.append("Watch — " + "; ".join(watch_items) + ".")

    except Exception:
        pass

    return bullets, checks


def check_summary_mismatches(ticker: str, checks: list) -> list[str]:
    """
    Every summary figure that has a table counterpart is asserted against
    it, in the same spirit as the glossary gap check: a future edit that
    reintroduces a parallel data path surfaces immediately instead of
    silently contradicting the body of the report.

    Shared by EquityBriefRenderer.render() (prints these as it goes) and
    scripts/validate_summary_reconciliation.py (collects them without
    rendering a PDF), so the two can never drift onto different rules for
    what counts as a mismatch.
    """
    lines = []
    for _name, _summary_val, _table_val in checks:
        if _summary_val is None or _table_val is None:
            continue
        if abs(_summary_val - _table_val) > 0.01:
            lines.append(f"[{ticker}] SUMMARY CHECK: {_name} "
                         f"summary={_summary_val} table={_table_val} — MISMATCH")
    return lines


# ─────────────────────────────────────────────
# Main renderer
# ─────────────────────────────────────────────

class EquityBriefRenderer:

    def render(self,
               ticker: str,
               sector: str,
               profile,                 # CompanyFinancialProfile
               fundamental: dict,       # FundamentalAgent output
               risk: dict,              # RiskAgent output
               valuation: dict,         # ValuationAgent output
               commentary: dict,        # TrendCommentaryAgent output
               guidance_analysis: dict = None,  # GuidanceAgent output
               track_analysis: dict = None,     # GuidanceTracker output
               structured_guidance_analysis: dict = None,  # LLM-based guidance analysis
               execution_quality: dict = None,  # ExecutionQuality output
               communication_quality: dict = None,  # CommunicationQuality output
               management_quality: dict = None,     # Composite score output
               ownership: dict = None,          # OwnershipLoader output
               insider_activity: dict = None,   # InsiderTransactionLoader output
               short_interest: dict = None,     # ShortInterestLoader output
               peer_comparison: dict = None,    # PeerComparisonLoader output
               company_overview: dict = None,   # CompanyOverviewLoader output
               analyst_targets: dict = None,    # AnalystTargetsLoader output
               price_context: dict = None,      # PriceContextLoader output
               estimate_revisions: dict = None, # EstimateRevisionsLoader output
               momentum: dict = None,          # MomentumLoader output
               author: str = "Research Team",
               output_path: str = None) -> str:
        """
        Build and save the PDF. Returns the output file path.
        """
        if output_path is None:
            output_path = f"{ticker}_equity_brief_{date.today().isoformat()}.pdf"

        styles = _build_styles()
        periods = fundamental["periods"]
        story = []

        # ── Rendered-label collection (drives the Appendix) ──────────────────
        # Every analytical table routes through _emit_table, which records its
        # row labels. The appendix is then built from that set, so it tracks
        # what was actually rendered instead of re-deriving the same sector
        # conditionals a second time. Descriptive tables (peers, price
        # context, momentum, insider, debt tranches) keep plain _ratio_table:
        # their first column holds data (years, tickers, tranche names), not
        # metric terms.
        _rendered_labels: set = set()

        def _emit_table(header_row, data_rows, **kw):
            _rendered_labels.update(str(r[0]).strip() for r in data_rows if r)
            return _ratio_table(header_row, data_rows, **kw)

        # ── Cover header table ──
        mc_str  = _fmt_market_cap(profile.market_cap)
        hdr_data = [[
            Paragraph(f"{ticker}", styles["h1"]),
            Paragraph(
                f"Sector: {sector}<br/>Market Cap: {mc_str}<br/>Report date: {date.today().isoformat()}",
                styles["h2"]
            ),
        ]]
        hdr_t = Table(hdr_data, colWidths=[80 * mm, A4[0] - 40 * mm - 80 * mm])
        hdr_t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), C_DARK),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(hdr_t)
        story.append(Spacer(1, 4))

        # ── Table of Contents (clickable internal links) ──
        # Sections 5-8 render conditionally further down; the TOC must only
        # link to anchors that will actually exist, or reportlab raises
        # "format not resolved ... undefined destination target" at save time.
        _narrative = commentary.get("narrative", [])
        _guidance  = commentary.get("management_guidance", [])
        _needs_p2  = bool(_narrative or _guidance)
        _ga_avail  = bool((guidance_analysis or {}).get("available")) or bool(_guidance)
        _own_avail = bool(ownership or fundamental.get("ownership")) or bool(insider_activity)

        # Section 2B renders only for industries with a meaningful operating
        # working-capital cycle (see _WC_SUPPRESSION in agents.py) and only
        # when at least one metric resolved — build its rows now so the TOC
        # links to an anchor that will actually exist.
        _wc_payload = fundamental.get("working_capital", {}) or {}
        _wc_rows    = _build_wc_rows(_wc_payload, periods)

        _TOC_SECTIONS = [
            ("s1", "1 · Fundamental Analysis"),
            ("s2", "2 · Risk & Solvency"),
        ]
        if _wc_rows:
            _TOC_SECTIONS.append(("s2b", "2B · Working Capital"))
        _TOC_SECTIONS += [
            ("s3", "3 · Valuation"),
            ("s4", "4 · Red Flags"),
        ]
        if _needs_p2:
            _TOC_SECTIONS.append(("s5", "5 · Trend Commentary"))
            if _ga_avail:
                _TOC_SECTIONS.append(("s6", "6 · Management Guidance & Tone"))
            # Section 7 always renders something — at minimum Mode C's
            # one-line "no transcript available" — so it always gets a TOC
            # anchor once page 2 exists at all.
            _TOC_SECTIONS.append(("s7", "7 · Management Track Record"))
        if _own_avail:
            _TOC_SECTIONS.append(("s8", "8 · Insider & Institutional Information"))
        _TOC_SECTIONS.append(("appendix", "Appendix · Abbreviations"))
        toc_parts = "  ·  ".join(
            f'<link href="#{anchor}" color="#2980B9">{title}</link>'
            for anchor, title in _TOC_SECTIONS
        )
        story.append(Paragraph(toc_parts, styles["meta"]))
        story.append(Spacer(1, 6))

        # ── Company Overview ──
        # summary: yfinance longBusinessSummary — safe, verified pattern.
        # segments: best-effort XBRL extraction, EXPERIMENTAL/unverified —
        # see company_overview.py docstring. Renders only if present; no
        # placeholder or "N/A" shown when absent, since this is optional
        # bonus content, not a primary report figure.
        co = company_overview or {}
        if co.get("summary") or co.get("segments"):
            if co.get("summary"):
                t_biz_hdr = Table(
                    [[Paragraph("Business Summary", styles["section"])]],
                    colWidths=["100%"]
                )
                t_biz_hdr.setStyle(TableStyle([
                    ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                    ("TOPPADDING",    (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 6),
                ]))
                story.append(t_biz_hdr)
                story.append(Spacer(1, 2))
                story.append(Paragraph(co["summary"], styles["body"]))
                story.append(Spacer(1, 4))

            if co.get("segments"):
                t_seg_hdr = Table(
                    [[Paragraph(f"Revenue by Segment  (10-K, {co.get('segment_period', '')}"
                               f" — experimental, unverified)", styles["section"])]],
                    colWidths=["100%"]
                )
                t_seg_hdr.setStyle(TableStyle([
                    ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                    ("TOPPADDING",    (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 6),
                ]))
                story.append(t_seg_hdr)
                story.append(Spacer(1, 2))

                segments = co["segments"]
                total = sum(segments.values()) or 1
                seg_header = ["Segment", "Revenue", "% of Total"]
                seg_rows = []
                for name, val in sorted(segments.items(), key=lambda kv: kv[1], reverse=True):
                    seg_rows.append([
                        name,
                        f"${val/1e6:,.0f}M" if val < 1e9 else f"${val/1e9:,.2f}B",
                        f"{val/total*100:.1f}%",
                    ])
                story.append(_ratio_table(seg_header, seg_rows))
                story.append(Spacer(1, 2))
                story.append(Paragraph(
                    "Segment breakdown is an experimental extraction from 10-K "
                    "XBRL and has not been independently verified against the "
                    "filing — cross-check before relying on it.",
                    styles["meta"]
                ))

            story.append(Spacer(1, 6))

        # ── Analyst Price Targets ──
        at = analyst_targets or {}
        if at.get("low") is not None or at.get("mean") is not None or at.get("high") is not None:
            t_at_hdr = Table(
                [[Paragraph("Analyst Price Targets  (yfinance)", styles["section"])]],
                colWidths=["100%"]
            )
            t_at_hdr.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(t_at_hdr)
            story.append(Spacer(1, 2))

            def _at_price_str(v):
                try:
                    return f"${float(v):,.2f}"
                except Exception:
                    return "N/A"

            current_str = _at_price_str(at.get("current"))
            fetched_date = at.get("fetched_date", "")
            story.append(Paragraph(
                f"Current Price: <b>{current_str}</b>  |  "
                f"Targets fetched: {fetched_date}",
                styles["body"]
            ))
            story.append(Spacer(1, 2))

            at_header = ["", "Current", "Low", "Median", "Mean", "High"]
            at_row = [
                "Price Target ($)",
                current_str,
                _at_price_str(at.get("low")),
                _at_price_str(at.get("median")),
                _at_price_str(at.get("mean")),
                _at_price_str(at.get("high")),
            ]
            story.append(_ratio_table(at_header, [at_row]))
            story.append(Spacer(1, 2))

            upside = at.get("upside_pct")
            upside_str = f"{upside*100:+.1f}%" if upside is not None else "N/A"
            story.append(Paragraph(
                f"Upside/downside to mean target: <b>{upside_str}</b>. "
                "Sell-side price targets are analyst opinions, not this "
                "pipeline's own valuation work — shown here for context; "
                "see Section 3 for this pipeline's own DCF/multiple-based "
                "valuation figures.",
                styles["meta"]
            ))
            story.append(Spacer(1, 6))

        # ── Price Context (52-week range, SMAs, volume) ──
        pc = price_context or {}
        if pc.get("low_52w") is not None and pc.get("high_52w") is not None:
            def _pc_pct(v):
                try:
                    return f"{float(v)*100:+.1f}%"
                except Exception:
                    return "N/A"
            def _pc_price(v):
                try:
                    return f"${float(v):,.2f}"
                except Exception:
                    return "N/A"
            def _pc_vol(v):
                try:
                    v = float(v)
                    return f"{v/1e6:.1f}M" if v >= 1e6 else f"{v:,.0f}"
                except Exception:
                    return "N/A"

            t_pc_hdr = Table(
                [[Paragraph(f"Price Context  (as of {pc.get('_as_of', '')})", styles["section"])]],
                colWidths=["100%"]
            )
            t_pc_hdr.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(t_pc_hdr)
            story.append(Spacer(1, 2))

            pc_header = ["Metric", "Value", "Context"]
            pc_rows   = []

            pos     = pc.get("position_pct")
            pos_str = f"{pos*100:.0f}% through 52w range" if pos is not None else ""
            pc_rows.append([
                "52w Range",
                f"{_pc_price(pc.get('low_52w'))} – {_pc_price(pc.get('high_52w'))}",
                f"Current {_pc_price(pc.get('current'))}  |  {pos_str}  |  "
                f"{_pc_pct(pc.get('pct_above_low'))} vs low  "
                f"{_pc_pct(pc.get('pct_below_high'))} vs high",
            ])

            for label, key in [("SMA 30d", "sma_30"), ("SMA 90d", "sma_90"), ("SMA 200d", "sma_200")]:
                if key in pc:
                    pc_rows.append([
                        label,
                        _pc_price(pc[key]),
                        f"Current {_pc_pct(pc.get(f'{key}_pct'))} vs {label}",
                    ])

            if "volume_last" in pc:
                vol_ctx = ""
                if "volume_avg_90" in pc:
                    vol_ctx = (f"90d avg: {_pc_vol(pc['volume_avg_90'])}  |  "
                               f"vs avg: {_pc_pct(pc.get('volume_vs_avg'))}")
                pc_rows.append(["Volume (last)", _pc_vol(pc["volume_last"]), vol_ctx])

            story.append(_ratio_table(pc_header, pc_rows))
            story.append(Spacer(1, 6))

        # ── Momentum / Relative Strength ──
        mo = momentum or {}
        if mo.get("periods"):
            etf_label = mo.get("etf") or "Sector ETF"
            t_mo_hdr = Table(
                [[Paragraph(f"Return vs. Peers & Benchmarks  (as of {mo.get('_as_of', '')})", styles["section"])]],
                colWidths=["100%"]
            )
            t_mo_hdr.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), C_DARK),
                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(t_mo_hdr)

            mo_header = ["Period", "Stock", "vs SPY", f"vs {etf_label}", "Peer Rank"]
            mo_rows   = []
            for label in ["1m", "3m", "6m", "12m", "3y", "5y"]:
                p = mo["periods"].get(label)
                if not p:
                    continue
                def _pct(v): return f"{v*100:+.1f}%" if v is not None else "N/A"
                rank = p.get("peer_rank")
                rank_str = f"{rank:.0f}th" if rank is not None else "N/A"
                mo_rows.append([
                    label,
                    _pct(p.get("stock_return")),
                    _pct(p.get("vs_spy")),
                    _pct(p.get("vs_etf")),
                    rank_str,
                ])
            if mo_rows:
                story.append(_ratio_table(mo_header, mo_rows))
                story.append(Spacer(1, 2))
                peers_str = ", ".join(mo.get("_peers", [])) or "N/A"
                story.append(Paragraph(
                    f"Returns over trailing trading days (1m=21, 3m=63, 6m=126, 12m=252, 3y=756, 5y=1260). " \
                    f"3y/5y use earliest available price when full history unavailable (e.g. recent IPOs). "
                    f"vs SPY = stock return minus S&P 500 return. "
                    f"vs {etf_label} = stock return minus sector ETF return. "
                    f"Peer rank = percentile vs industry peers (100th = beat all peers). "
                    f"Peers: {peers_str}.",
                    styles["meta"]
                ))
            story.append(Spacer(1, 6))

        # ── Estimate Revisions ──
        er = estimate_revisions or {}
        if er.get("periods"):
            t_er_hdr = Table(
                [[Paragraph(f"Estimate Revisions  (as of {er.get('_as_of', '')})", styles["section"])]],
                colWidths=["100%"]
            )
            t_er_hdr.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(t_er_hdr)
            story.append(Spacer(1, 2))

            def _er_est(v, is_rev=False):
                """Format an estimate value — EPS as $X.XX, revenue as $XB/$XM."""
                try:
                    f = float(v)
                    if is_rev:
                        if abs(f) >= 1e9:
                            return f"${f/1e9:.2f}B"
                        elif abs(f) >= 1e6:
                            return f"${f/1e6:.1f}M"
                        else:
                            return f"${f:,.0f}"
                    else:
                        return f"${f:.2f}"
                except Exception:
                    return "N/A"

            def _er_trend(trend, pct):
                try:
                    pct_str = f" ({float(pct)*100:+.1f}%)" if pct is not None else ""
                    return f"{trend}{pct_str}"
                except Exception:
                    return trend or "N/A"

            _PERIOD_ORDER = ["0q", "0y", "+1y"]

            # ── EPS revision table ────────────────────────────────────────────
            eps_header = ["Period", "Current Est.", "30d Ago", "60d Ago", "90d Ago", "Trend (90d)"]
            eps_rows   = []
            for pk in _PERIOD_ORDER:
                pdata = er["periods"].get(pk)
                if not pdata:
                    continue
                m = pdata.get("eps")
                if not m or m.get("current") is None:
                    continue
                eps_rows.append([
                    pdata.get("label", pk),
                    _er_est(m.get("current")),
                    _er_est(m.get("ago_30d")) if m.get("ago_30d") is not None else "N/A",
                    _er_est(m.get("ago_60d")) if m.get("ago_60d") is not None else "N/A",
                    _er_est(m.get("ago_90d")) if m.get("ago_90d") is not None else "N/A",
                    _er_trend(m.get("trend"), m.get("pct_change")),
                ])

            if eps_rows:
                story.append(Paragraph("<b>EPS Estimates</b>", styles["body"]))
                story.append(_ratio_table(eps_header, eps_rows))
                story.append(Spacer(1, 2))
                story.append(Paragraph(
                    "Trend = direction of estimate revision vs. 90 days ago "
                    "(Rising >+1%, Falling <-1%, Stable within +/-1%).",
                    styles["meta"]
                ))
                story.append(Spacer(1, 6))

            # ── Revenue table ─────────────────────────────────────────────────
            rev_header = ["Period", "Current Est.", "Prior Year", "YoY Growth"]
            rev_rows   = []
            for pk in _PERIOD_ORDER:
                pdata = er["periods"].get(pk)
                if not pdata:
                    continue
                m = pdata.get("revenue")
                if not m or m.get("current") is None:
                    continue
                pct = m.get("pct_change")
                rev_rows.append([
                    pdata.get("label", pk),
                    _er_est(m.get("current"),  is_rev=True),
                    _er_est(m.get("year_ago"), is_rev=True) if m.get("year_ago") is not None else "N/A",
                    f"{pct*100:+.1f}%" if pct is not None else "N/A",
                ])

            if rev_rows:
                story.append(Paragraph("<b>Revenue Estimates</b>", styles["body"]))
                story.append(_ratio_table(rev_header, rev_rows))
                story.append(Spacer(1, 2))
                story.append(Paragraph(
                    "YoY Growth = consensus revenue growth vs. prior year actuals. "
                    "Source: yfinance eps_trend + revenue_estimate (sell-side consensus).",
                    styles["meta"]
                ))
            story.append(Spacer(1, 6))

        # ── Executive Summary ──
        # Deterministic template — no API call. Pulls from structured outputs
        # already computed elsewhere in the render call. Fixed slot order:
        # valuation → fundamentals → management → primary risk → insider → watch.
        _exec_bullets, _exec_checks = _build_exec_summary(
            ticker         = ticker,
            fundamental    = fundamental,
            valuation      = valuation,
            commentary     = commentary,
            management_quality = management_quality,
            insider_activity   = insider_activity,
            analyst_targets    = analyst_targets,
            peer_comparison    = peer_comparison,
            periods        = periods,
            track_analysis = track_analysis,
            risk           = risk,
        )
        for _line in check_summary_mismatches(ticker, _exec_checks):
            print(_line)
        if _exec_bullets:
            t_exec_hdr = Table(
                [[Paragraph("Executive Summary", styles["section"])]],
                colWidths=["100%"]
            )
            t_exec_hdr.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(t_exec_hdr)
            story.append(Spacer(1, 2))
            for bullet in _exec_bullets:
                story.append(Paragraph(f"• {bullet}", styles["bullet"]))
            story.append(Spacer(1, 6))

        # ── Section 1: Fundamentals ──
        story += _section_header("1 · Fundamental Analysis", styles, anchor="s1")

        gm_label = fundamental.get("gross_margin_label", "Gross Margin")
        sg = fundamental.get("sector_group", "general")

        # ── TTM column (trailing twelve months, via quarterly aggregation) ──
        # Prepended as the first data column, before FY1. Only flow-based
        # margins have a TTM figure -- see the "ttm" dict's construction in
        # agents.py for why ROE/ROA/turnover/etc. are intentionally absent
        # (would need a most-recent-quarter balance sheet this pipeline
        # doesn't fetch). Rows without a TTM figure show "—" in that column
        # so the FY columns stay aligned.
        _ttm = fundamental.get("ttm") or {"available": False}
        _ttm_on = _ttm.get("available", False)
        _ttm_dagger = "†" if (_ttm_on and _ttm.get("is_partial")) else ""
        fund_header = (["Metric"] + ([f"TTM{_ttm_dagger}"] if _ttm_on else [])
                      + [_short_period(p) for p in periods])

        def _ttm_cell(key: str) -> list:
            if not _ttm_on:
                return []
            if sg == "financials" and key in ("gross_margin", "operating_margin", "net_margin"):
                # TTM Revenue for Financial Services resolves through the same
                # base "Revenue" candidate list the TTM path shares with the
                # annual waterfall (InterestIncomeExpenseNet, i.e. NII alone)
                # -- but the ANNUAL pipeline's Revenue additionally goes
                # through a bank-specific post-processing step in
                # facts_processor.load_data() (NII + NonInterestIncome, with
                # capping against "Revenues") that the TTM path doesn't
                # replicate. Confirmed live for JPM: TTM Revenue = NII alone
                # ($99.8B) vs. the annual pipeline's properly-adjusted $182.4B
                # (yfinance: $186.3B) -- an ~46% understatement that would
                # otherwise inflate TTM Net Margin to a nonsensical 65%
                # (real: ~30-35%). Suppressed until TTM replicates that
                # adjustment; gross_margin was already suppressed for the
                # unrelated reason that NIM isn't computed by the TTM bundle
                # at all.
                return ["—"]
            return [_ttm.get(key, "—")]

        # Build fundamentals rows dynamically — only show rows meaningful for the sector
        fund_rows = []
        fund_rows.append(
            [gm_label] + _ttm_cell("gross_margin")
            + [fundamental["gross_margin"].get(p, "N/A") for p in periods]
        )
        fund_rows.append(
            ["Operating Margin"] + _ttm_cell("operating_margin")
            + [fundamental["operating_margin"].get(p, "N/A") for p in periods]
        )
        if sg != "financials":
            fund_rows.append(
                ["EBITDA Margin"] + _ttm_cell("ebitda_margin")
                + [fundamental["ebitda_margin"].get(p, "N/A") for p in periods]
            )
        fund_rows.append(
            ["Net Margin"] + _ttm_cell("net_margin")
            + [fundamental["net_margin"].get(p, "N/A") for p in periods]
        )
        fund_rows.append(
            ["ROE"] + (["—"] if _ttm_on else []) + [fundamental["roe"].get(p, "N/A") for p in periods]
        )
        fund_rows.append(
            ["ROA"] + (["—"] if _ttm_on else []) + [fundamental["roa"].get(p, "N/A") for p in periods]
        )
        if sg == "financials":
            fund_rows.append(
                ["Efficiency Ratio"] + (["—"] if _ttm_on else [])
                + [fundamental["efficiency_ratio"].get(p, "N/A") for p in periods]
            )
            fund_rows.append(
                ["ROTCE"] + (["—"] if _ttm_on else [])
                + [fundamental["rotce"].get(p, "N/A") for p in periods]
            )
        else:
            fund_rows.append(
                ["ROIC (proxy)"] + (["—"] if _ttm_on else [])
                + [fundamental["roic_proxy"].get(p, "N/A") for p in periods]
            )
        fund_rows.append(
            ["Asset Turnover"] + (["—"] if _ttm_on else [])
            + [fundamental["asset_turnover"].get(p, "N/A") for p in periods]
        )
        if sg == "general":
            fund_rows.append(
                ["Inv. Turnover"] + (["—"] if _ttm_on else [])
                + [fundamental["inventory_turnover"].get(p, "N/A") for p in periods]
            )
            fund_rows.append(
                ["Days Sales of Inv."] + (["—"] if _ttm_on else [])
                + [fundamental["dsi"].get(p, "N/A") for p in periods]
            )
        if sg == "real_estate" and fundamental.get("ffo_available"):
            # REITs: FFO/AFFO (NAREIT cash-flow standard) replace FCF Margin,
            # which is a GAAP-CFO-derived metric distorted by real-estate D&A
            # for this sector (see CFO/EV suppression note in Section 3).
            # Not TTM'd -- FFO/AFFO aren't in _compute_ttm_bundle (see Stage 3
            # scope note: only the standard flow/margin set is TTM'd there).
            def _fmt_ffo_row_val(v):
                if not isinstance(v, (int, float)):
                    return "N/A"
                return f"${v/1e6:,.0f}M"
            fund_rows.append(
                ["FFO"] + (["—"] if _ttm_on else [])
                + [_fmt_ffo_row_val(fundamental["ffo"].get(p)) for p in periods]
            )
            fund_rows.append(
                ["FFO Margin"] + (["—"] if _ttm_on else [])
                + [fundamental["ffo_margin"].get(p, "N/A") for p in periods]
            )
            affo_label = "AFFO†" if fundamental.get("affo_uses_total_capex") else "AFFO"
            fund_rows.append(
                [affo_label] + (["—"] if _ttm_on else [])
                + [_fmt_ffo_row_val(fundamental["affo"].get(p)) for p in periods]
            )
            fund_rows.append(
                ["AFFO Margin"] + (["—"] if _ttm_on else [])
                + [fundamental["affo_margin"].get(p, "N/A") for p in periods]
            )
        elif sg != "financials":
            fund_rows.append(
                ["FCF Margin"] + _ttm_cell("fcf_margin")
                + [fundamental["fcf_margin"].get(p, "N/A") for p in periods]
            )
            # SBC rows sit in this same branch deliberately: they must be
            # suppressed exactly where FCF Margin is, and sharing the branch
            # (plus the _SUPPRESSION_ROW_ALIASES entry below) means there is
            # one suppression decision, not two that could drift apart.
            # Not TTM'd -- SBC/Revenue and FCF-after-SBC need a TTM SBC/
            # Revenue pairing this stage doesn't build; "—" keeps columns
            # aligned rather than mixing a TTM numerator with an FY ratio.
            _sbc = fundamental.get("sbc", {}) or {}
            if _sbc.get("available"):
                fund_rows.append(
                    ["SBC / Revenue"] + (["—"] if _ttm_on else [])
                    + [_fmt_pct1(_sbc.get("sbc_pct_revenue", {}).get(p)) for p in periods]
                )
                fund_rows.append(
                    ["FCF after SBC"] + (["—"] if _ttm_on else [])
                    + [_fmt_nwc(_sbc.get("fcf_after_sbc", {}).get(p)) for p in periods]
                )
                fund_rows.append(
                    ["FCF after SBC Mgn"] + (["—"] if _ttm_on else [])
                    + [_fmt_pct1(_sbc.get("fcf_after_sbc_margin", {}).get(p)) for p in periods]
                )

        # ── Sector-appropriate metric suppression (display-only) ──────────
        # Removes rows for metrics FundamentalAgent flagged as not
        # meaningful for this industry (_METRIC_SUPPRESSION in agents.py).
        # Underlying values are still computed above — only the row is
        # hidden here. See the "Basis of Omission" footnote block below.
        suppressed_metrics    = fundamental.get("suppressed_metrics", []) or []
        suppression_footnotes = fundamental.get("suppression_footnotes", {}) or {}
        if suppressed_metrics:
            _suppress_row_labels = set()
            for m in suppressed_metrics:
                _alias = _SUPPRESSION_ROW_ALIASES.get(m)
                if _alias is None:
                    continue
                if isinstance(_alias, tuple):
                    _suppress_row_labels.update(_alias)
                else:
                    _suppress_row_labels.add(_alias)
            fund_rows = [row for row in fund_rows if row[0] not in _suppress_row_labels]

        # Add revenue CAGR as a single merged row note
        cagr_note = f"Revenue {len(periods)-1}-yr CAGR: {fundamental.get('revenue_cagr', 'N/A')}"
        _fund_rows_shown = _drop_all_na(fund_rows)
        story.append(_emit_table(fund_header, _fund_rows_shown))
        story.append(Spacer(1, 2))
        story.append(Paragraph(cagr_note, styles["meta"]))
        # SBC footnote — only when those rows actually survived both the
        # sector suppression and the all-N/A drop.
        if any(r[0] == "SBC / Revenue" for r in _fund_rows_shown):
            story.append(Paragraph(
                "FCF after SBC treats stock compensation as a real economic "
                "cost. GAAP FCF above excludes it. Analysts differ on this "
                "treatment; both are shown.",
                styles["meta"]
            ))
        if sg == "real_estate" and fundamental.get("ffo_available"):
            _ffo_note = (
                "FFO = Net Income + Real Estate D&A − Gains on Sale of RE + Impairment "
                "(NAREIT definition). AFFO = FFO − Straight-line Rent − Lease Amortization "
                "− Maintenance CapEx + Non-cash Stock Comp + Non-cash Interest. "
                "Where a component doesn't resolve from filed XBRL, it is simply omitted "
                "from the sum (treated as zero) rather than blocking the calculation."
            )
            if fundamental.get("affo_uses_total_capex"):
                _ffo_note += (
                    " † No filer-tagged maintenance/recurring CapEx concept was found — "
                    "AFFO uses total CapEx as a conservative proxy."
                )
            story.append(Paragraph(_ffo_note, styles["meta"]))

        # ── Shareholder Returns sub-block ──
        # Dollars and payout ratios come from FundamentalAgent; the three
        # yield rows and the share-count change come from ValuationAgent,
        # which owns the FY-end market cap and the split/unit-anomaly
        # suppression those rows must honour.
        _sr  = fundamental.get("shareholder_returns", {}) or {}
        _sry = (valuation.get("shareholder_yields", {}) or {})
        if _sr.get("available"):
            def _sr_row(label, series, fmt):
                # This table reuses fund_header (Section 1's), which gains a
                # TTM column when TTM is available -- these rows must match
                # or every FY value silently shifts one column left. None of
                # these are TTM'd here (yields/payout ratios need a TTM
                # market cap and net-of-issuance buyback figure this stage
                # doesn't build), so it's always "—", never a real value.
                return ([label] + (["—"] if _ttm_on else [])
                       + [fmt(series.get(p)) for p in periods])

            _basis = _sr.get("payout_basis", "FCF")
            sr_rows = [
                _sr_row("Dividends Paid", _sr.get("dividends_paid", {}), _fmt_nwc),
                _sr_row("Net Buybacks",   _sr.get("net_buyback", {}),    _fmt_nwc),
                _sr_row("Dividend Yield", _sry.get("dividend_yield", {}), _fmt_pct2),
                _sr_row("Buyback Yield",  _sry.get("buyback_yield", {}),  _fmt_pct2),
                _sr_row("Total Shareholder Yld", _sry.get("total_yield", {}), _fmt_pct2),
                # When the basis IS earnings (financials), the dedicated
                # earnings row below already carries it -- don't print the
                # same series twice under two labels.
                *([] if _basis == "Earnings" else
                  [_sr_row(f"Payout (% of {_basis})",
                           _sr.get("payout_ratio_fcf", {}), _fmt_pct1)]),
                _sr_row("Payout (% of Earnings)",
                        _sr.get("payout_ratio_earnings", {}), _fmt_pct1),
                _sr_row("Diluted Share Δ YoY",
                        _sry.get("share_change_yoy", {}), _fmt_signed_pct1),
            ]
            sr_rows = _drop_all_na(sr_rows)
            if sr_rows:
                story.append(Spacer(1, 4))
                t_sr = Table(
                    [[Paragraph("Shareholder Returns", styles["section"])]],
                    colWidths=["100%"]
                )
                t_sr.setStyle(TableStyle([
                    ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                    ("TOPPADDING",    (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 6),
                ]))
                story.append(t_sr)
                story.append(Spacer(1, 1))
                story.append(_emit_table(fund_header, sr_rows))
                story.append(Spacer(1, 2))
                _sr_note = (
                    "Net Buybacks = share repurchases less equity issuance "
                    "(employee plans, option exercises). Yields are computed "
                    "against FY-end market cap (FY-end price × diluted shares), "
                    "the same basis as EV elsewhere in this report."
                )
                if _basis == "AFFO":
                    _sr_note += (
                        " Payout is shown against AFFO rather than FCF: AFFO is "
                        "the NAREIT distribution base for REITs, and GAAP FCF is "
                        "distorted by real-estate depreciation for this sector."
                    )
                story.append(Paragraph(_sr_note, styles["meta"]))

        # Operating leverage rows (suppress for financials)
        ol = fundamental.get("operating_leverage", {})
        if ol and sg != "financials":
            ol_str = "  |  ".join(f"{_short_ol_key(k)}: {v}" for k, v in ol.items())
            story.append(Paragraph(f"Operating leverage: {ol_str}", styles["meta"]))

        # ── Basel III capital ratios (financials only) ──
        basel = fundamental.get("basel", {})
        if sg == "financials" and basel:
            story.append(Spacer(1, 4))
            # Sub-header
            t_sub = Table(
                [[Paragraph("Basel III Capital Adequacy", styles["section"])]],
                colWidths=["100%"]
            )
            t_sub.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(t_sub)
            story.append(Spacer(1, 1))

            b_header = ["Metric"] + [_short_period(p) for p in periods]
            b_rows = [
                ["Tier 1 Capital  (min 8.5%)"]   + [basel.get(p, {}).get("tier1",         "N/A") for p in periods],
                ["Total Capital  (min 10.5%)"]   + [basel.get(p, {}).get("total_capital", "N/A") for p in periods],
                ["T1 Leverage/avg assets  (min 4%)"]  + [basel.get(p, {}).get("lev_ratio",     "N/A") for p in periods],
            ]
            story.append(_emit_table(b_header, _drop_all_na(b_rows)))
            story.append(Spacer(1, 2))
            story.append(Paragraph(
                "Source: FR Y-9C Schedule HC-R (FFIEC bulk data).  "
                "Benchmarks: CET1 >=7.0% | Tier 1 >=8.5% | Total Capital >=10.5% | T1 Leverage >=4.0% "
                "(Basel III minimums incl. 2.5% conservation buffer).",
                styles["meta"]
            ))
            story.append(Paragraph(
                "CET1 ratio: G-SIBs (JPM, BAC, WFC, GS) file CET1 under FFIEC 101 — a separate "
                "regulatory report not included in the FR Y-9C bulk file. "
                "Obtain prior-year BHCF files from the FFIEC portal to populate FY24/FY23 columns.",
                styles["meta"]
            ))
            story.append(Paragraph(
                "T1 Leverage (Tier 1 / avg total assets) differs from the Supplementary Leverage Ratio "
                "(SLR) reported in bank press releases. SLR uses a broader denominator "
                "(on + off-balance-sheet exposures), producing a lower figure (~5.5-6.0% vs ~6.5-7.0%).",
                styles["meta"]
            ))

        # ── Basis of Omission (only when metrics were suppressed above) ──
        if suppression_footnotes:
            story.append(Spacer(1, 4))
            story.append(HRFlowable(
                width="100%", thickness=0.5, color=C_GREY,
                spaceBefore=0, spaceAfter=3,
            ))
            story.append(Paragraph("<i>Basis of Omission</i>", styles["meta"]))
            # Group metrics that share identical footnote text to avoid repetition
            _groups: dict[str, list] = {}
            for _metric, _text in suppression_footnotes.items():
                _groups.setdefault(_text, []).append(_metric)
            for _text, _metrics in _groups.items():
                story.append(Paragraph(
                    f"<b>{', '.join(_metrics)}</b> — {_text}", styles["meta"]
                ))

        story.append(Spacer(1, 6))

        # ── Section 2: Risk & Solvency ──
        story += _section_header("2 · Risk & Solvency", styles, anchor="s2")

        risk_header = ["Metric"] + [_short_period(p) for p in periods]

        # Build risk rows dynamically — only show rows meaningful for the sector
        risk_rows = []
        risk_rows.append(
            ["Current Ratio"] + [risk["current_ratio"].get(p, "N/A") for p in periods]
        )
        risk_rows.append(
            ["Quick Ratio"] + [risk["quick_ratio"].get(p, "N/A") for p in periods]
        )
        risk_rows.append(
            ["D/E Ratio"] + [risk["de_ratio"].get(p, "N/A") for p in periods]
        )
        if sg == "energy":
            # Debt/Capital is more standard than D/E for E&P capital structure analysis
            risk_rows.append(
                ["Debt/Capital"] + [risk["debt_capital"].get(p, "N/A") for p in periods]
            )
        if sg != "financials":
            risk_rows.append(
                ["Net Debt/EBITDA"] + [risk["debt_ebitda"].get(p, "N/A") for p in periods]
            )
        risk_rows.append(
            ["Interest Coverage"] + [risk["interest_coverage"].get(p, "N/A") for p in periods]
        )
        story.append(_emit_table(risk_header, _drop_all_na(risk_rows)))

        # Altman Z (most recent only; suppress for financials and energy)
        z_val = risk["altman_z"].get(periods[0], "N/A") if periods else "N/A"
        story.append(Spacer(1, 2))
        if sg == "general":
            story.append(Paragraph(
                f"Altman Z-Score (most recent): {z_val}   "
                f"[Safe >2.99 | Grey 1.81–2.99 | Distress <1.81]",
                styles["meta"]
            ))
        else:
            story.append(Paragraph(
                f"Altman Z-Score: {z_val}",
                styles["meta"]
            ))
        if risk.get("de_trend_note"):
            story.append(Paragraph(f"Leverage trend: {risk['de_trend_note']}", styles["meta"]))
        # Derived interest-expense estimate, shown only when the filed line
        # didn't resolve while debt is outstanding (see RiskAgent guard).
        if risk.get("est_interest_expense"):
            story.append(Paragraph(
                f"Est. Interest Expense: {risk['est_interest_expense']}",
                styles["meta"]
            ))

        # ── Credit Quality (sub-section within Risk & Solvency) ──
        cq = risk.get("credit_quality", {})
        if cq.get("available"):
            story.append(Spacer(1, 4))
            t_cq = Table(
                [[Paragraph("Credit Quality", styles["section"])]],
                colWidths=["100%"]
            )
            t_cq.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(t_cq)
            story.append(Spacer(1, 2))

            # ── Ratings line ──
            sp  = cq.get("sp_rating",     "N/A")
            mdy = cq.get("moodys_rating", "N/A")
            fit = cq.get("fitch_rating",  "N/A")
            outlook     = cq.get("outlook",      "N/A")
            rating_date = cq.get("rating_as_of", "N/A")
            rating_parts = []
            if sp  and sp  != "N/A": rating_parts.append(f"S&amp;P: {sp}")
            if mdy and mdy != "N/A": rating_parts.append(f"Moody's: {mdy}")
            if fit and fit != "N/A": rating_parts.append(f"Fitch: {fit}")
            rating_str = "  |  ".join(rating_parts) if rating_parts else "N/A"
            story.append(Paragraph(
                f"<b>Credit Ratings:</b>  {rating_str}  —  Outlook: {outlook}  "
                f"<font color='#7F8C8D'>(as of {rating_date})</font>",
                styles["body"]
            ))
            story.append(Spacer(1, 2))

            # ── Market metrics table ──
            cq_rows = [
                ["Metric", "Value"],
                ["Risk-Free Rate (10Y UST)",  cq.get("risk_free",    "N/A")],
                ["OAS Credit Spread",
                 f"{cq.get('oas_spread', 'N/A')}  [{cq.get('oas_tier', '')} tier  |  ICE BofA]"],
                ["Implied Cost of Debt",      cq.get("cost_of_debt", "N/A")],
                ["Equity Risk Premium (ERP)", cq.get("erp",          "N/A")],
                ["Country Risk Premium (CRP)",cq.get("crp",          "N/A")],
            ]
            if cq.get("wtd_avg_rate") and cq["wtd_avg_rate"] != "N/A":
                cq_rows.append(["Wtd Avg Effective Rate", cq["wtd_avg_rate"]])
            if cq.get("total_debt_m") and cq["total_debt_m"] != "N/A":
                cq_rows.append(["Total Debt Outstanding", cq["total_debt_m"]])

            # ── Largest single-year maturity tranche vs. EV / total debt ──
            _lm_amount = cq.get("largest_maturity_amount")
            if _lm_amount:
                _lm_parts = [f"${_lm_amount/1e6:,.0f}M ({cq.get('largest_maturity_year', '')})"]
                _lm_pct_ev = cq.get("largest_maturity_pct_ev")
                if isinstance(_lm_pct_ev, (int, float)):
                    _lm_parts.append(f"{_lm_pct_ev:.1f}% of EV")
                _lm_pct_debt = cq.get("largest_maturity_pct_debt")
                if isinstance(_lm_pct_debt, (int, float)):
                    _lm_parts.append(f"{_lm_pct_debt:.1f}% of debt")
                cq_rows.append(["Largest Maturity", " | ".join(_lm_parts)])
            else:
                cq_rows.append(["Largest Maturity", "N/A (maturity schedule unavailable)"])

            if cq.get("nearest_maturity") and cq["nearest_maturity"] != "N/A":
                cq_rows.append(["Nearest Maturity", cq["nearest_maturity"]])
            story.append(_emit_table(cq_rows[0], cq_rows[1:]))
            story.append(Spacer(1, 2))

            # ── Debt schedule: top 5 tranches (when available) ──
            tranches = cq.get("tranches", [])
            if tranches:
                # Detect format: if any tranche has rate_range it's Format B
                is_range_format = any(t.get("rate_range") for t in tranches)
                story.append(Paragraph("<b>Debt Schedule (top tranches by issuance)</b>",
                                       styles["meta"]))
                story.append(Spacer(1, 2))
                if is_range_format:
                    tranche_header = ["Tranche", "Rate Range"]
                    tranche_rows = [
                        [
                            t.get("name", ""),
                            t.get("rate_range") or "N/A",
                        ]
                        for t in tranches[:5]
                    ]
                else:
                    tranche_header = ["Tranche", "Stated Rate", "Eff. Rate"]
                    tranche_rows = [
                        [
                            t.get("name", ""),
                            f"{t['stated_rate']:.3f}%" if t.get("stated_rate") else "N/A",
                            f"{t['effective_rate']:.2f}%" if t.get("effective_rate") else "N/A",
                        ]
                        for t in tranches[:5]
                    ]
                story.append(_ratio_table(tranche_header, tranche_rows))
                story.append(Spacer(1, 2))
            else:
                # Tranche disclaimer — shown when debt note exists but tranches unparseable
                if cq.get("total_debt_m") and cq["total_debt_m"] != "N/A":
                    story.append(Paragraph(
                        "<i>Individual debt tranches not shown — instrument-level detail "
                        "varies by filer format. Total debt and maturity profile sourced "
                        "from SEC XBRL filings.</i>",
                        styles["meta"]
                    ))
                    story.append(Spacer(1, 2))

            # ── Maturity Wall ──
            wall  = cq.get("maturity_wall", {})
            flags = cq.get("maturity_flags", [])
            wam   = cq.get("wam_years")

            if wall:
                story.append(Spacer(1, 4))
                story.append(Paragraph(
                    "<b>Debt Maturity Profile</b>",
                    styles["meta"]
                ))
                story.append(Spacer(1, 2))

                # Build maturity wall table with per-row concentration colouring
                # Rank ALL rows by pct ascending: rank 1=lowest, n=highest
                # "thereafter" is included — it is real debt with the highest concentration
                all_items = sorted(wall.items(), key=lambda x: x[1]["pct"])
                n_rows    = len(all_items)
                rank_map  = {yr: i + 1 for i, (yr, _) in enumerate(all_items)}

                wall_header = ["Year", "Amount", "% of Total", ""]
                wall_rows   = []

                for yr_label, data in wall.items():
                    amt_m         = data["amount_m"]
                    pct           = data["pct"]
                    yr_str        = str(yr_label)
                    is_thereafter = "thereafter" in yr_str.lower()

                    if pct >= 25 and not is_thereafter:
                        yr_str = f"{yr_str} ⚠"

                    circle_color = _heat_color(rank_map[yr_label], n_rows)

                    wall_rows.append([
                        yr_str,
                        f"${amt_m:,.0f}M",
                        f"{pct:.1f}%",
                        _HeatCircle(circle_color, radius=5),
                    ])

                page_w     = A4[0] - 40 * mm
                col_widths = [38*mm, 32*mm, 24*mm, 12*mm]

                wrapped_header = [Paragraph(str(c), _CELL_STYLE_HEADER)
                                  for c in wall_header]
                wrapped_data = [
                    [
                        Paragraph(row[0], _CELL_STYLE),
                        _cell(row[1]),
                        _cell(row[2]),
                        row[3],   # _HeatCircle flowable
                    ]
                    for row in wall_rows
                ]

                all_rows = [wrapped_header] + wrapped_data
                mat_t    = Table(all_rows, colWidths=col_widths, repeatRows=1)
                mat_style = [
                    ("BACKGROUND",    (0, 0), (-1, 0),  C_DARK),
                    ("TEXTCOLOR",     (0, 0), (-1, 0),  C_WHITE),
                    ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
                    ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
                    ("TOPPADDING",    (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
                    ("ALIGN",         (1, 0), (-1, -1), "CENTER"),
                    ("ALIGN",         (0, 0), (0,  -1), "LEFT"),
                    ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                    ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                    ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
                ]
                mat_t.setStyle(TableStyle(mat_style))
                if n_rows <= 8:
                    story.append(KeepTogether(mat_t))
                else:
                    story.append(mat_t)
                story.append(Spacer(1, 2))

                meta_parts = []
                if wam:
                    meta_parts.append(f"Weighted avg maturity: <b>{wam:.1f} yrs</b>")
                if flags:
                    for f in flags:
                        meta_parts.append(f"<font color='#E74C3C'>⚠ {f}</font>")
                if meta_parts:
                    story.append(Paragraph(
                        "  ·  ".join(meta_parts),
                        styles["meta"]
                    ))
                    story.append(Spacer(1, 2))

                # Colour legend
                story.append(Paragraph(
                    "<font color='#2471A3'>●</font> Smallest maturity tranche  "
                    "  <font color='#C0392B'>●</font> Largest maturity tranche",
                    styles["meta"]
                ))
                story.append(Spacer(1, 2))

            elif cq.get("maturities"):
                mat_items = sorted(cq["maturities"].items())
                mat_str   = "  |  ".join(
                    f"{yr}: ${amt:,.0f}M" for yr, amt in mat_items
                )
                story.append(Paragraph(
                    f"<b>Maturity profile:</b>  {mat_str}",
                    styles["meta"]
                ))
                story.append(Spacer(1, 2))

            elif cq.get("total_debt_m") and cq["total_debt_m"] != "N/A":
                # Debt exists but per-tranche dollar amounts could not be parsed
                # from the filing (e.g. format only yields rate/name, no face
                # value) — state this explicitly rather than omitting the
                # section or showing a fabricated all-zero table.
                story.append(Spacer(1, 4))
                story.append(Paragraph(
                    "<b>Debt Maturity Profile</b>",
                    styles["meta"]
                ))
                story.append(Spacer(1, 2))
                story.append(Paragraph(
                    "<i>Maturity profile not available — tranche-level dollar "
                    "amounts could not be parsed from the filing.</i>",
                    styles["meta"]
                ))
                story.append(Spacer(1, 2))

            # ── Source footnote ──
            debt_src = cq.get("debt_source", "N/A")
            story.append(Paragraph(
                f"Rating source: ratings.csv (manually maintained).  "
                f"Spread: FRED ICE BofA OAS (live).  "
                f"ERP: Damodaran US implied.  "
                f"Debt schedule: {debt_src}.",
                styles["meta"]
            ))

        story.append(Spacer(1, 6))

        # ── Section 2B: Working Capital & Capital Intensity ──
        # Rows were built before the TOC (see _wc_rows above). Suppressed
        # entirely for banks, insurers and REITs — no operating working-
        # capital cycle — and for any ticker where nothing resolved.
        if _wc_rows:
            story += _section_header(
                "2B · Working Capital &amp; Capital Intensity", styles, anchor="s2b"
            )
            wc_header = ["Metric"] + [_short_period(p) for p in periods]
            story.append(_emit_table(wc_header, _wc_rows))
            story.append(Spacer(1, 2))
            story.append(Paragraph(
                "DSO = AR / Revenue × 365. DPO = AP / COGS × 365 (revenue basis "
                "where no cost-of-sales line is filed). DIO = Inventory / COGS × 365. "
                "CCC = DIO + DSO − DPO. NWC = Current Assets − Current Liabilities. "
                "CapEx shown on magnitude (sign convention varies by filer).",
                styles["meta"]
            ))
            if _wc_payload.get("mode") == "partial":
                story.append(Paragraph(
                    "Inventory metrics (DIO, Inv / Revenue, Inv / Assets) omitted — "
                    "this industry either carries no trade inventory or holds "
                    "commodity inventory whose turnover does not describe a "
                    "working-capital cycle. CCC is therefore DSO − DPO.",
                    styles["meta"]
                ))
            story.append(Spacer(1, 6))

        # ── Section 3: Valuation ──
        story += _section_header("3 · Valuation", styles, anchor="s3")

        # One row per metric: "Current (TTM)" (today's price over trailing-
        # twelve-month fundamentals) column first, then one FY-end-price
        # column per historical period. P/B and P/TBV are the exception --
        # book value is a balance-sheet (point-in-time) figure, so those two
        # rows stay on an MRQ basis even though the column header says TTM;
        # ValuationAgent never swaps their denominator for a TTM figure.
        curr_p = periods[0] if periods else "—"
        _val_ttm_on = bool(getattr(profile, "ttm", None))
        _curr_col_label = "Current (TTM)" if _val_ttm_on else "Current"
        val_header = ["Metric", _curr_col_label] + [_short_period(p) for p in periods]

        def _fmt_x_val(v):
            # fcf_ev/cfo_ev/cfo_per_share/ev_fcf/ev_cfo store raw
            # floats/None/N/A-strings (not pre-formatted, unlike the other
            # valuation dicts) -- render() formats them as "x.xx" here.
            if isinstance(v, str):
                return v  # "N/A (neg FCF)" / "N/A (neg CFO)" etc., as-is
            if isinstance(v, (int, float)):
                return f"{v:.2f}x"
            return "N/A"

        def _fmt_pct_val(v):
            if isinstance(v, str):
                return v
            if isinstance(v, (int, float)):
                return f"{v:.2f}%"
            return "N/A"

        def _fmt_dollar_val(v):
            if isinstance(v, str):
                return v
            if isinstance(v, (int, float)):
                # Sign outside the currency symbol: "-$1.23", not "$-1.23".
                return f"-${abs(v):.2f}" if v < 0 else f"${v:.2f}"
            return "N/A"

        def _fmt_shares_val(v):
            if isinstance(v, str):
                return v
            if isinstance(v, (int, float)) and v > 0:
                if v >= 1e9:
                    return f"{v/1e9:,.2f}B"
                return f"{v/1e6:,.1f}M"
            return "N/A"

        def _ev_yield_row(label, key, fmt_fn):
            d = valuation.get(key, {}) or {}
            by_period = d.get("by_period", {}) or {}
            return ([label, fmt_fn(d.get("current"))]
                   + [fmt_fn(by_period.get(p)) for p in periods])

        # Build valuation rows dynamically.
        # Rows are tagged (row, is_sub) so the YoY Δ / CAGR annotation rows
        # added under the non-multiple metrics keep their smaller indented
        # style after _drop_all_na renumbers the list.
        _val_tagged: list = []

        def _add_val_row(row, is_sub=False):
            _val_tagged.append((row, is_sub))

        def _add_metric_with_trend(label, key, fmt_fn, trend_key):
            """A yield/per-share metric row plus its YoY Δ and CAGR sub-rows."""
            _add_val_row(_ev_yield_row(label, key, fmt_fn))
            for sub in _trend_subrows(valuation.get(trend_key, {}), periods):
                _add_val_row(sub, is_sub=True)

        # EPS and BVPS are the denominators of the two multiples that follow
        # them; both are non-multiple metrics, so they get the same YoY/CAGR
        # annotation treatment as the other per-share rows.
        _add_metric_with_trend("Diluted EPS", "eps", _fmt_dollar_val, "eps_trend")
        _add_val_row(
            ["P/E", valuation["pe_current"].get(curr_p, "N/A")]
            + [valuation["pe_historical"].get(p, "N/A") for p in periods]
        )
        _add_metric_with_trend("BVPS", "bvps", _fmt_dollar_val, "bvps_trend")
        _add_val_row(
            ["P/B", valuation["pb_current"].get(curr_p, "N/A")]
            + [valuation["pb_historical"].get(p, "N/A") for p in periods]
        )
        if sg != "financials":
            _add_val_row(
                ["EV/EBITDA", valuation.get("ev_ebitda_current", {}).get(curr_p, "N/A")]
                + [valuation["ev_ebitda"].get(p, "N/A") for p in periods]
            )
        _add_val_row(
            ["EV/Sales", valuation.get("ev_sales_current", {}).get(curr_p, "N/A")]
            + [valuation["ev_sales"].get(p, "N/A") for p in periods]
        )
        # EV/FCF, FCF/EV are still shown for financials -- agents.py fills
        # them with an informative "N/A (financials -- use P/TBV instead)"
        # string rather than hiding the row, since CapEx/FCF are non-core
        # for banks/insurers. EV/CFO, CFO/EV ARE meaningful for financials
        # (operating cash flow is a real figure), so always shown.
        _add_val_row(_ev_yield_row("EV/FCF", "ev_fcf", _fmt_x_val))
        _add_val_row(_ev_yield_row("EV/CFO", "ev_cfo", _fmt_x_val))
        # FCF/EV, CFO/EV and CFO/Share are yields and per-share cash figures,
        # not point-in-time multiples — they carry YoY Δ and CAGR annotations.
        _add_metric_with_trend("FCF/EV", "fcf_ev", _fmt_pct_val, "fcf_ev_trend")
        _add_metric_with_trend("CFO/EV", "cfo_ev", _fmt_pct_val, "cfo_ev_trend")
        _add_metric_with_trend("CFO/Share", "cfo_per_share", _fmt_dollar_val,
                               "cfo_share_trend")
        if sg == "real_estate":
            # FFO/AFFO-based multiples replace the suppressed CFO/EV, EV/CFO
            # (see REIT footnote below) as the sector-appropriate cash yield.
            _add_val_row(_ev_yield_row("EV/FFO", "ev_ffo", _fmt_x_val))
            _add_metric_with_trend("FFO/EV", "ffo_ev", _fmt_pct_val, "ffo_ev_trend")
            _add_val_row(_ev_yield_row("EV/AFFO", "ev_affo", _fmt_x_val))
            _add_metric_with_trend("AFFO/EV", "affo_ev", _fmt_pct_val, "affo_ev_trend")
        _add_val_row(_ev_yield_row("Diluted Shares", "diluted_shares", _fmt_shares_val))
        _add_val_row(_ev_yield_row("Shares Outstanding", "shares_outstanding", _fmt_shares_val))

        # Drop all-N/A rows, then recompute the sub-row indices against the
        # surviving list. A suppressed metric row (e.g. FCF/EV for banks)
        # takes its annotation rows with it — their cells are all "—" too.
        _val_kept  = [(r, s) for (r, s) in _val_tagged
                      if not all(_is_na_cell(c) for c in r[1:])]
        val_rows   = [r for r, _ in _val_kept]
        _val_subs  = {i for i, (_, s) in enumerate(_val_kept) if s}
        story.append(_emit_table(val_header, val_rows, sub_rows=_val_subs))
        story.append(Spacer(1, 2))
        if _val_ttm_on:
            _ttm_note_bits = []
            _ttm_meta = fundamental.get("ttm", {}) or {}
            if _ttm_meta.get("is_partial"):
                _ttm_note_bits.append(
                    f"†TTM based on {_ttm_meta.get('quarters_used')} of 4 quarters "
                    f"(most recent: {_ttm_meta.get('most_recent_quarter', 'N/A')})."
                )
            story.append(Paragraph(
                "Current (TTM): P/E, EV/EBITDA, EV/Sales, EV/FCF, EV/CFO, FCF/EV, "
                "CFO/EV, and CFO/Share use trailing-twelve-month fundamentals against "
                "today's price/EV, not the most recent fiscal year (which may be 6-12 "
                "months stale). P/B and P/TBV use book value (a balance-sheet figure) "
                "and remain on a most-recent-quarter basis regardless. "
                + " ".join(_ttm_note_bits),
                styles["meta"]
            ))

        cp_str = f"${valuation.get('current_price', 0):.2f}" if valuation.get("current_price") else "N/A"
        story.append(Paragraph(
            f"Current price: {cp_str}   Market cap: {_fmt_market_cap(valuation.get('market_cap', 0))}",
            styles["meta"]
        ))
        story.append(Paragraph(
            "FCF = CFO − CapEx (Pathway A). EV/FCF and EV/CFO shown as multiples (×); "
            "FCF/EV and CFO/EV shown as yield (%). "
            "EV computed at FY-end price × shares + debt + operating lease "
            "liabilities + finance lease liabilities − cash.",
            styles["meta"]
        ))
        # ── Share-count anomaly footnotes ──
        # Three classes, three notes. A unit error is repaired in place, so
        # its note explains a restatement rather than a suppression; a
        # split and an unclassified break are both suppressions.
        _split = valuation.get("split_contamination", {}) or {}
        _corrections = _split.get("corrections") or {}
        if _corrections:
            for _p, _c in sorted(_corrections.items()):
                story.append(Paragraph(
                    f"FY{str(_p)[:4]} share count restated ×{_c['scale']:,} — "
                    f"as-filed value ({_c['raw']:,.0f}) appears tagged in "
                    f"{'thousands' if _c['scale'] == 1000 else 'millions'} rather "
                    f"than units; per-share and EV metrics for that year are "
                    f"computed on the restated count ({_c['corrected']:,.0f}).",
                    styles["meta"]
                ))
        _unknown_periods = [p for p in _split.get("periods", [])
                            if (_split.get("classes") or {}).get(p) == "unknown"]
        if _unknown_periods:
            _ratios = _split.get("ratios") or {}
            for _p in _unknown_periods:
                _r = _ratios.get(_p)
                _r_str = f"{_r:,.4g}×" if isinstance(_r, (int, float)) else "an unclassified"
                story.append(Paragraph(
                    f"FY{str(_p)[:4]} suppressed — share count discontinuity of "
                    f"{_r_str} could not be classified as either a split or a "
                    f"unit-tagging error; per-share and EV metrics for that year "
                    f"are withheld rather than published on an unverified basis.",
                    styles["meta"]
                ))
        _presplit_periods = [p for p in _split.get("periods", [])
                             if (_split.get("classes") or {}).get(p) != "unknown"]
        if _presplit_periods:
            _fy_list = ", ".join(f"FY{str(p)[:4]}" for p in _presplit_periods)
            _factor  = _split.get("factor")
            _dirn    = _split.get("direction") or "split"
            _before  = str(_split.get("before") or "")[:4]
            _after   = str(_split.get("after") or "")[:4]
            _when    = (f"between FY{_before} and FY{_after}"
                        if _before and _after else "within this window")
            # Unit-tagging errors are classified out and repaired before
            # reaching here (see the restatement note above), so a step that
            # survives to this point is a split the as-filed count predates
            # and can be named as one.
            _factor_str = (
                f"A {_factor:g}-for-1 {_dirn} occurred {_when}."
                if _factor else
                f"A {_dirn} occurred {_when}."
            )
            story.append(Paragraph(
                f"{_fy_list} per-share and EV metrics suppressed — share count is "
                f"as-filed (never retroactively adjusted) while prices are "
                f"split-adjusted. {_factor_str} "
                f"Margins, returns, turnover and other non-per-share metrics for "
                f"{_fy_list} are unaffected and are shown normally.",
                styles["meta"]
            ))
        if _val_subs:
            story.append(Paragraph(
                "<i>YoY Δ</i> and <i>CAGR</i> rows annotate the non-multiple metrics only "
                "(FCF/EV, CFO/EV, CFO/Share, and FFO/EV, AFFO/EV for REITs) — the "
                "multiples above are point-in-time valuations, not growth series. "
                "YoY Δ = change vs. the prior fiscal year, suppressed where the prior "
                "value is zero or of the opposite sign (a negative base makes a "
                "percentage change meaningless). CAGR is the compound annual rate from "
                "the oldest to the most recent fiscal year shown, computed only from a "
                "positive base and only where at least three years resolve.",
                styles["meta"]
            ))
        if sg == "real_estate":
            story.append(Paragraph(
                "CFO/EV, EV/CFO, and CFO/Share suppressed for REITs. GAAP CFO includes "
                "real estate depreciation — use FFO (Funds From Operations) for peer comparison. "
                "FFO = Net Income + Real Estate D&A − Gains on Sale of RE + Impairment "
                "(NAREIT definition). AFFO = FFO − Straight-line Rent − Lease Amortization "
                "− Maintenance CapEx + Non-cash Stock Comp + Non-cash Interest.",
                styles["meta"]
            ))
        story.append(Paragraph(
            "Diluted Shares: yfinance annual \"Diluted Average Shares\" (primary source; "
            "pre-cleaned, ~4 fiscal years), SEC XBRL diluted share count as fallback for any "
            "period yfinance doesn't cover. Shares Outstanding: yfinance live snapshot "
            "(sharesOutstanding) — Current column only, no historical series available. "
            "All per-share figures above (EPS, BVPS, P/E, P/B, EV, CFO/Share, etc.) use "
            "this share count.",
            styles["meta"]
        ))

        # ── WACC block ──
        wacc_data = valuation.get("wacc", {})
        if wacc_data and wacc_data.get("available"):
            story.append(Spacer(1, 4))
            def _pct_str(v, na="N/A"):
                try:
                    return f"{float(v)*100:.2f}%" if v not in (None, "N/A") else na
                except Exception:
                    return na
            def _x_str(v, na="N/A"):
                try:
                    return f"{float(v):.2f}x" if v not in (None, "N/A") else na
                except Exception:
                    return na

            wacc_rows = [
                ["WACC Component", "Value"],
                ["Beta (yfinance)",
                 _x_str(wacc_data.get("beta"))],
                ["Equity Risk Premium (ERP)",
                 f"{_pct_str(wacc_data.get('erp'))}  (Damodaran, {wacc_data.get('erp_date','')})"],
                ["Cost of Equity  [RF + β×ERP]",
                 _pct_str(wacc_data.get("cost_of_equity"))],
                ["Cost of Debt  [RF + OAS]",
                 _pct_str(wacc_data.get("cost_of_debt"))],
                ["Effective Tax Rate",
                 _pct_str(wacc_data.get("tax_rate"))],
                ["After-tax Cost of Debt",
                 _pct_str(wacc_data.get("kd_after_tax"))],
                ["Equity Weight",
                 _pct_str(wacc_data.get("weight_equity"))],
                ["Debt Weight",
                 _pct_str(wacc_data.get("weight_debt"))],
            ]
            story.append(_emit_table(wacc_rows[0], wacc_rows[1:]))
            story.append(Spacer(1, 2))
            wacc_val = wacc_data.get("wacc")
            wacc_str = _pct_str(wacc_val)
            story.append(Paragraph(
                f"<b>WACC: {wacc_str}</b>  "
                f"[We×Ke + Wd×Kd×(1-t)  |  CAPM cost of equity  |  Rating-implied cost of debt]",
                styles["body"]
            ))

        # ── Peer Comparison ──
        # All values here, including the subject's own row, are sourced from
        # yfinance .info for internal consistency within this table — not the
        # report's primary XBRL-derived figures used elsewhere in Section 1-3.
        # See peer_comparator.py docstring for why that's a deliberate choice.
        if peer_comparison and peer_comparison.get("metrics"):
            story.append(Spacer(1, 6))
            t_peer_hdr = Table(
                [[Paragraph("Peer Comparison  (yfinance)", styles["section"])]],
                colWidths=["100%"]
            )
            t_peer_hdr.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(t_peer_hdr)
            story.append(Spacer(1, 2))

            def _peer_x_str(v):
                try:
                    return f"{float(v):.2f}x"
                except Exception:
                    return "N/A"
            def _peer_pct_str(v):
                try:
                    return f"{float(v)*100:.2f}%"
                except Exception:
                    return "N/A"

            industry = peer_comparison.get("industry", "")
            peer_tickers = peer_comparison.get("peer_tickers", [])
            _dropped   = peer_comparison.get("dropped", []) or []
            _n_valid   = peer_comparison.get("n_valid", len(peer_tickers))
            _n_derived = peer_comparison.get("n_derived", len(peer_tickers))
            _basis     = peer_comparison.get("peer_basis", "peer_set")
            _basis_lbl = ("industry median (constituent set widened — too few "
                          "peers survived validation)"
                          if _basis == "industry_median" else "peer set")
            story.append(Paragraph(
                f"Industry: {industry}  |  {_n_valid} valid peers of "
                f"{_n_derived} derived ({len(_dropped)} dropped)  |  "
                f"basis: {_basis_lbl}<br/>"
                f"Peers: {', '.join(peer_tickers)}  "
                f"(source: {peer_comparison.get('peer_source', '')})",
                styles["body"]
            ))
            story.append(Spacer(1, 2))
            if _dropped:
                _drop_str = "; ".join(f"{p} — {r}" for p, r in _dropped)
                story.append(Paragraph(
                    f"<b>Dropped from the peer set:</b> {_drop_str}. "
                    f"A name is dropped when it does not resolve to a live "
                    f"listing, when it is not registered with the SEC, or when "
                    f"it files no US-GAAP XBRL data. The last case removes "
                    f"genuine competitors that list outside the US (for example "
                    f"Asian semiconductor or electronics names, which may hold "
                    f"an SEC registration but report under IFRS). The table "
                    f"itself is built from yfinance for every name including the "
                    f"subject, so such a peer does have quoted figures — but "
                    f"they cannot be reconciled against this report's own "
                    f"XBRL-derived numbers, and secondary or foreign listings "
                    f"frequently carry currency- and convention-mismatched "
                    f"fields that distort a small-sample median. They are "
                    f"excluded to keep the median comparable, not judged "
                    f"irrelevant as competitors.",
                    styles["meta"]
                ))
                story.append(Spacer(1, 2))

            _METRIC_LABELS = {
                "pe_trailing": "P/E (trailing)",
                "ev_ebitda":   "EV/EBITDA",
                "pb":          "P/B",
                "roe":         "ROE",
                "rev_growth":  "Revenue Growth",
            }
            _PCT_METRICS = {"roe", "rev_growth"}

            peer_header = ["Metric", "Subject", "Peer Median", "Percentile"]
            peer_rows = []
            for key, label in _METRIC_LABELS.items():
                m = peer_comparison["metrics"].get(key)
                if not m:
                    continue
                if key in _PCT_METRICS:
                    subj_str   = _peer_pct_str(m["subject"])
                    median_str = _peer_pct_str(m["peer_median"])
                else:
                    subj_str   = _peer_x_str(m["subject"])
                    median_str = _peer_x_str(m["peer_median"])
                peer_rows.append([
                    label, subj_str, median_str, f"{m['percentile']:.0f}th"
                ])
            if peer_rows:
                story.append(_ratio_table(peer_header, peer_rows))
                story.append(Spacer(1, 2))
                story.append(Paragraph(
                    "Percentile = how favorably the subject compares to peers "
                    "on this metric (100th = most favorable in group: cheapest "
                    "multiple for P/E, EV/EBITDA, P/B, or highest ROE/growth). "
                    "All figures from yfinance — may differ from this report's "
                    "primary valuation figures above, which use this pipeline's "
                    "own XBRL-sourced data.",
                    styles["meta"]
                ))

        story.append(Spacer(1, 6))

        # ── Check if page 2 is needed ──
        all_flags  = commentary.get("flags", [])
        narrative  = commentary.get("narrative", [])
        guidance   = commentary.get("management_guidance", [])

        # ── Section 4: Red Flags ── (always rendered — fixed positioning)
        story.append(PageBreak())
        story += _section_header("4 · Red Flags", styles, anchor="s4")
        if all_flags:
            story += _flag_box(all_flags, styles)
        else:
            story.append(Paragraph("No material red flags identified.", styles["body"]))
        story.append(Spacer(1, 6))

        needs_p2   = bool(narrative or guidance)

        if needs_p2:

            # ── Section 4: Trend Commentary ──
            story += _section_header("5 · Trend Commentary", styles, anchor="s5")
            for line in narrative:
                story.append(Paragraph(f"• {line}", styles["bullet"]))
            story.append(Spacer(1, 6))

            # ── Section 6: Management Guidance & Tone ──
            ga = guidance_analysis or {}
            if ga.get("available") or guidance:
                story += _section_header("6 · Management Guidance & Tone", styles, anchor="s6")

                # Source + date attribution
                source_str = ga.get("source", "Annual report (10-K MD\u0026A)")
                date_str   = ga.get("filing_date", "")
                attr = f"Source: {source_str}"
                if date_str and date_str != "N/A":
                    attr += f"  |  Filed: {date_str}"
                story.append(Paragraph(attr, styles["meta"]))
                story.append(Spacer(1, 4))

                # Tone indicator
                tone = ga.get("tone", {})
                tone_label = tone.get("label", "N/A")
                tone_score = tone.get("score", 0.0)
                tone_sigs  = tone.get("signals", [])
                if tone_label != "N/A":
                    tone_color = (
                        "#27AE60" if tone_label == "Confident" else
                        "#C0392B" if tone_label == "Cautious"  else
                        "#7F8C8D"
                    )
                    tone_bg = colors.HexColor(
                        "#E8F8F0" if tone_label == "Confident" else
                        "#FDEDEC" if tone_label == "Cautious"  else
                        "#F2F3F4"
                    )
                    # Filter out adjustment signals from display — shown separately
                    display_sigs = [s for s in tone_sigs[:4] if not s.startswith("adj:")]
                    sig_text = f"  ({', '.join(display_sigs)})" if display_sigs else ""

                    # Credibility adjustment indicator
                    cred_note = tone.get("credibility_note", "")
                    adj_text  = (
                        f"  ⚠ {cred_note}" if cred_note else ""
                    )

                    tone_style = ParagraphStyle(
                        "tone_inline",
                        parent=styles["meta"],
                        fontName="Helvetica-Bold",
                        fontSize=8,
                        textColor=colors.HexColor(tone_color),
                    )
                    tone_note_style = ParagraphStyle(
                        "tone_note",
                        parent=styles["meta"],
                        fontSize=7,
                        textColor=colors.HexColor("#7F8C8D"),
                        fontName="Helvetica-Oblique",
                    )
                    tone_cell_content = [
                        Paragraph(
                            f"Management Tone: {tone_label}{sig_text}",
                            tone_style
                        )
                    ]
                    if adj_text:
                        tone_cell_content.append(
                            Paragraph(adj_text, tone_note_style)
                        )
                    tone_bar = Table(
                        [tone_cell_content] if not adj_text else
                        [[Paragraph(
                            f"Management Tone: {tone_label}{sig_text}",
                            tone_style
                        )],
                         [Paragraph(adj_text, tone_note_style)]],
                        colWidths=[170 * mm]
                    )
                    tone_bar.setStyle(TableStyle([
                        ("BACKGROUND",    (0, 0), (-1, -1), tone_bg),
                        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
                        ("TOPPADDING",    (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ("LINEBELOW",     (0, 0), (-1, -1), 0.8,
                         colors.HexColor(tone_color)),
                    ]))
                    story.append(tone_bar)
                    story.append(Spacer(1, 5))

                # Category labels for display
                _CAT_LABELS = {
                    "financial_targets":  "Financial Targets",
                    "capital_allocation": "Capital Allocation",
                    "growth_outlook":     "Growth Outlook",
                    "risk_factors":       "Risk Factors",
                    "macro_view":         "Macro View",
                }

                def _render_cat_block(cats: dict) -> bool:
                    """Render categorised guidance bullets. Returns True if anything rendered."""
                    rendered = False
                    for cat_key, cat_label in _CAT_LABELS.items():
                        sents = cats.get(cat_key, [])
                        if not sents:
                            continue
                        story.append(Paragraph(f"<b>{cat_label}</b>", styles["body"]))
                        for s in sents:
                            story.append(Paragraph(f"• {s}", styles["bullet"]))
                        story.append(Spacer(1, 3))
                        rendered = True
                    return rendered

                def _render_subsection(source_key: str, cats_key: str, label: str):
                    """Render one guidance subsection if it has content."""
                    src  = ga.get(source_key)
                    cats = ga.get(cats_key, {})
                    if not src or not any(v for v in cats.values()):
                        return False
                    story.append(Paragraph(
                        f"<b>{label}</b>  "
                        f"<font size=7 color='#7F8C8D'>({src})</font>",
                        styles["body"]
                    ))
                    story.append(Spacer(1, 3))
                    _render_cat_block(cats)
                    story.append(Spacer(1, 2))
                    return True

                # ── Four subsections — shown independently, all sources ────────
                any_rendered = False

                # Earnings Call — render header whenever transcript fetched,
                # even if forward cats are empty (backward + summary may still have content)
                fool_src = ga.get("fool_source")
                fool_cats_data = ga.get("fool_cats", {})
                fool_backward_cats = ga.get("fool_backward_cats", {})
                fool_summary_bullets = ga.get("fool_summary_bullets", [])
                fool_summary = ga.get("fool_summary")
                fool_has_content = (
                    any(v for v in fool_cats_data.values()) or
                    any(v for v in fool_backward_cats.values()) or
                    bool(fool_summary_bullets) or
                    bool(fool_summary)
                )
                if fool_src and fool_has_content:
                    story.append(Paragraph(
                        f"<b>Earnings Call</b>  "
                        f"<font size=7 color='#7F8C8D'>({fool_src})</font>",
                        styles["body"]
                    ))
                    story.append(Spacer(1, 3))
                    if fool_summary:
                        story.append(Paragraph(f'<i>{fool_summary}</i>', styles["guidance"]))
                        story.append(Spacer(1, 6))
                    if any(v for v in fool_cats_data.values()):
                        _render_cat_block(fool_cats_data)
                        story.append(Spacer(1, 2))
                    if any(v for v in fool_backward_cats.values()):
                        story.append(Paragraph("<b>Results &amp; Context</b>", styles["body"]))
                        story.append(Spacer(1, 3))
                        _render_cat_block(fool_backward_cats)
                        story.append(Spacer(1, 2))
                    if fool_summary_bullets:
                        story.append(Paragraph("<b>Management Commentary</b>", styles["body"]))
                        story.append(Spacer(1, 3))
                        for sent in fool_summary_bullets:
                            story.append(Paragraph(f"– {sent}", styles["bullet"]))
                        story.append(Spacer(1, 4))
                    any_rendered = True

                any_rendered |= _render_subsection("ex99_2_source", "ex99_2_cats", "Prepared Remarks (99.2)")
                any_rendered |= _render_subsection("mda_source",    "mda_cats",    "SEC Filings MD&amp;A")
                any_rendered |= _render_subsection("ex99_1_source", "ex99_1_cats", "Press Release (99.1)")

                # ── Fallback: old schema or all empty ─────────────────────────
                if not any_rendered:
                    categories = ga.get("categories", {})
                    if any(v for v in categories.values()):
                        _render_cat_block(categories)
                    elif ga.get("available"):
                        story.append(Paragraph(
                            "No forward-looking guidance statements identified.",
                            styles["meta"]
                        ))
                    elif guidance:
                        import re as _re
                        clean_guidance = [
                            g for g in guidance
                            if len(g) > 60
                            and not _re.search(r'\d{2,}\s+\d{2,}\s+\d{2,}', g)
                            and g.count('$') < 5
                            and not g[:30].replace(' ', '').isdigit()
                        ][:5]
                        if clean_guidance:
                            story.append(Paragraph(
                                "Selected forward-looking statements (10-K MD&A):",
                                styles["body"]
                            ))
                            story.append(Spacer(1, 3))
                            for g in clean_guidance:
                                display = g if len(g) <= 300 else g[:297] + "..."
                                story.append(Paragraph(f'"{display}"', styles["guidance"]))

                story.append(Spacer(1, 6))

            # ── Section 7: Management Track Record ──────────────────────────
            # Three display modes, driven by structured_guidance_analysis
            # (LLM-based extraction — core.agents._extract_structured_guidance
            # / _compare_guidance_to_actuals). track_analysis (GuidanceTracker,
            # regex-based) still drives Communication Score / Execution
            # Quality below and its own flags, which carry signal the new
            # engine doesn't compute.
            ta  = track_analysis or {}
            sga = structured_guidance_analysis or {}
            forward_guidance = sga.get("forward_guidance", []) or []
            comparisons      = sga.get("comparisons", []) or []
            s7_credibility   = sga.get("credibility")
            _transcript_available = bool((guidance_analysis or {}).get("available"))

            def _fmt_native(val, unit):
                if val is None:
                    return "—"
                u = (unit or "").upper()
                if "B" in u:
                    return f"${val:,.2f}B"
                if "M" in u:
                    return f"${val:,.0f}M"
                if "%" in u:
                    return f"{val:.1f}%"
                if u == "$":
                    return f"${val:,.2f}"
                return f"{val:,.2f}"

            def _fmt_forward_guidance_value(item):
                lo, hi = item.get("low"), item.get("high")
                mid, unit = item.get("midpoint"), item.get("unit")
                if lo is not None and hi is not None and lo != hi:
                    s = f"{_fmt_native(lo, unit)}–{_fmt_native(hi, unit)}"
                    if mid is not None:
                        s += f" (midpoint {_fmt_native(mid, unit)})"
                    return s
                if mid is not None:
                    return f"~{_fmt_native(mid, unit)}"
                if lo is not None:
                    return _fmt_native(lo, unit)
                return (item.get("raw_text") or "—")[:80]

            def _small_table(hdrs, data_rows, col_w):
                rows = [hdrs] + data_rows
                t = Table(rows, colWidths=col_w, repeatRows=1)
                t.setStyle(TableStyle([
                    ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#2C3E50")),
                    ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.HexColor("#FFFFFF")),
                    ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
                    ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
                    ("ROWBACKGROUNDS",(0, 1), (-1, -1),
                     [colors.HexColor("#F8F9FA"), colors.HexColor("#FFFFFF")]),
                    ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#BDC3C7")),
                    ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
                    ("TOPPADDING",    (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                return t

            if comparisons:
                # ── MODE A — Full: 1+ guidance comparisons available ────────
                story += _section_header("7 · Management Track Record", styles, anchor="s7")

                n_beats  = sum(1 for c in comparisons if c["outcome"] == "beat")
                n_meets  = sum(1 for c in comparisons if c["outcome"] == "meet")
                n_misses = sum(1 for c in comparisons if c["outcome"] == "miss")
                cred_val = s7_credibility if s7_credibility is not None else 50
                cred_color = (
                    "#27AE60" if cred_val >= 70
                    else "#E67E22" if cred_val >= 45
                    else "#E74C3C"
                )
                story.append(Paragraph(
                    f"<b>Credibility Score: <font color='{cred_color}'>{cred_val}/100</font></b>"
                    f"  &nbsp;&nbsp;{n_beats} beats, {n_misses} misses, {n_meets} meets "
                    f"across {len(comparisons)} comparisons",
                    styles["body"]
                ))
                story.append(Spacer(1, 6))

                story.append(Paragraph("<b>Prior Guidance vs Actuals</b>", styles["body"]))
                story.append(Spacer(1, 2))
                _outcome_style = {
                    "beat": ("#27AE60", "✓ Beat"),
                    "meet": ("#2980B9", "= Meet"),
                    "miss": ("#E74C3C", "✗ Miss"),
                }
                comp_rows = []
                for c in comparisons:
                    unit = c.get("unit")
                    guided_str = f"{_fmt_native(c['guided_low'], unit)}–{_fmt_native(c['guided_high'], unit)}"
                    actual_str = _fmt_native(c["actual"], unit)
                    o_color, o_label = _outcome_style.get(c["outcome"], ("#7F8C8D", c["outcome"]))
                    comp_rows.append([
                        c["metric"], c.get("period") or "—", guided_str, actual_str,
                        Paragraph(f"<font color='{o_color}'><b>{o_label}</b></font>", styles["body"]),
                    ])
                story.append(_small_table(
                    ["Metric", "Period", "Guided Range", "Actual", "Outcome"],
                    comp_rows, [30*mm, 22*mm, 42*mm, 28*mm, 24*mm]
                ))
                story.append(Spacer(1, 8))

                if forward_guidance:
                    story.append(Paragraph(
                        "<b>Forward Guidance</b>  "
                        "<font size=7 color='#7F8C8D'>(from most recent transcript)</font>",
                        styles["body"]
                    ))
                    story.append(Spacer(1, 2))
                    fwd_rows = [
                        [g["metric"], g.get("period") or "—", _fmt_forward_guidance_value(g)]
                        for g in forward_guidance
                    ]
                    story.append(_small_table(
                        ["Metric", "Period", "Guidance"], fwd_rows,
                        [30*mm, 22*mm, 94*mm]
                    ))
                    story.append(Spacer(1, 8))

                cq = communication_quality or {}
                cq_score = cq.get("score")
                if cq_score is not None:
                    cq_color = (
                        "#27AE60" if cq_score >= 70
                        else "#E67E22" if cq_score >= 45
                        else "#E74C3C"
                    )
                    story.append(Paragraph(
                        f"<b>Communication Score:</b> "
                        f"<font color='{cq_color}'><b>{cq_score}/100</b></font>",
                        styles["body"]
                    ))

                eq = execution_quality or {}
                eq_score = eq.get("score")
                if eq_score is not None:
                    eq_color = (
                        "#27AE60" if eq_score >= 70
                        else "#E67E22" if eq_score >= 45
                        else "#E74C3C"
                    )
                    story.append(Paragraph(
                        f"<b>Execution Quality:</b> "
                        f"<font color='{eq_color}'><b>{eq_score}/100</b></font>",
                        styles["body"]
                    ))
                story.append(Spacer(1, 4))

                for flag in ta.get("flags", []):
                    story.append(Paragraph(flag, styles["flag"]))
                    story.append(Spacer(1, 2))

            elif forward_guidance:
                # ── MODE B — Partial: guidance extracted, no prior period ───
                story += _section_header("7 · Management Track Record", styles, anchor="s7")

                _n_quarters = len(ta.get("quarters", []) or [])
                story.append(Paragraph(
                    f"<b>Credibility:</b> Unrated — no prior period to compare "
                    f"against ({_n_quarters} quarter(s) reviewed, guidance "
                    f"figures not consistently comparable across periods).",
                    styles["body"]
                ))
                story.append(Spacer(1, 6))

                story.append(Paragraph(
                    "<b>Forward Guidance</b>  "
                    "<font size=7 color='#7F8C8D'>(from most recent transcript)</font>",
                    styles["body"]
                ))
                story.append(Spacer(1, 2))
                fwd_rows = [
                    [g["metric"], g.get("period") or "—", _fmt_forward_guidance_value(g)]
                    for g in forward_guidance
                ]
                story.append(_small_table(
                    ["Metric", "Period", "Guidance"], fwd_rows,
                    [30*mm, 22*mm, 94*mm]
                ))
                story.append(Spacer(1, 6))

                cq = communication_quality or {}
                cq_score = cq.get("score")
                if cq_score is not None:
                    cq_color = (
                        "#27AE60" if cq_score >= 70
                        else "#E67E22" if cq_score >= 45
                        else "#E74C3C"
                    )
                    story.append(Paragraph(
                        f"<b>Communication Score:</b> "
                        f"<font color='{cq_color}'><b>{cq_score}/100</b></font>"
                        f"  &nbsp;&nbsp;<font color='#7F8C8D'>(tone vs. delivery)</font>",
                        styles["body"]
                    ))
                story.append(Spacer(1, 4))

            else:
                # ── MODE C — Minimal: no transcript, or no guidance found ───
                story += _section_header("7 · Management Track Record", styles, anchor="s7")
                if _transcript_available:
                    msg = (
                        f"<b>Management Track Record:</b> a transcript was found for "
                        f"{ticker} but no structured guidance figures were extracted."
                    )
                else:
                    msg = (
                        f"<b>Management Track Record:</b> No earnings transcript "
                        f"available for {ticker}. Section omitted."
                    )
                story.append(Paragraph(msg, styles["body"]))
                story.append(Spacer(1, 6))

        # ── Section 8: Insider & Institutional Information ─────────────────────
        _own = ownership or fundamental.get("ownership") or {}
        _ins = insider_activity or {}
        if _own or _ins:
            story += _section_header("8 · Insider & Institutional Information", styles, anchor="s8")

        # ── 8a. Institutional Ownership (subsection) ──
        if _own and (
            _own.get("institutional_pct") is not None
            or _own.get("top_holders")
        ):
            t_own_hdr = Table(
                [[Paragraph("Institutional Ownership  (13-F / yfinance)", styles["section"])]],
                colWidths=["100%"]
            )
            t_own_hdr.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(t_own_hdr)
            story.append(Spacer(1, 2))

            inst_pct = _own.get("institutional_pct")
            ins_pct  = _own.get("insider_pct")
            top10    = _own.get("top10_concentration_pct")
            source   = _own.get("_source", "yfinance")
            as_of    = _own.get("_as_of", "current")

            # Retail / Public float = 100% - Institutional% - Insider%.
            # Only computable when both components are known; capped at 0%
            # (with a flag) if Institutional + Insider somehow exceed 100%.
            retail_pct       = None
            retail_capped    = False
            if inst_pct is not None and ins_pct is not None:
                retail_pct = 1.0 - inst_pct - ins_pct
                if retail_pct < 0:
                    retail_capped = True
                    retail_pct    = 0.0

            own_summary_header = []
            own_summary_row    = []
            if inst_pct is not None:
                own_summary_header.append("Institutional")
                own_summary_row.append(f"{inst_pct*100:.1f}%")
            if retail_pct is not None:
                own_summary_header.append("Retail / Public")
                retail_str = f"{retail_pct*100:.1f}%"
                if retail_capped:
                    retail_str += " ⚠"
                own_summary_row.append(retail_str)
            if ins_pct is not None:
                own_summary_header.append("Insider")
                own_summary_row.append(f"{ins_pct*100:.1f}%")
            if top10 is not None:
                own_summary_header.append("Top-10 Concentration")
                own_summary_row.append(f"{top10*100:.1f}%")
            if own_summary_header:
                story.append(_ratio_table(own_summary_header, [own_summary_row]))
                story.append(Spacer(1, 3))

            top_holders    = _own.get("top_holders", [])
            delta_note     = _own.get("_delta_note")
            recent_label   = _own.get("_recent_label")          # "Qtr", "6mo", or "1yr"
            has_delta_rec  = any(h.get("delta_recent_shares") is not None for h in top_holders)
            # suppress separate 1yr column when recent already used 1yr snapshot
            has_delta_1yr  = (recent_label != "1yr") and any(h.get("delta_1yr_shares") is not None for h in top_holders)
            has_delta_3yr  = any(h.get("delta_3yr_shares") is not None for h in top_holders)

            if top_holders:
                own_header = ["Top-10 Holders", "% Out", "Shares Held"]
                if has_delta_rec:
                    own_header.append(f"Δ {recent_label} (shares)")
                if has_delta_1yr:
                    own_header.append("Δ 1yr (shares)")
                if has_delta_3yr:
                    own_header.append("Δ 3yr (shares)")

                own_rows = []
                for h in top_holders:
                    pct_str = f"{h['pct']*100:.2f}%" if h.get("pct") is not None else "N/A"
                    sh_str  = f"{h['shares']:,}"     if h.get("shares") is not None else "N/A"
                    row = [h.get("name", "—"), pct_str, sh_str]
                    if has_delta_rec:
                        dr = h.get("delta_recent_shares")
                        row.append("—" if dr is None else f"{'+'if dr>=0 else''}{dr:,}")
                    if has_delta_1yr:
                        d1 = h.get("delta_1yr_shares")
                        row.append("—" if d1 is None else f"{'+'if d1>=0 else''}{d1:,}")
                    if has_delta_3yr:
                        d3 = h.get("delta_3yr_shares")
                        row.append("—" if d3 is None else f"{'+'if d3>=0 else''}{d3:,}")
                    own_rows.append(row)

                story.append(_ratio_table(own_header, own_rows))
                story.append(Spacer(1, 2))

            inst_dr = _own.get("institutional_pct_delta_recent")
            inst_d1 = _own.get("institutional_pct_delta_1yr")
            inst_d3 = _own.get("institutional_pct_delta_3yr")
            delta_parts = []
            if inst_dr is not None:
                delta_parts.append(f"Inst. Δ {recent_label}: {'+'if inst_dr>=0 else ''}{inst_dr*100:.1f}pp")
            if inst_d1 is not None and recent_label != "1yr":
                delta_parts.append(f"Inst. Δ 1yr: {'+'if inst_d1>=0 else''}{inst_d1*100:.1f}pp")
            if inst_d3 is not None:
                delta_parts.append(f"Inst. Δ 3yr: {'+'if inst_d3>=0 else''}{inst_d3*100:.1f}pp")
            if delta_parts:
                story.append(Paragraph("  |  ".join(delta_parts), styles["meta"]))
                story.append(Spacer(1, 2))

            footnote_own = (
                f"Source: {source}.  As of: {as_of}.  "
                f"13-F filings are disclosed quarterly with up to 45-day lag."
            )
            if delta_note:
                footnote_own += f"  {delta_note}."
            if retail_capped:
                footnote_own += (
                    "  ⚠ Institutional + Insider ownership exceeds 100% — "
                    "Retail/Public float capped at 0%."
                )
            story.append(Paragraph(footnote_own, styles["meta"]))
            story.append(Spacer(1, 8))

        # ── 8b. Insider Activity (subsection) ──
        if _own or _ins:
            t_ins_hdr = Table(
                [[Paragraph("Insider Activity  (SEC Form 4)", styles["section"])]],
                colWidths=["100%"]
            )
            t_ins_hdr.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(t_ins_hdr)
            story.append(Spacer(1, 2))

        if _ins and _ins.get("transactions"):
            lookback   = _ins.get("lookback_days", 90)
            buy_count  = _ins.get("buy_count", 0)
            sell_count = _ins.get("sell_count", 0)
            buy_value  = _ins.get("buy_value", 0)
            sell_value = _ins.get("sell_value", 0)
            net_value  = _ins.get("net_value", 0)
            cluster    = _ins.get("cluster_buying", False)
            all_txns   = _ins.get("transactions", [])

            # Share-weighted average execution price, separately for buys
            # and sells (mixing the two wouldn't be meaningful — they're
            # different decisions at potentially different times/prices).
            # Weighted by shares rather than a simple average across
            # transactions, since one 500,000-share sale should count for
            # more than five 100-share sales when judging "what price did
            # the selling cluster around."
            buy_shares  = sum(t["shares"] for t in all_txns if t["transaction"] == "BUY")
            sell_shares = sum(t["shares"] for t in all_txns if t["transaction"] == "SELL")
            avg_buy_price  = (buy_value  / buy_shares)  if buy_shares  else None
            avg_sell_price = (sell_value / sell_shares) if sell_shares else None

            summary_header = ["Window", "Buyers", "Sellers", "Buy Value",
                              "Sell Value", "Net"]
            summary_row    = [
                f"{lookback}d",
                str(buy_count),
                str(sell_count),
                f"${buy_value:,.0f}",
                f"${sell_value:,.0f}",
                f"{'+$' if net_value >= 0 else '-$'}{abs(net_value):,.0f}",
            ]
            if avg_buy_price is not None:
                summary_header.append("Avg Buy")
                summary_row.append(f"${avg_buy_price:,.2f}")
            if avg_sell_price is not None:
                summary_header.append("Avg Sell")
                summary_row.append(f"${avg_sell_price:,.2f}")
            # Explicit widths: _ratio_table's default reserves 55mm for a
            # label column, but col 0 here is a short window ("90d"), which
            # starved the currency columns and split values mid-number
            # ("-$410,446,40 1"). Give col 0 what it needs and share the
            # rest evenly so the widest amount fits on one line.
            _sum_page_w = A4[0] - 40 * mm
            _sum_rest   = (_sum_page_w - 18 * mm) / max(len(summary_header) - 1, 1)
            story.append(_ratio_table(
                summary_header, [summary_row],
                col_widths=[18 * mm] + [_sum_rest] * (len(summary_header) - 1),
            ))
            story.append(Spacer(1, 3))

            if cluster:
                story.append(Paragraph(
                    f"<font color='#27AE60'><b>■ Cluster buying signal — "
                    f"{buy_count} distinct insiders bought in the past "
                    f"{lookback} days</b></font>",
                    styles["body"]
                ))
                story.append(Spacer(1, 3))

            if _ins.get("large_position_change"):
                story.append(Paragraph(
                    f"<font color='#E74C3C'><b>■ Large position change — at least "
                    f"one transaction represents 50%+ of that insider's prior "
                    f"holding (see % of Position column below)</b></font>",
                    styles["body"]
                ))
                story.append(Spacer(1, 3))

            txn_header = ["Date", "Insider", "Position", "Txn", "Shares",
                         "Remaining", "% of Pos.", "Price", "Value"]
            txn_rows = []
            for t in _ins["transactions"][:15]:
                plan_note = " [10b5-1]" if t.get("is_10b5_1") else ""
                txn_color = "#27AE60" if t["transaction"] == "BUY" else "#E74C3C"
                txn_cell = Paragraph(
                    f"<font color='{txn_color}'><b>{t['transaction']}</b></font>{plan_note}",
                    styles["body"]
                )
                remaining = t.get("remaining")
                remaining_str = f"{remaining:,.0f}" if remaining is not None else "—"
                pct_pos = t.get("pct_of_position")
                if pct_pos is None:
                    pct_str = "—"
                else:
                    pct_color = "#E74C3C" if pct_pos >= 0.50 else "#1A1A1A"
                    pct_str_raw = f"{pct_pos*100:.1f}%"
                    pct_str = Paragraph(
                        f"<font color='{pct_color}'>{pct_str_raw}</font>",
                        styles["body"]
                    )
                txn_rows.append([
                    t["date"],
                    t.get("insider_name", "—"),
                    t.get("position", "—"),
                    txn_cell,
                    f"{t['shares']:,}",
                    remaining_str,
                    pct_str,
                    f"${t['price']:,.2f}",
                    f"${t['value']:,.0f}",
                ])

            # Built directly rather than via _ratio_table — that helper
            # str()s every cell before wrapping, which would re-render the
            # colored Txn Paragraph as its raw repr() instead of formatted
            # text. Cells that are already flowables (Txn, and % of Pos.
            # when colored) are passed through untouched; plain-string cells
            # go through _cell() as usual. Same pattern as the maturity wall
            # table above.
            txn_page_w     = A4[0] - 40 * mm
            # Date widened from 16mm: a 10-character ISO date needs ~15mm of
            # text plus 8mm of padding, so it was breaking mid-value
            # ("2026-06-1 8"). The width taken back comes from Position and
            # the numeric columns, which are the fields that SHOULD wrap.
            # Sums to the full 170mm content width.
            txn_col_widths = [22*mm, 23*mm, 24*mm, 15*mm, 15*mm, 17*mm, 13*mm, 17*mm, 24*mm]
            txn_wrapped_header = [Paragraph(str(c), _CELL_STYLE_HEADER)
                                  for c in txn_header]
            txn_wrapped_data = [
                [
                    _cell_nowrap(row[0]),   # date — atomic, never split
                    _cell(row[1]),
                    _cell(row[2]),
                    row[3],   # pre-built colored Paragraph — do not re-wrap
                    _cell(row[4]),
                    _cell(row[5]),
                    row[6] if isinstance(row[6], Paragraph) else _cell(row[6]),
                    _cell(row[7]),
                    _cell(row[8]),
                ]
                for row in txn_rows
            ]
            txn_all_rows = [txn_wrapped_header] + txn_wrapped_data
            txn_t = Table(txn_all_rows, colWidths=txn_col_widths, repeatRows=1)
            txn_t.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, 0),  C_DARK),
                ("TEXTCOLOR",     (0, 0), (-1, 0),  C_WHITE),
                ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
                ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 4),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
                ("ALIGN",         (4, 0), (-1, -1), "RIGHT"),
                ("ALIGN",         (0, 0), (3,  -1), "LEFT"),
                ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
            ]))
            story.append(txn_t)
            story.append(Spacer(1, 2))

            story.append(Paragraph(
                f"Source: {_ins.get('_source', 'SEC EDGAR Form 4')}.  "
                f"As of: {_ins.get('_as_of', 'current')}.  "
                f"Open-market buy/sell transactions only — option exercises, RSU "
                f"vesting, and gifts excluded. % of Pos. is the share of the "
                f"insider's prior holding represented by that single transaction "
                f"(e.g. selling 300,000 of 6,000,000 prior shares = 5.0%) — a "
                f"better gauge of materiality than dollar value alone, since the "
                f"same dollar amount can mean routine trimming for a large holder "
                f"or a near-total exit for a small one; shown in red at 50%+. "
                f"A Rule 10b5-1 plan is a pre-arranged, "
                f"written trading schedule adopted in advance, before the insider "
                f"has any material non-public information; trades then execute "
                f"automatically on that schedule regardless of what the insider "
                f"later learns, which is why they're a weaker signal of current "
                f"sentiment than an unscheduled, discretionary trade. Transactions "
                f"marked [10b5-1] are flagged accordingly and excluded from the "
                f"buyer count and cluster-buying signal above, but are still "
                f"included in buy/sell dollar totals.",
                styles["meta"]
            ))
        elif _own or _ins:
            lookback = _ins.get("lookback_days", 90) if _ins else 90
            story.append(Paragraph(
                f"<i>No open-market insider transactions found via SEC Form 4 "
                f"in the last {lookback} days.</i>",
                styles["meta"]
            ))

        # ── 8c. Short Interest (subsection) ──
        _short = short_interest or {}
        if _own or _ins or _short:
            story.append(Spacer(1, 8))
            t_short_hdr = Table(
                [[Paragraph("Short Interest  (FINRA, via yfinance)", styles["section"])]],
                colWidths=["100%"]
            )
            t_short_hdr.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#2C3E50")),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(t_short_hdr)
            story.append(Spacer(1, 2))

        if _short and _short.get("shares_short") is not None:
            shares_short = _short.get("shares_short")
            shares_prior = _short.get("shares_short_prior_month")
            pct_change   = _short.get("pct_change_mom")
            dtc          = _short.get("days_to_cover")
            pct_float    = _short.get("short_pct_of_float")
            as_of        = _short.get("_as_of", "unknown")

            short_header = ["As Of", "Shares Short"]
            short_row    = [as_of, f"{shares_short:,}"]
            if shares_prior is not None:
                short_header.append("Prior Month")
                short_row.append(f"{shares_prior:,}")
            if pct_change is not None:
                short_header.append("MoM Change")
                short_row.append(f"{'+' if pct_change >= 0 else ''}{pct_change*100:.1f}%")
            if dtc is not None:
                short_header.append("Days to Cover")
                short_row.append(f"{dtc:.2f}x")
            if pct_float is not None:
                short_header.append("% of Float")
                short_row.append(f"{pct_float*100:.2f}%")
            story.append(_ratio_table(short_header, [short_row]))
            story.append(Spacer(1, 3))

            story.append(Paragraph(
                f"Source: {_short.get('_source', 'yfinance')}.  "
                f"Short interest is reported by member firms to FINRA twice "
                f"monthly (mid-month and end-of-month) with roughly a two-week "
                f"publication lag, so the figures above reflect the most "
                f"recently published settlement date, not the report date. "
                f"Days to cover (short ratio) estimates how many trading days "
                f"it would take to close out all short positions at average "
                f"volume — there's no universal \"high\" threshold, as typical "
                f"levels vary by sector and float size. A rising % of float "
                f"alongside deteriorating fundamentals, guidance, or insider "
                f"selling (see sections above) is a more meaningful combined "
                f"signal than short interest viewed in isolation.",
                styles["meta"]
            ))
        elif _own or _ins or _short:
            story.append(Paragraph(
                "<i>No short interest data available for this ticker.</i>",
                styles["meta"]
            ))

        # ── Appendix: abbreviations ──
        # Always rendered: every report uses abbreviations somewhere. Content
        # is gated to the sections this particular report produced, so a bank
        # gets the NIM/CET1 terms and a REIT gets FFO/AFFO, not both.
        story.append(PageBreak())
        story += _section_header("Appendix · Abbreviations &amp; Definitions",
                                 styles, anchor="appendix")
        story.append(Paragraph(
            "Terms as used in this report. Definitions describe this "
            "pipeline's specific calculation where it differs from the "
            "general textbook meaning.",
            styles["meta"]
        ))
        story.append(Spacer(1, 4))
        story += _abbreviations_appendix(styles, _rendered_labels, ticker)

        # ── Build PDF ──
        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=20 * mm,
            rightMargin=20 * mm,
            topMargin=15 * mm,
            bottomMargin=18 * mm,
        )
        wm_cb = _make_watermark_callback(author, ticker)
        doc.build(story, onFirstPage=wm_cb, onLaterPages=wm_cb)
        return output_path


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _short_period(period_str: str) -> str:
    """'2025-09-27 (FY)' → 'FY25' for compact table headers."""
    try:
        year = period_str[:4]
        yr2  = year[2:]
        if "(FY)" in period_str:
            return f"FY{yr2}"
        return year
    except Exception: 
        return period_str[:7]


def _short_ol_key(key: str) -> str:
    """'2023-09-30 (FY)→2024-09-28 (FY)' → 'FY23→FY24'"""
    parts = key.split("→")
    return "→".join(_short_period(p.strip()) for p in parts)
