"""
guidance_tracker.py — Management guidance accuracy tracker

Compares what management guided vs what actually happened, producing:
  - Per-metric beat/miss/inline for revenue, EPS, gross margin
  - Miss attribution (external macro vs internal execution language)
  - Credibility score (0-100) across last N quarters
  - Trend: improving / stable / deteriorating accuracy

Data sources:
  - Guidance text: fool_transcripts.db (TranscriptLoader history)
  - Actuals:       CompanyFinancialProfile (XBRL via data_layer.py)

Usage
-----
    from guidance_tracker import GuidanceTracker
    tracker = GuidanceTracker()
    result  = tracker.analyse(ticker, profile, transcript_loader)
"""

import re
import sqlite3
import logging
import os

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fool_transcripts.db"
)

# ── Guidance extraction patterns ──────────────────────────────────────────────

# Revenue guidance: "revenue of approximately $X billion" or "$X to $Y billion"
# Revenue guidance — next-quarter only (excludes full-year to avoid
# matching "$80B full-year guidance" against a $2B quarterly actual)
_REV_RE = re.compile(
    r"(?:revenue|net revenue|total revenue)"
    r"[^.]{0,60}"
    r"\$\s*([\d,\.]+)\s*(?:billion|million|B\b|M\b)?\s*(?:to|-)\s*\$?\s*([\d,\.]+)\s*(billion|million|B\b|M\b)"
    r"|(?:revenue|net revenue|total revenue)"
    r"[^.]{0,120}"
    r"range\s+of\s+(?:approximately\s+)?\$\s*([\d,\.]+)\s*(?:billion|million|B\b|M\b)?"
    r"\s*(?:to|-)\s*\$?\s*([\d,\.]+)\s*(billion|million|B\b|M\b)"
    r"|(?:revenue|net revenue|total revenue)"
    r"[^.]{0,60}"
    r"(?:approximately|around)\s*\$\s*([\d,\.]+)\s*(billion|million|B\b|M\b)",
    re.IGNORECASE
)

# Reject revenue guidance sentences that are clearly full-year/annual.
# Used as a pre-filter before _REV_RE to skip annual totals.
_REV_ANNUAL_FILTER = re.compile(
    r"(?:full[- ]year|annual|fiscal[- ]year\b|for\s+(?:fiscal\s+)?20\d{2})",
    re.IGNORECASE
)

# EPS guidance: "EPS of $X.XX" or "$X.XX to $X.XX per share"
_EPS_RE = re.compile(
    r"(?:EPS|earnings per (?:diluted )?share|diluted EPS|non-GAAP EPS)"
    r"[^.]{0,60}"
    r"\$\s*([\d\.]+)\s*(?:to|-)\s*\$?\s*([\d\.]+)"
    r"|\$\s*([\d\.]+)\s*(?:to|-)\s*\$?\s*([\d\.]+)\s*per\s*(?:diluted\s*)?share",
    re.IGNORECASE
)

# Gross margin guidance: "gross margin of approximately X%"
_GM_RE = re.compile(
    r"gross\s+margin[^.]{0,60}"
    r"([\d\.]+)\s*(?:%|percent)\s*(?:to|-)\s*([\d\.]+)\s*(?:%|percent)"
    r"|gross\s+margin[^.]{0,60}"
    r"(?:approximately|of|around)\s*([\d\.]+)\s*(?:%|percent)",
    re.IGNORECASE
)
# Sanity bounds for parsed gross margin — reject implausible ranges
_GM_MIN, _GM_MAX = 5.0, 95.0   # percent — rejects 0.5%–49% style mis-parses

# External attribution language
_EXTERNAL_RE = re.compile(
    r"\b(?:macro(?:economic)?|foreign exchange|currency|FX|tariff|trade war|"
    r"supply chain|demand environment|geopolit|inflation|interest rate|"
    r"consumer spending|weather|pandemic|war|sanctions|regulatory|"
    r"industry-wide|sector-wide|market condition|commodity price|"
    r"lower demand|softer demand|demand weakness|demand softness)\b",
    re.IGNORECASE
)

# Internal attribution language
_INTERNAL_RE = re.compile(
    r"\b(?:execution|we underestimated|operational|cost overrun|"
    r"pricing error|we fell short|below our expectation|"
    r"our own|self-inflicted|internal|integration|transition|"
    r"product delay|launch delay|ramp slower|slower than expected ramp)\b",
    re.IGNORECASE
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_dollars(val: float, unit: str) -> float:
    """Convert a value + unit string to dollars."""
    u = unit.lower().strip()
    if u in ("billion", "b"):
        return val * 1e9
    if u in ("million", "m"):
        return val * 1e6
    return val


def _parse_rev_guidance(text: str) -> tuple[float, float] | None:
    """
    Extract next-quarter revenue guidance range from text.
    Returns (low, high) in dollars, or None if not found.
    Skips sentences that appear to be full-year/annual guidance.
    """
    # Split into sentences and filter out annual guidance sentences
    sentences = re.split(r'(?<=[.!?])\s+', text)
    filtered  = ' '.join(
        s for s in sentences
        if not _REV_ANNUAL_FILTER.search(s)
    )
    for m in _REV_RE.finditer(filtered):
        g = m.groups()
        try:
            if g[0] and g[1] and g[2]:    # arm1: $X to $Y billion (unit optional on first)
                lo = _to_dollars(float(g[0].replace(",", "")), g[2])
                hi = _to_dollars(float(g[1].replace(",", "")), g[2])
                return lo, hi
            elif g[3] and g[4] and g[5]:  # arm2: range of approximately $X to $Y billion
                lo = _to_dollars(float(g[3].replace(",", "")), g[5])
                hi = _to_dollars(float(g[4].replace(",", "")), g[5])
                return lo, hi
            elif g[6] and g[7]:           # arm3: approximately $X billion (point estimate)
                v = _to_dollars(float(g[6].replace(",", "")), g[7])
                return v * 0.98, v * 1.02
        except (ValueError, TypeError):
            continue
    return None


def _parse_eps_guidance(text: str) -> tuple[float, float] | None:
    """
    Extract EPS guidance range from text.
    Returns (low, high), or None.
    Pre-filters sentences to exclude dividend/distribution language so
    "declared $0.01 per share dividend" doesn't match the per-share arm.
    """
    # Sentence-level filter: exclude dividend/distribution sentences
    _DIV_FILTER = re.compile(
        r"\b(?:dividend|distribution|declared|quarterly\s+payment|"
        r"per\s+share\s+dividend|cash\s+dividend)\b",
        re.IGNORECASE
    )
    sentences = re.split(r"(?<=[.!?])\s+", text)
    filtered  = " ".join(s for s in sentences if not _DIV_FILTER.search(s))

    for m in _EPS_RE.finditer(filtered):
        g = m.groups()
        try:
            if g[0] and g[1]:
                return float(g[0]), float(g[1])
            elif g[2] and g[3]:
                return float(g[2]), float(g[3])
        except (ValueError, TypeError):
            continue
    return None


def _parse_gm_guidance(text: str) -> tuple[float, float] | None:
    """
    Extract gross margin guidance range from text.
    Returns (low_pct, high_pct) as decimals e.g. (0.55, 0.57), or None.
    Rejects implausible ranges (e.g. 0.5%–49% from AAPL mis-parse).
    """
    for m in _GM_RE.finditer(text):
        g = m.groups()
        try:
            if g[0] and g[1]:
                lo, hi = float(g[0]), float(g[1])
                # Reject if outside plausible bounds or range is too wide (>20pp)
                if lo < _GM_MIN or hi > _GM_MAX or (hi - lo) > 20:
                    continue
                return lo / 100, hi / 100
            elif g[2]:
                v = float(g[2])
                if v < _GM_MIN or v > _GM_MAX:
                    continue
                return (v - 1.5) / 100, (v + 1.5) / 100   # ±150bps band
        except (ValueError, TypeError):
            continue
    return None


def _beat_miss(actual: float, lo: float, hi: float) -> str:
    """
    Classify actual vs guidance range.
    Guard: period mismatch detection — quarterly actual vs annual guidance
    or vice versa. Threshold: if actual differs from guided midpoint by
    more than 3x in either direction, flag as mismatch rather than BEAT/MISS.
    This catches LLY ($80B annual guidance vs $19.8B quarterly actual = 4x diff)
    and NFLX ($50B annual vs $12.25B quarterly = 4x diff).
    """
    mid = (lo + hi) / 2
    if mid and mid > 0:
        ratio = actual / mid
        if ratio > 3.0 or ratio < 0.33:
            # Format based on magnitude
            if abs(actual) >= 1e9 or abs(mid) >= 1e9:
                return (f"PERIOD MISMATCH? "
                        f"actual=${actual/1e9:.2f}B vs guided "
                        f"${lo/1e9:.2f}B–${hi/1e9:.2f}B")
            else:
                return (f"PERIOD MISMATCH? "
                        f"actual=${actual:.2f} vs guided "
                        f"${lo:.2f}–${hi:.2f}")
    if actual >= hi:
        pct = (actual - mid) / abs(mid) * 100 if mid else 0
        return f"BEAT +{pct:.1f}%"
    elif actual <= lo:
        pct = (mid - actual) / abs(mid) * 100 if mid else 0
        return f"MISS -{pct:.1f}%"
    else:
        return "INLINE"


def _credibility_score(results: list[dict]) -> int:
    """
    0-100 score based on beat/miss/inline history.
    BEAT=100, INLINE=80, MISS=0 per metric. Average across all.
    """
    if not results:
        return 50   # neutral if no data

    scores = []
    for r in results:
        for key in ("revenue", "eps", "gross_margin"):
            outcome = r.get(key, {}).get("outcome", "")
            if "BEAT" in outcome:
                scores.append(100)
            elif "INLINE" in outcome:
                scores.append(80)
            elif "MISS" in outcome:
                scores.append(0)

    if not scores:
        return 50
    return round(sum(scores) / len(scores))


# ── Main agent ────────────────────────────────────────────────────────────────

class GuidanceTracker:
    """
    Compares management guidance to actual results across last N quarters.

    Parameters
    ----------
    db_path : path to fool_transcripts.db
    """

    def __init__(self, db_path: str = _DB_PATH):
        self._db_path = db_path

    def analyse(
        self,
        ticker: str,
        profile,           # CompanyFinancialProfile
        transcript_loader, # TranscriptLoader instance
        n_quarters: int = 4,
        cache = None,      # FilingCache — passed to QuarterlyDataProcessor
        earnings_text: str = "",  # 8-K narrative — fallback when transcript has no guidance
    ) -> dict:
        """
        Run guidance vs actuals analysis.

        Returns
        -------
        dict:
            available       : bool
            quarters        : list of per-quarter result dicts
            credibility     : int 0-100
            trend           : "improving" | "stable" | "deteriorating" | "insufficient data"
            attribution     : {"external_pct": float, "internal_pct": float}
            flags           : list of flag strings
            summary         : str one-liner
        """
        ticker = ticker.upper()

        # ── Sector routing ────────────────────────────────────────────────────
        # Financials (banks, insurance) guide on NII, efficiency ratio, CET1 —
        # not revenue/EPS/gross-margin. Current regex parsers are calibrated for
        # non-financial companies. Skip rather than produce misleading credibility
        # scores. TODO: add _BANK_NII_RE, _BANK_EFFICIENCY_RE for financials.
        sector_lower = (getattr(profile, "sector", "") or "").lower()
        _is_financial = any(k in sector_lower for k in
                           ["financ", "bank", "insurance", "reit", "real estate"])
        _is_fintech   = any(k in sector_lower for k in
                           ["fintech", "payments", "financial technology"])
        if _is_financial and not _is_fintech:
            return self._empty(
                "Financials sector — GuidanceTracker pending bank-specific "
                "metric parsers (NII, efficiency ratio, CET1). "
                "Standard revenue/EPS/GM parsers not applicable."
            )

        # Get transcript history from DB
        history = transcript_loader.get_history(ticker) if transcript_loader else []
        if not history:
            return self._empty("No transcript history in DB")

        # ── Build actuals dict ───────────────────────────────────────────────
        # Priority: quarterly standalone actuals (10-Q derived) are more accurate
        # than annual actuals for matching against quarterly guidance figures.
        # Annual actuals caused period mismatches (e.g. CRM BEAT +304% because
        # quarterly EPS guidance was compared against full-year EPS).
        actuals = {}

        # Try quarterly actuals first
        try:
            from quarterly_processor import QuarterlyDataProcessor
            qp = QuarterlyDataProcessor(ticker, cache=cache, n_quarters=n_quarters + 2,
                                             sector=getattr(profile, "sector", ""))
            quarterly_actuals = qp.load()
            if quarterly_actuals:
                # Merge quarterly actuals into actuals dict
                for period_date, vals in quarterly_actuals.items():
                    actuals[period_date] = {
                        "revenue":      vals.get("revenue"),
                        "eps":          vals.get("eps"),
                        "gross_margin": vals.get("gross_margin"),
                    }
                print(f"    [GuidanceTracker] Using quarterly actuals "
                      f"({len(actuals)} periods: {sorted(actuals)[-3:]})")
        except Exception as e:
            logger.debug("GuidanceTracker: quarterly actuals failed (%s) — "
                         "falling back to annual", e)

        # Fallback: annual actuals from 10-K profile
        if not actuals:
            print(f"    [GuidanceTracker] Falling back to annual actuals")
            periods    = profile.periods
            inc        = profile.income_statement
            revenues   = inc.revenue
            net_incomes= inc.net_income
            shares     = inc.diluted_shares
            gp_vals    = inc.gross_profit

            for i, period in enumerate(periods):
                period_date = period[:10]
                rev  = revenues[i]    if i < len(revenues)    else None
                ni   = net_incomes[i] if i < len(net_incomes) else None
                sh   = shares[i]      if i < len(shares)      else None
                eps  = (ni / sh)      if ni and sh and sh > 0 else None
                gp   = gp_vals[i]     if i < len(gp_vals)     else None
                gm   = (gp / rev)     if gp and rev and rev > 0 else None
                actuals[period_date] = {
                    "revenue":      rev,
                    "eps":          eps,
                    "gross_margin": gm,
                }

        # Match guidance quarters to actual periods
        quarters_done = []
        external_hits = 0
        internal_hits = 0
        total_text    = 0

        for entry in history[:n_quarters]:
            q_date  = entry["date"]       # "2026-03-31"
            quarter = entry.get("quarter")
            fy      = entry.get("fiscal_year")
            url     = entry["url"]

            # Fetch transcript text
            try:
                import requests
                resp = requests.get(
                    url,
                    headers={"User-Agent": "EquityPipeline research@equitypipeline.com"},
                    timeout=15
                )
                if resp.status_code != 200:
                    text = ""
                else:
                    from transcript_loader import TranscriptLoader as _TL
                    text, _ = _TL._extract_guidance_text(resp.text, ticker)
            except Exception as e:
                logger.debug("GuidanceTracker: fetch error %s — %s", url, e)
                text = ""

            # Parse guidance from transcript
            rev_guide = _parse_rev_guidance(text) if text else None
            eps_guide = _parse_eps_guidance(text) if text else None
            gm_guide  = _parse_gm_guidance(text)  if text else None

            # Fallback: if transcript found no revenue guidance, try the 8-K narrative.
            # Covers AAPL-style guidance in prepared remarks (truncated by Motley Fool)
            # but present verbatim in the press release.
            if rev_guide is None and earnings_text:
                rev_guide = _parse_rev_guidance(earnings_text)

            if not text and rev_guide is None:
                continue



            # Find matching actual period
            # Transcripts are quarterly — actuals are annual FY periods.
            # Strategy: find the FY period whose end date is closest to and
            # strictly after the transcript (call) date — i.e. genuinely in
            # the future relative to when guidance was given. This handles
            # non-Dec FY ends correctly.
            #
            # Bounded to a maximum forward gap of ~120 days — a single
            # quarter's reporting cadence is ~90 days; allowing materially
            # more than that risks silently skipping over a missing quarter
            # and matching the wrong, later one instead. Confirmed bug:
            # LULU Q3 FY2025's Q4 guidance ($3.50B-$3.59B) should have
            # matched genuine Q4 FY2025 actuals (~51 days after the call),
            # but when those weren't available the matcher skipped ahead to
            # Q1 FY2026 actuals (~143 days after the call) — a 190-day bound
            # was loose enough to permit that wrong skip. No bounded match
            # found → treat as unmatched (matched_actual stays None) rather
            # than guessing.
            _MAX_FORWARD_GAP_DAYS = 120
            matched_actual = None
            matched_period = None
            best_gap = None
            for p_date, actual_vals in actuals.items():
                if p_date > q_date:
                    from datetime import date as _date
                    try:
                        gap = (_date.fromisoformat(p_date) -
                               _date.fromisoformat(q_date)).days
                        if gap > _MAX_FORWARD_GAP_DAYS:
                            continue   # too far forward — likely the wrong quarter
                        if best_gap is None or gap < best_gap:
                            best_gap       = gap
                            matched_actual = actual_vals
                            matched_period = p_date
                    except ValueError:
                        continue

            # Diagnostic — visible in pipeline console
            q_label = f"Q{quarter} FY{fy}" if quarter and fy else q_date
            rev_str = (f"${rev_guide[0]/1e9:.1f}B–${rev_guide[1]/1e9:.1f}B"
                       if rev_guide else "none")
            eps_str = (f"${eps_guide[0]:.2f}–${eps_guide[1]:.2f}"
                       if eps_guide else "none")
            gm_str  = (f"{gm_guide[0]*100:.1f}%–{gm_guide[1]*100:.1f}%"
                       if gm_guide else "none")
            print(f"    [GuidanceTracker] {q_label} | "
                  f"rev={rev_str} eps={eps_str} gm={gm_str} | "
                  f"match→{matched_period or 'none'}")

            # Attribution language
            ext = len(_EXTERNAL_RE.findall(text))
            itn = len(_INTERNAL_RE.findall(text))
            external_hits += ext
            internal_hits += itn
            total_text    += 1

            q_result = {
                "date":       q_date,
                "quarter":    quarter,
                "fiscal_year": fy,
                "guidance": {
                    "revenue":      rev_guide,
                    "eps":          eps_guide,
                    "gross_margin": gm_guide,
                },
                "actual_period": matched_period,
                "revenue":      {},
                "eps":          {},
                "gross_margin": {},
                "attribution":  {"external": ext, "internal": itn},
            }

            # Compare guidance vs actuals
            if matched_actual:
                if rev_guide and matched_actual.get("revenue"):
                    act_rev = matched_actual["revenue"]
                    q_result["revenue"] = {
                        "guided_low":  rev_guide[0],
                        "guided_high": rev_guide[1],
                        "actual":      act_rev,
                        "outcome":     _beat_miss(act_rev, rev_guide[0], rev_guide[1]),
                    }
                if eps_guide and matched_actual.get("eps"):
                    act_eps = matched_actual["eps"]
                    q_result["eps"] = {
                        "guided_low":  eps_guide[0],
                        "guided_high": eps_guide[1],
                        "actual":      act_eps,
                        "outcome":     _beat_miss(act_eps, eps_guide[0], eps_guide[1]),
                    }
                if gm_guide and matched_actual.get("gross_margin"):
                    act_gm = matched_actual["gross_margin"]
                    q_result["gross_margin"] = {
                        "guided_low":  gm_guide[0],
                        "guided_high": gm_guide[1],
                        "actual":      act_gm,
                        "outcome":     _beat_miss(act_gm, gm_guide[0], gm_guide[1]),
                    }

            quarters_done.append(q_result)

        if not quarters_done:
            return self._empty("No guidance figures found in transcripts")

        # Credibility score
        score = _credibility_score(quarters_done)

        # Trend — compare first half vs second half of quarters
        # Only meaningful when BOTH halves have at least one scored comparison.
        # Without this, unscored quarters (no actual yet) return 50 (neutral)
        # and can spuriously show "deteriorating" vs a scored older half.
        def _has_scored(qs):
            return any(
                q.get(k, {}).get("outcome", "") not in ("", "PERIOD MISMATCH?")
                for q in qs
                for k in ("revenue", "eps", "gross_margin")
            )

        if len(quarters_done) >= 4 and _has_scored(quarters_done[:2]) and _has_scored(quarters_done[2:]):
            recent  = _credibility_score(quarters_done[:2])
            older   = _credibility_score(quarters_done[2:])
            if recent > older + 10:
                trend = "improving"
            elif recent < older - 10:
                trend = "deteriorating"
            else:
                trend = "stable"
        else:
            trend = "insufficient data"

        # Attribution
        total_attr = external_hits + internal_hits
        ext_pct    = external_hits / total_attr if total_attr else 0
        int_pct    = internal_hits / total_attr if total_attr else 0

        # Flags
        flags = []
        if score is not None and score < 40:
            flags.append(
                f"■ Management credibility score {score}/100 — "
                f"consistent guidance misses over last {len(quarters_done)} quarters"
            )
        if ext_pct > 0.75 and len(quarters_done) >= 2:
            flags.append(
                f"■ {ext_pct*100:.0f}% of miss attribution is external — "
                f"management consistently blames macro factors"
            )
        if trend == "deteriorating":
            flags.append(
                "■ Guidance accuracy deteriorating over recent quarters"
            )

        # One-line summary
        beats  = sum(
            1 for q in quarters_done
            for k in ("revenue", "eps", "gross_margin")
            if "BEAT" in q.get(k, {}).get("outcome", "")
        )
        misses = sum(
            1 for q in quarters_done
            for k in ("revenue", "eps", "gross_margin")
            if "MISS" in q.get(k, {}).get("outcome", "")
        )
        total_comps = beats + misses + sum(
            1 for q in quarters_done
            for k in ("revenue", "eps", "gross_margin")
            if "INLINE" in q.get(k, {}).get("outcome", "")
        )
        summary = (
            f"Credibility {score}/100 | "
            f"{beats} beats, {misses} misses across {total_comps} guidance comparisons "
            f"({len(quarters_done)} quarters) | trend: {trend}"
        )

        # ── Console summary ───────────────────────────────────────────────────
        print(f"    [GuidanceTracker] {summary}")
        if flags:
            for f_ in flags:
                print(f"    [GuidanceTracker] FLAG: {f_}")
        # Per-quarter outcome table
        print(f"    {'Quarter':<12} {'Metric':<14} {'Guided':<22} {'Actual':<14} Outcome")
        print(f"    {'─'*72}")
        for q in quarters_done:
            q_label = (f"Q{q['quarter']} FY{q['fiscal_year']}"
                       if q.get("quarter") and q.get("fiscal_year")
                       else q.get("date","")[:7])
            first = True
            for metric_key, metric_label in [
                ("revenue", "Revenue"),
                ("eps", "EPS"),
                ("gross_margin", "Gross Margin"),
            ]:
                m = q.get(metric_key, {})
                if not m or not m.get("outcome"):
                    continue
                lo  = m.get("guided_low")
                hi  = m.get("guided_high")
                act = m.get("actual")
                out = m.get("outcome", "")
                if metric_key == "revenue":
                    guided_str = f"${lo/1e9:.2f}B–${hi/1e9:.2f}B" if lo and hi else "—"
                    actual_str = f"${act/1e9:.2f}B" if act else "—"
                elif metric_key == "eps":
                    guided_str = f"${lo:.2f}–${hi:.2f}" if lo and hi else "—"
                    actual_str = f"${act:.2f}" if act else "—"
                else:
                    guided_str = f"{lo*100:.1f}%–{hi*100:.1f}%" if lo and hi else "—"
                    actual_str = f"{act*100:.1f}%" if act else "—"
                label_col = q_label if first else ""
                print(f"    {label_col:<12} {metric_label:<14} {guided_str:<22} {actual_str:<14} {out}")
                first = False
        print()

        return {
            "available":    True,
            "quarters":     quarters_done,
            "credibility":  score,
            "trend":        trend,
            "attribution":  {"external_pct": round(ext_pct, 2), "internal_pct": round(int_pct, 2)},
            "flags":        flags,
            "summary":      summary,
        }

    @staticmethod
    def _empty(reason: str) -> dict:
        return {
            "available":   False,
            "quarters":    [],
            "credibility": None,
            "trend":       "insufficient data",
            "attribution": {"external_pct": 0.0, "internal_pct": 0.0},
            "flags":       [],
            "summary":     reason,
        }
