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
    s = str(val)
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


def _drop_all_na(rows: list) -> list:
    """
    Remove rows where every data cell (columns 1+) is an N/A variant.
    Keeps label column 0 out of the check.
    Used to suppress metrics that are entirely unavailable for a ticker
    (e.g. OXY Operating Cost Ratio when cost tags are missing).
    """
    return [r for r in rows if not all(_is_na_cell(c) for c in r[1:])]


def _ratio_table(header_row: list, data_rows: list,
                 col_widths=None) -> Table:
    """
    Builds a ratio table. Every cell is wrapped in a Paragraph so ReportLab
    word-wraps long strings (e.g. diagnostic N/A messages) instead of clipping.
    header_row: list of strings
    data_rows:  list of lists of strings
    """
    # Wrap header cells
    wrapped_header = [Paragraph(str(c), _CELL_STYLE_HEADER) for c in header_row]
    # Wrap data cells — col 0 is the label (left-aligned), rest are values
    wrapped_data = []
    for row in data_rows:
        wrapped_row = []
        for j, c in enumerate(row):
            p = _cell(str(c))
            if j == 0:
                # Label column: use normal (non-muted) style even for N/A labels
                p = Paragraph(str(c), _CELL_STYLE)
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
    t.setStyle(TableStyle(style))
    return t


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


def _build_exec_summary(ticker, fundamental, valuation, commentary,
                        management_quality, insider_activity, analyst_targets,
                        peer_comparison, periods, track_analysis=None,
                        risk=None) -> list[str]:
    """
    Returns a list of plain-text bullet strings (no leading '•').
    Fixed slot order: valuation → fundamentals → management → risk →
    insider → watch. Slots are omitted when data is unavailable rather
    than emitting a placeholder.
    """
    bullets = []

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

        if avg_pct is not None:
            if avg_pct <= 33:
                valuation_stance = f"trades at a premium to {industry} peers"
            elif avg_pct >= 67:
                valuation_stance = f"trades at a discount to {industry} peers"
            else:
                valuation_stance = f"trades broadly in line with {industry} peers"

            pe_str = f"P/E {pe_subj:.1f}x vs. peer median {pe_med:.1f}x" \
                     if pe_subj and pe_med else ""
        else:
            # Fallback: current vs. FY-end P/E
            cur_pe  = (valuation or {}).get("pe_current")
            hist_pe = (valuation or {}).get("pe_fy_end", {})
            most_recent_pe = hist_pe.get(periods[0]) if periods else None
            if cur_pe and most_recent_pe:
                try:
                    cur_f  = float(str(cur_pe).replace("x",""))
                    hist_f = float(str(most_recent_pe).replace("x",""))
                    if cur_f < hist_f * 0.85:
                        valuation_stance = "trades below recent historical multiples"
                    elif cur_f > hist_f * 1.15:
                        valuation_stance = "trades above recent historical multiples"
                    else:
                        valuation_stance = "trades in line with recent historical multiples"
                    pe_str = f"current P/E {cur_f:.1f}x vs. FY-end {hist_f:.1f}x"
                except Exception:
                    valuation_stance = pe_str = None
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
        if cagr_str:
            parts.append(f"revenue 2-yr CAGR {cagr_str}")

        if gm_bullet:
            # Strip the bullet prefix to get a clean clause
            gm_clean = gm_bullet.lstrip("•").strip()
            parts.append(gm_clean[0].lower() + gm_clean[1:].rstrip("."))

        # FCF: flag thin or deteriorating margin
        fcf_vals = fundamental.get("fcf_margin", {})
        if fcf_vals and periods:
            try:
                latest_fcf = float(str(fcf_vals.get(periods[0], "")).replace("%",""))
                if latest_fcf < 5:
                    parts.append(f"FCF margin thin at {latest_fcf:.1f}%")
                elif latest_fcf > 15:
                    parts.append(f"strong FCF margin {latest_fcf:.1f}%")
            except Exception:
                pass

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

    return bullets


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
        _TOC_SECTIONS = [
            ("s1", "1 · Fundamental Analysis"),
            ("s2", "2 · Risk & Solvency"),
            ("s3", "3 · Valuation"),
            ("s4", "4 · Red Flags"),
            ("s5", "5 · Trend Commentary"),
            ("s6", "6 · Management Guidance & Tone"),
            ("s7", "7 · Management Track Record"),
            ("s8", "8 · Insider & Institutional Information"),
        ]
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
        _exec_bullets = _build_exec_summary(
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

        fund_header = ["Metric"] + [_short_period(p) for p in periods]
        gm_label = fundamental.get("gross_margin_label", "Gross Margin")
        sg = fundamental.get("sector_group", "general")

        # Build fundamentals rows dynamically — only show rows meaningful for the sector
        fund_rows = []
        fund_rows.append(
            [gm_label] + [fundamental["gross_margin"].get(p, "N/A") for p in periods]
        )
        fund_rows.append(
            ["Operating Margin"] + [fundamental["operating_margin"].get(p, "N/A") for p in periods]
        )
        if sg != "financials":
            fund_rows.append(
                ["EBITDA Margin"] + [fundamental["ebitda_margin"].get(p, "N/A") for p in periods]
            )
        fund_rows.append(
            ["Net Margin"] + [fundamental["net_margin"].get(p, "N/A") for p in periods]
        )
        fund_rows.append(
            ["ROE"] + [fundamental["roe"].get(p, "N/A") for p in periods]
        )
        fund_rows.append(
            ["ROA"] + [fundamental["roa"].get(p, "N/A") for p in periods]
        )
        if sg == "financials":
            fund_rows.append(
                ["Efficiency Ratio"] + [fundamental["efficiency_ratio"].get(p, "N/A") for p in periods]
            )
            fund_rows.append(
                ["ROTCE"] + [fundamental["rotce"].get(p, "N/A") for p in periods]
            )
        else:
            fund_rows.append(
                ["ROIC (proxy)"] + [fundamental["roic_proxy"].get(p, "N/A") for p in periods]
            )
        fund_rows.append(
            ["Asset Turnover"] + [fundamental["asset_turnover"].get(p, "N/A") for p in periods]
        )
        if sg == "general":
            fund_rows.append(
                ["Inv. Turnover"] + [fundamental["inventory_turnover"].get(p, "N/A") for p in periods]
            )
            fund_rows.append(
                ["Days Sales of Inv."] + [fundamental["dsi"].get(p, "N/A") for p in periods]
            )
        if sg != "financials":
            fund_rows.append(
                ["FCF Margin"] + [fundamental["fcf_margin"].get(p, "N/A") for p in periods]
            )
        # Add revenue CAGR as a single merged row note
        cagr_note = f"Revenue {len(periods)-1}-yr CAGR: {fundamental.get('revenue_cagr', 'N/A')}"
        story.append(_ratio_table(fund_header, _drop_all_na(fund_rows)))
        story.append(Spacer(1, 2))
        story.append(Paragraph(cagr_note, styles["meta"]))

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
            story.append(_ratio_table(b_header, _drop_all_na(b_rows)))
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
        story.append(_ratio_table(risk_header, _drop_all_na(risk_rows)))

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
            if cq.get("nearest_maturity") and cq["nearest_maturity"] != "N/A":
                cq_rows.append(["Nearest Maturity", cq["nearest_maturity"]])
            story.append(_ratio_table(cq_rows[0], cq_rows[1:]))
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

        # ── Section 3: Valuation ──
        story += _section_header("3 · Valuation", styles, anchor="s3")

        # Two price contexts: historical FY-end and current
        # Show as sub-groups in the table
        curr_p = periods[0] if periods else "—"
        val_header = ["Metric"] + [_short_period(p) for p in periods]

        # Build valuation rows dynamically
        val_rows = []
        val_rows.append(
            ["P/E (FY-end price)"] + [valuation["pe_historical"].get(p, "N/A") for p in periods]
        )
        val_rows.append(
            ["P/E (current price)"] + [valuation["pe_current"].get(p, "—") for p in periods]
        )
        val_rows.append(
            ["P/B (FY-end price)"] + [valuation["pb_historical"].get(p, "N/A") for p in periods]
        )
        val_rows.append(
            ["P/B (current price)"] + [valuation["pb_current"].get(p, "—") for p in periods]
        )
        val_rows.append(
            ["P/TBV"] + [valuation["ptbv"].get(p, "N/A") for p in periods]
        )
        if sg != "financials":
            val_rows.append(
                ["EV/EBITDA"] + [valuation["ev_ebitda"].get(p, "N/A") for p in periods]
            )
        val_rows.append(
            ["EV/Sales (approx.)"] + [valuation["ev_sales"].get(p, "N/A") for p in periods]
        )
        story.append(_ratio_table(val_header, _drop_all_na(val_rows)))
        story.append(Spacer(1, 2))

        cp_str = f"${valuation.get('current_price', 0):.2f}" if valuation.get("current_price") else "N/A"
        story.append(Paragraph(
            f"Current price: {cp_str}   Market cap: {_fmt_market_cap(valuation.get('market_cap', 0))}",
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
            story.append(_ratio_table(wacc_rows[0], wacc_rows[1:]))
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
            n_peers  = len(peer_tickers)
            story.append(Paragraph(
                f"Industry: {industry}  |  {n_peers} peers "
                f"({', '.join(peer_tickers)})  "
                f"(source: {peer_comparison.get('peer_source', '')})",
                styles["body"]
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

            # ── Section 7: Management Track Record ──
            ta = track_analysis or {}
            if ta.get("available"):
                story += _section_header("7 · Management Track Record", styles, anchor="s7")

                # ── Composite Management Quality Score ────────────────────
                mq = management_quality or {}
                mq_score = mq.get("score")
                if mq_score is not None:
                    mq_color = (
                        "#27AE60" if mq_score >= 70
                        else "#E67E22" if mq_score >= 45
                        else "#E74C3C"
                    )
                    avail_pct = mq.get("available_pct", 1.0)
                    avail_note = (
                        "" if avail_pct >= 0.99
                        else f"  <font color='#7F8C8D'>(based on {int(avail_pct*100)}% of full weighting)</font>"
                    )
                    story.append(Paragraph(
                        f"<b>Management Quality Score: "
                        f"<font color='{mq_color}'>{mq_score}/100</font></b>"
                        f"{avail_note}",
                        styles["body"]
                    ))
                    story.append(Spacer(1, 4))

                    # Component breakdown row
                    comps = mq.get("components", {})
                    comp_rows = [[
                        Paragraph("<b>Component</b>", styles["meta"]),
                        Paragraph("<b>Weight</b>",    styles["meta"]),
                        Paragraph("<b>Score</b>",     styles["meta"]),
                        Paragraph("<b>Contribution</b>", styles["meta"]),
                    ]]
                    total_w = sum(
                        v["base_weight"] for v in comps.values()
                        if v.get("score") is not None
                    ) or 1.0
                    for key in ["accuracy", "execution", "communication"]:
                        c = comps.get(key, {})
                        c_score  = c.get("score")
                        c_weight = c.get("base_weight", 0)
                        c_label  = c.get("label", key.title())
                        eff_w    = c_weight / total_w
                        if c_score is not None:
                            contrib  = round(c_score * eff_w)
                            s_color  = (
                                "#27AE60" if c_score >= 70
                                else "#E67E22" if c_score >= 45
                                else "#E74C3C"
                            )
                            score_str  = f"<font color='{s_color}'>{c_score}/100</font>"
                            contrib_str = f"{contrib}pts"
                        else:
                            score_str   = "<font color='#95A5A6'>N/A</font>"
                            contrib_str = "—"
                        comp_rows.append([
                            Paragraph(c_label, styles["body"]),
                            Paragraph(f"{int(c_weight*100)}%", styles["body"]),
                            Paragraph(score_str, styles["body"]),
                            Paragraph(contrib_str, styles["body"]),
                        ])

                    comp_tbl = Table(comp_rows, colWidths=[45*mm, 18*mm, 22*mm, 25*mm])
                    comp_tbl.setStyle(TableStyle([
                        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#2C3E50")),
                        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.HexColor("#FFFFFF")),
                        ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
                        ("ROWBACKGROUNDS",(0, 1), (-1, -1),
                         [colors.HexColor("#F8F9FA"), colors.HexColor("#FFFFFF")]),
                        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#BDC3C7")),
                        ("ALIGN",         (1, 0), (-1, -1), "CENTER"),
                        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
                        ("TOPPADDING",    (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]))
                    story.append(comp_tbl)
                    story.append(Spacer(1, 8))

                # Summary bar
                cred   = ta.get("credibility")   # None = unrated
                cred_note = ta.get("credibility_note")
                trend  = ta.get("trend", "insufficient data")
                attrib = ta.get("attribution", {})
                ext_pct = attrib.get("external_pct", 0) * 100
                int_pct = attrib.get("internal_pct", 0) * 100

                # Credibility score colour — grey when unrated
                if cred is None:
                    cred_str   = "Unrated"
                    cred_color = "#7F8C8D"
                else:
                    cred_str   = f"{cred}/100"
                    cred_color = (
                        "#27AE60" if cred >= 70
                        else "#E67E22" if cred >= 45
                        else "#E74C3C"
                    )
                story.append(Paragraph(
                    f"<b>Credibility Score:</b> "
                    f"<font color='{cred_color}'><b>{cred_str}</b></font>  "
                    f"&nbsp;&nbsp;Trend: <b>{trend.title()}</b>  "
                    f"&nbsp;&nbsp;Attribution: "
                    f"{ext_pct:.0f}% external / {int_pct:.0f}% internal",
                    styles["body"]
                ))
                if cred_note:
                    story.append(Paragraph(
                        f"<i>Note: {cred_note}</i>",
                        styles["meta"]
                    ))
                story.append(Spacer(1, 6))

                # Per-quarter table
                quarters = ta.get("quarters", [])
                if quarters:
                    # Build table data
                    hdrs = ["Quarter", "Metric", "Guided", "Actual", "Outcome"]
                    rows = [hdrs]
                    for q in quarters:
                        q_label = (
                            f"Q{q['quarter']} FY{q['fiscal_year']}"
                            if q.get("quarter") and q.get("fiscal_year")
                            else q.get("date","")[:7]
                        )
                        def _fmt_rev(v):
                            if v is None: return "—"
                            if v >= 1e9:  return f"${v/1e9:.2f}B"
                            if v >= 1e6:  return f"${v/1e6:.0f}M"
                            return f"${v:,.0f}"

                        def _outcome_color(out):
                            if "BEAT"   in out: return "#27AE60"
                            if "MISS"   in out: return "#E74C3C"
                            if "INLINE" in out: return "#2980B9"
                            return "#7F8C8D"

                        for metric_key, metric_label, fmt_fn in [
                            ("revenue",      "Revenue",     _fmt_rev),
                            ("eps",          "EPS",         lambda v: f"${v:.2f}" if v else "—"),
                            ("gross_margin", "Gross Margin",lambda v: f"{v*100:.1f}%" if v else "—"),
                        ]:
                            m = q.get(metric_key, {})
                            if not m or not m.get("outcome"):
                                continue
                            out = m.get("outcome", "")
                            if out.startswith("PERIOD MISMATCH"):
                                # Period mismatches (annual guidance vs quarterly
                                # actual or vice versa) are excluded from the
                                # track record table per the footnote
                                # methodology — they are not real beat/miss/
                                # inline outcomes.
                                continue
                            lo  = m.get("guided_low")
                            hi  = m.get("guided_high")
                            act = m.get("actual")
                            guided_str = (
                                f"{fmt_fn(lo)} – {fmt_fn(hi)}"
                                if lo and hi else "—"
                            )
                            color = _outcome_color(out)
                            rows.append([
                                q_label,
                                metric_label,
                                guided_str,
                                fmt_fn(act),
                                Paragraph(f"<font color='{color}'><b>{out}</b></font>",
                                          styles["body"]),
                            ])
                            q_label = ""   # only show quarter label on first row

                    if len(rows) > 1:
                        col_w = [22*mm, 28*mm, 38*mm, 28*mm, 30*mm]
                        tbl = Table(rows, colWidths=col_w, repeatRows=1)
                        tbl.setStyle(TableStyle([
                            ("BACKGROUND",  (0,0), (-1,0),  colors.HexColor("#2C3E50")),
                            ("TEXTCOLOR",   (0,0), (-1,0),  colors.HexColor("#FFFFFF")),
                            ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
                            ("FONTSIZE",    (0,0), (-1,-1), 7.5),
                            ("ROWBACKGROUNDS", (0,1), (-1,-1),
                             [colors.HexColor("#F8F9FA"), colors.HexColor("#FFFFFF")]),
                            ("GRID",        (0,0), (-1,-1), 0.3, colors.HexColor("#BDC3C7")),
                            ("ALIGN",       (2,1), (-1,-1), "RIGHT"),
                            ("LEFTPADDING", (0,0), (-1,-1), 4),
                            ("RIGHTPADDING",(0,0), (-1,-1), 4),
                            ("TOPPADDING",  (0,0), (-1,-1), 3),
                            ("BOTTOMPADDING",(0,0),(-1,-1), 3),
                        ]))
                        story.append(tbl)
                        story.append(Spacer(1, 6))

                # Tracker flags
                for flag in ta.get("flags", []):
                    story.append(Paragraph(flag, styles["flag"]))
                    story.append(Spacer(1, 2))

                # Summary line
                if ta.get("summary"):
                    story.append(Paragraph(
                        ta["summary"], styles["meta"]
                    ))

                # ── Communication Score ────────────────────────────────────
                cq = communication_quality or {}
                cq_score = cq.get("score")
                if cq_score is not None:
                    cq_color = (
                        "#27AE60" if cq_score >= 70
                        else "#E67E22" if cq_score >= 45
                        else "#E74C3C"
                    )
                    story.append(Spacer(1, 4))
                    story.append(Paragraph(
                        f"<b>Communication Score:</b> "
                        f"<font color='{cq_color}'><b>{cq_score}/100</b></font>"
                        f"  &nbsp;&nbsp;<font color='#7F8C8D'>"
                        f"{cq.get('alignment', '')} "
                        f"({cq.get('tone_label', '')}, "
                        f"credibility {cq.get('credibility', '?')}/100)</font>",
                        styles["body"]
                    ))

                # ── Execution Quality ──────────────────────────────────────
                eq = execution_quality or {}
                eq_score    = eq.get("score")
                eq_quarters = eq.get("quarters", [])

                story.append(Spacer(1, 8))
                story += _section_header("Execution Quality", styles)

                if eq_score is None or not eq_quarters:
                    story.append(Paragraph(
                        "Insufficient scored quarters to compute execution quality.",
                        styles["meta"]
                    ))
                else:
                    eq_color = (
                        "#27AE60" if eq_score >= 70
                        else "#E67E22" if eq_score >= 45
                        else "#E74C3C"
                    )
                    story.append(Paragraph(
                        f"<b>Execution Score:</b> "
                        f"<font color='{eq_color}'><b>{eq_score}/100</b></font>"
                        f"  &nbsp;&nbsp;<font color='#7F8C8D'>"
                        f"({eq.get('n_quarters', 0)} scored quarter(s))</font>",
                        styles["body"]
                    ))
                    story.append(Spacer(1, 6))
                    bar_rows = [["Quarter", "Rev Δ%", "GM Δpp", "Score"]]
                    for qd in eq_quarters:
                        rev_str = (f"{qd['rev_delta_pct']:+.1f}%"
                                   if qd["rev_delta_pct"] is not None else "—")
                        gm_str  = (f"{qd['gm_delta_pp']:+.1f}pp"
                                   if qd["gm_delta_pp"] is not None else "—")
                        qs = qd["quarter_score"]
                        qs_color = (
                            "#27AE60" if qs >= 70
                            else "#E67E22" if qs >= 45
                            else "#E74C3C"
                        )
                        bar_rows.append([
                            qd["label"], rev_str, gm_str,
                            Paragraph(
                                f"<font color='{qs_color}'><b>{qs}/100</b></font>",
                                styles["body"]
                            ),
                        ])
                    col_w = [30*mm, 25*mm, 25*mm, 30*mm]
                    eq_tbl = Table(bar_rows, colWidths=col_w, repeatRows=1)
                    eq_tbl.setStyle(TableStyle([
                        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#34495E")),
                        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.HexColor("#FFFFFF")),
                        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
                        ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
                        ("ROWBACKGROUNDS",(0, 1), (-1, -1),
                         [colors.HexColor("#F8F9FA"), colors.HexColor("#FFFFFF")]),
                        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#BDC3C7")),
                        ("ALIGN",         (1, 1), (-1, -1), "RIGHT"),
                        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
                        ("TOPPADDING",    (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]))
                    story.append(eq_tbl)
                    story.append(Spacer(1, 4))

                # ── Section 7 Footnote ─────────────────────────────────────
                story.append(Spacer(1, 10))
                footnote_style = ParagraphStyle(
                    "s7_footnote",
                    parent=styles["meta"],
                    fontSize=6.5,
                    textColor=colors.HexColor("#95A5A6"),
                    fontName="Helvetica-Oblique",
                )
                story.append(Paragraph(
                    "<b>Scoring methodology:</b> "
                    "Management Quality Score = Guidance Accuracy (40%) + Execution Quality (40%) "
                    "+ Communication Quality (20%); weights renormalised when components are unavailable. "
                    "Credibility Score (0–100): 100 = all beats, 0 = all misses; Unrated when no guidance comparisons available (feature in development). "
                    "Execution Score (0–100): actual vs guided midpoint — revenue ±25% range, GM ±10pp; "
                    "50 = on guide, 100 = max beat. "
                    "Communication Score (0–100): tone-outcome alignment — Confident + strong delivery = 90; "
                    "Confident + weak delivery = 20; Cautious + strong delivery = 70. "
                    "Period mismatches (annual guidance vs quarterly actual) excluded. "
                    "All figures from public SEC filings and earnings transcripts; not independently verified.",
                    footnote_style
                ))

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

            own_summary_header = []
            own_summary_row    = []
            if inst_pct is not None:
                own_summary_header.append("Institutional")
                own_summary_row.append(f"{inst_pct*100:.1f}%")
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
            story.append(_ratio_table(summary_header, [summary_row]))
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
            txn_col_widths = [16*mm, 23*mm, 27*mm, 16*mm, 16*mm, 18*mm, 14*mm, 18*mm, 22*mm]
            txn_wrapped_header = [Paragraph(str(c), _CELL_STYLE_HEADER)
                                  for c in txn_header]
            txn_wrapped_data = [
                [
                    _cell(row[0]),
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