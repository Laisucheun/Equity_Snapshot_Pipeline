"""
orchestrator.py — EquityAnalystOrchestrator

Entry point for the full pipeline:
    run(ticker, sector, author) → PDF file path

Usage:
    from core.orchestrator import EquityAnalystOrchestrator
    orch = EquityAnalystOrchestrator()
    path = orch.run("AAPL", sector="Technology", author="Jane Smith")
    print(f"Report saved: {path}")

Prerequisites (same as existing notebook):
    pip install edgartools yfinance pandas numpy reportlab
"""

import os
import yfinance as yf
import edgar

from core.agents import FundamentalAgent, RiskAgent, ValuationAgent, TrendCommentaryAgent, GuidanceAgent
from core.renderer import EquityBriefRenderer

# ── Paste your existing classes here or import them ──
# The orchestrator expects these to already be importable:
#   RobustDataProcessor, StatementProfile, IncomeStatementProfile,
#   BalanceSheetProfile, CashFlowProfile, CompanyFinancialProfile
#
# If you run this from the same directory as your notebook-exported .py,
# just import from that file. Otherwise, copy the classes into data_layer.py.
#
# For now we assume they live in data_layer.py:
# from core.data_layer import RobustDataProcessor, CompanyFinancialProfile   # ← old: edgartools single-filing
from core.data_layer import CompanyFinancialProfile
from data.facts_processor import FactsDataProcessor, set_identity as _facts_set_identity
from financials.fr_y9c import FRY9CFetcher, _FALLBACK_RSSD_MAP as RSSD_MAP
from ingestion.ownership_loader import OwnershipLoader
from ingestion.insider_transactions import InsiderTransactionLoader
from ingestion.short_interest_loader import ShortInterestLoader
from market.peer_comparator import PeerComparisonLoader
from financials.company_overview import CompanyOverviewLoader
from market.analyst_targets import AnalystTargetsLoader
from market.price_context import PriceContextLoader
from market.estimate_revisions import EstimateRevisionsLoader
from market.momentum import MomentumLoader
from data.filing_cache import FilingCache
from ingestion.transcript_loader import TranscriptLoader
from ingestion.edgar_transcript import EdgarTranscriptLoader
from ingestion.fool_transcript_db import maybe_update as _db_maybe_update
from financials.guidance_tracker import GuidanceTracker
from core.config import FRED_API_KEY
from market.fred_client import FredClient
from ingestion.debt_note_fetcher import fetch_debt_note
from ingestion.xbrl_debt_fetcher import set_identity as _xbrl_set_identity


# ── Execution quality ─────────────────────────────────────────────────────────

def _compute_execution_quality(track_analysis: dict) -> dict:
    if not track_analysis or not track_analysis.get("available"):
        return {"score": None, "n_quarters": 0, "quarters": []}
    scored = []
    for q in track_analysis.get("quarters", []):
        q_label = (
            f"Q{q['quarter']} FY{q['fiscal_year']}"
            if q.get("quarter") and q.get("fiscal_year")
            else q.get("date", "")[:7]
        )
        rev_score = gm_score = None
        rev_delta = gm_delta = None
        rev = q.get("revenue", {})
        if rev and rev.get("outcome", "").startswith(("BEAT", "MISS")):
            lo, hi, act = rev.get("guided_low"), rev.get("guided_high"), rev.get("actual")
            if lo and hi and act:
                mid = (lo + hi) / 2
                delta_pct = (act - mid) / abs(mid)
                rev_score = max(0, min(100, 50 + delta_pct / 0.25 * 50))
                rev_delta = round(delta_pct * 100, 1)
        gm = q.get("gross_margin", {})
        if gm and gm.get("outcome", "").startswith(("BEAT", "MISS")):
            lo, hi, act = gm.get("guided_low"), gm.get("guided_high"), gm.get("actual")
            if lo and hi and act:
                mid = (lo + hi) / 2
                delta_pp = (act - mid) * 100
                gm_score = max(0, min(100, 50 + delta_pp / 10.0 * 50))
                gm_delta = round(delta_pp, 1)
        metrics = [s for s in [rev_score, gm_score] if s is not None]
        if not metrics:
            continue
        q_score = round(sum(metrics) / len(metrics))
        scored.append({
            "label":         q_label,
            "rev_delta_pct": rev_delta,
            "gm_delta_pp":   gm_delta,
            "quarter_score": q_score,
        })
    if not scored:
        return {"score": None, "n_quarters": 0, "quarters": scored}
    final_score = round(sum(q["quarter_score"] for q in scored) / len(scored))
    return {"score": final_score, "n_quarters": len(scored), "quarters": scored}


# ── Communication quality ──────────────────────────────────────────────────────

def _compute_communication_quality(guidance_analysis: dict,
                                   track_analysis: dict) -> dict:
    cred = track_analysis.get("credibility") if track_analysis else None
    if cred is None:
        return {"score": None, "tone_label": None, "credibility": None,
                "alignment": "insufficient data"}
    ga = guidance_analysis or {}
    tone = ga.get("tone", {})
    tone_label = tone.get("label", "Neutral")
    _MATRIX = {
        ("Confident", "high"):  (90, "Tone matches delivery — credible optimism"),
        ("Confident", "mid"):   (60, "Tone slightly ahead of delivery record"),
        ("Confident", "low"):   (20, "Tone materially misrepresents delivery record"),
        ("Neutral",   "high"):  (75, "Conservative framing, strong delivery record"),
        ("Neutral",   "mid"):   (65, "Tone and delivery aligned"),
        ("Neutral",   "low"):   (40, "Appropriately cautious given delivery record"),
        ("Cautious",  "high"):  (70, "Under-promises, over-delivers — positive signal"),
        ("Cautious",  "mid"):   (55, "Tone and delivery roughly matched"),
        ("Cautious",  "low"):   (60, "Cautious tone consistent with delivery record"),
    }
    cred_band = "high" if cred >= 70 else "mid" if cred >= 40 else "low"
    score, alignment = _MATRIX.get((tone_label, cred_band), (50, "Insufficient data"))
    return {
        "score":       score,
        "tone_label":  tone_label,
        "credibility": cred,
        "cred_band":   cred_band,
        "alignment":   alignment,
    }


# ── Composite management quality score ────────────────────────────────────────

def _compute_management_quality(track_analysis: dict,
                                execution_quality: dict,
                                communication_quality: dict) -> dict:
    """
    Composite Management Quality Score (0–100).

    Weights: Guidance Accuracy 40%, Execution Quality 40%, Communication 20%.
    If one or more components are None (insufficient data), weights are
    renormalised across available components so partial scores remain meaningful.

    Returns dict with keys:
        score          int|None
        components     {accuracy, execution, communication} — each with score/weight
        available_pct  float — proportion of full weighting represented
    """
    cred  = track_analysis.get("credibility")   if track_analysis    else None
    eq    = execution_quality.get("score")      if execution_quality else None
    cq    = communication_quality.get("score")  if communication_quality else None

    components = {
        "accuracy":      {"score": cred, "base_weight": 0.40, "label": "Guidance Accuracy"},
        "execution":     {"score": eq,   "base_weight": 0.40, "label": "Execution Quality"},
        "communication": {"score": cq,   "base_weight": 0.20, "label": "Communication"},
    }

    available = {k: v for k, v in components.items() if v["score"] is not None}
    if not available:
        return {"score": None, "components": components, "available_pct": 0.0}

    total_weight = sum(v["base_weight"] for v in available.values())
    composite = sum(
        v["score"] * (v["base_weight"] / total_weight)
        for v in available.values()
    )

    return {
        "score":         round(composite),
        "components":    components,
        "available_pct": round(total_weight, 2),
    }


# ── Tone credibility adjustment ───────────────────────────────────────────────

def _apply_tone_credibility_adjustment(
    guidance_analysis: dict,
    track_analysis: dict,
    fundamental: dict,
    commentary: dict,
) -> dict:
    """
    Adjusts management tone based on GuidanceTracker credibility score and
    fundamental metric deterioration. Two effects:

    1. Credibility penalty — if management has a miss history, downgrade
       Confident tone toward Neutral; boost Cautious weight when score < 40.

    2. Contrast note — if tone is Confident but key metrics are deteriorating,
       add a note to Trend Commentary flagging the divergence.

    Returns modified guidance_analysis dict (tone field updated in-place copy).
    """
    import copy
    ga = copy.deepcopy(guidance_analysis)
    tone = ga.get("tone", {})
    tone_label = tone.get("label", "N/A")
    tone_score = tone.get("score", 0.0)

    cred  = track_analysis.get("credibility") if track_analysis else None
    trend = track_analysis.get("trend", "insufficient data")

    # ── 1. Credibility penalty ────────────────────────────────────────────────
    # Only applies when: tone is Confident AND credibility score is available
    # AND management has a deteriorating or low credibility history.
    #
    # Penalty scale:
    #   credibility 40-59 (below average) → label stays, score penalised -0.15
    #   credibility 20-39 (poor)          → downgrade to Neutral
    #   credibility <20  (serial misser)  → downgrade to Cautious
    #   trend=deteriorating adds -0.10 additional penalty on top

    adjusted_label = tone_label
    adjusted_score = tone_score
    penalty_note   = None

    if tone_label == "Confident" and cred is not None:
        trend_penalty = 0.10 if trend == "deteriorating" else 0.0

        if cred < 20:
            adjusted_label = "Cautious"
            adjusted_score = max(0.0, tone_score - 0.40 - trend_penalty)
            penalty_note   = (
                f"Tone downgraded Confident→Cautious: "
                f"credibility score {cred}/100 (serial guidance misses)"
            )
        elif cred < 40:
            adjusted_label = "Neutral"
            adjusted_score = max(0.0, tone_score - 0.25 - trend_penalty)
            penalty_note   = (
                f"Tone downgraded Confident→Neutral: "
                f"credibility score {cred}/100 (consistent guidance misses)"
            )
        elif cred < 60:
            adjusted_score = max(0.0, tone_score - 0.15 - trend_penalty)
            penalty_note   = (
                f"Credibility score {cred}/100 — "
                f"Confident tone partially discounted"
            )
        elif trend == "deteriorating":
            adjusted_score = max(0.0, tone_score - 0.10)
            penalty_note   = (
                f"Guidance accuracy deteriorating — "
                f"Confident tone partially discounted"
            )

    if penalty_note:
        tone["label"]  = adjusted_label
        tone["score"]  = round(adjusted_score, 4)
        tone["signals"] = tone.get("signals", []) + [f"adj:{penalty_note[:40]}"]
        tone["credibility_adjusted"] = True
        tone["credibility_note"]     = penalty_note
        ga["tone"] = tone

    # ── 2. Contrast note ──────────────────────────────────────────────────────
    # Fires when tone is Confident (raw or adjusted) but metrics deteriorating.
    # Added to commentary narrative so it appears in Section 4.

    if tone_label == "Confident":   # use raw label for contrast check
        mr = fundamental.get("periods", [None])[0]
        if mr:
            # Check for deteriorating signals
            rev_cagr_str = fundamental.get("revenue_cagr", "")
            op_margin_mr = fundamental.get("operating_margin", {}).get(mr, "")
            op_margin_pr = fundamental.get("operating_margin", {}).get(
                fundamental.get("periods", [None, None])[1], ""
            ) if len(fundamental.get("periods", [])) > 1 else ""
            fcf_margin_mr = fundamental.get("fcf_margin", {}).get(mr, "")

            contrast_signals = []

            # Revenue declining
            try:
                cagr_v = float(str(rev_cagr_str).replace("%","").strip())
                if cagr_v < 0:
                    contrast_signals.append(
                        f"revenue 2-yr CAGR {rev_cagr_str}"
                    )
            except (ValueError, TypeError):
                pass

            # Operating margin compression >4pp YoY
            try:
                from core.agents import _parse_pct
                om_mr_f = _parse_pct(op_margin_mr)
                om_pr_f = _parse_pct(op_margin_pr)
                if om_mr_f is not None and om_pr_f is not None:
                    if (om_pr_f - om_mr_f) * 100 > 4:
                        contrast_signals.append(
                            f"operating margin {op_margin_pr}→{op_margin_mr}"
                        )
            except Exception:
                pass

            # FCF margin below 5%
            try:
                from core.agents import _parse_pct
                fcf_f = _parse_pct(fcf_margin_mr)
                if fcf_f is not None and fcf_f < 0.05:
                    contrast_signals.append(
                        f"FCF margin {fcf_margin_mr}"
                    )
            except Exception:
                pass

            if contrast_signals:
                cred_str = f" (credibility {cred}/100)" if cred is not None else ""
                contrast = (
                    f"Management tone Confident{cred_str} — "
                    f"note contrast with: {', '.join(contrast_signals)}. "
                    f"Monitor guidance reliability."
                )
                commentary.setdefault("narrative", []).append(contrast)
                commentary.setdefault("flags", []).append(
                    f"Tone/fundamentals divergence — "
                    f"Confident tone vs {'; '.join(contrast_signals)}"
                )

    return ga


class EquityAnalystOrchestrator:

    def __init__(self,
                 edgar_identity: str = "Research research@example.com",
                 nic_xml_path: str = None,
                 bhcf_dir: str = None):
        """
        Parameters
        ----------
        edgar_identity : SEC EDGAR identity string (name + email).
        nic_xml_path   : Path to XML_ATTRIBUTES_ACTIVE.XML for dynamic RSSD
                         lookup. Enables Basel III for any US BHC beyond the
                         hardcoded JPM/BAC/WFC/GS fallback.
        bhcf_dir       : Directory containing manually-downloaded BHCF zip files
                         from https://www.ffiec.gov/npw/FinancialReport/FinancialDataDownload
                         Files should be named BHCF{YY}{MM}.zip (e.g. BHCF2512.zip).
                         Required to populate Basel III capital ratios in the PDF.
        """
        edgar.set_identity(edgar_identity)
        self._edgar_identity = edgar_identity
        _xbrl_set_identity(edgar_identity)   # SEC XBRL API uses same identity
        _facts_set_identity(edgar_identity)  # company facts API uses same identity
        self._fundamental   = FundamentalAgent()
        self._risk          = RiskAgent()
        self._valuation     = ValuationAgent()
        self._commentary    = TrendCommentaryAgent()
        self._guidance      = GuidanceAgent()
        self._transcript    = TranscriptLoader()
        self._guidance_tracker = GuidanceTracker()
        self._edgar_transcript = EdgarTranscriptLoader(identity=edgar_identity)
        self._renderer      = EquityBriefRenderer()

        # Resolve paths relative to the project root (one level up from this script)
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _basel_dir = os.path.join(_root, "Industry Files", "Financials", "Basel_III")

        _nic_xml  = nic_xml_path  or os.path.join(_basel_dir, "XML_ATTRIBUTES_ACTIVE.XML")
        _bhcf_dir = bhcf_dir      or _basel_dir

        self._fry9c = FRY9CFetcher(
            nic_xml_path = _nic_xml  if os.path.exists(_nic_xml)  else None,
            bhcf_dir     = _bhcf_dir if os.path.isdir(_bhcf_dir)  else None,
        )
        self._ownership = OwnershipLoader(
            db_path=os.path.join(_root, "ownership_history.db")
        )
        self._insider = InsiderTransactionLoader(
            db_path=os.path.join(_root, "insider_transactions.db")
        )
        self._short_interest = ShortInterestLoader(
            db_path=os.path.join(_root, "short_interest_history.db")
        )
        self._peer_comparison = PeerComparisonLoader(
            peers_csv_path=os.path.join(_root, "peers.csv"),
            db_path=os.path.join(_root, "peer_metrics_cache.db"),
        )
        self._company_overview = CompanyOverviewLoader()
        self._analyst_targets  = AnalystTargetsLoader()
        self._price_context    = PriceContextLoader()
        self._estimate_revisions = EstimateRevisionsLoader()
        self._momentum = MomentumLoader()
        self._cache = FilingCache(cache_dir=_root)
        self._fred  = FredClient(api_key=FRED_API_KEY)

    def run(self,
            ticker: str,
            sector: str = "General",
            author: str  = "Research Team",
            output_dir: str = None,
            edgar_identity: str = None,
            skip_render: bool = False) -> str | None:
        """
        Full pipeline. Returns the path to the generated PDF, or None if skip_render=True.

        Parameters
        ----------
        ticker      : Stock ticker (e.g. "AAPL")
        sector      : Sector label for the cover page and future sector routing
        author      : Name printed on watermark and footer
        output_dir  : Directory to save the PDF.
                      Defaults to a "Reports" folder next to this script.
                      Created automatically if it does not exist.
        """

        print(f"\n{'='*60}")
        print(f"  EQUITY BRIEF PIPELINE — {ticker.upper()}")
        print(f"{'='*60}")

        # ── Auto-update transcript DB if current/previous month missing ──
        _db_maybe_update(verbose=True)

        # ── Step 1: Load financial data ──
        print(f"[1/5] Loading SEC filings and market data...")
        # processor = RobustDataProcessor(ticker, sector=sector, cache=self._cache)   # ← old: edgartools single-filing (3 yrs)
        # if not processor.load_data():                                                # ← old
        processor = FactsDataProcessor(ticker, sector=sector)
        if not processor.load_data(max_years=5):
            raise RuntimeError(f"Data ingestion failed for {ticker}. Check SEC availability.")

        profile = CompanyFinancialProfile(
            ticker     = ticker,
            sector     = sector,
            market_cap = processor.market_cap,
            financials_payload = processor.financials,
        )

        if not profile.periods:
            raise RuntimeError(f"No financial periods parsed for {ticker}.")

        # ── Skip foreign private issuers (20-F filers) ───────────────────────
        # 20-F companies lack 10-Q/8-K coverage. Section 5 (Management Guidance)
        # and Section 7 (Guidance Tracker) will be empty. Skipping avoids
        # producing a misleading half-populated report.
        # TODO: add 6-K + 20-F MD&A support for foreign filer coverage.
        _, _, _cached_meta = self._cache.get_financials(ticker)
        _form_type          = (_cached_meta or {}).get("form_type", "")
        if _form_type == "20-F":
            raise RuntimeError(
                f"{ticker} files a 20-F (foreign private issuer). "
                f"20-F support is pending — skipping to avoid incomplete output. "
                f"Remove this check once 6-K/20-F guidance extraction is implemented."
            )

        print(f"    Periods: {profile.periods}")

        # ── Step 2: Fetch historical FY-end prices ──
        print(f"[2/5] Fetching historical FY-end prices...")
        _price_hit, _cached_prices = self._cache.get_prices(ticker, profile.periods)
        if _price_hit:
            historical_prices = _cached_prices
            print(f"    Prices: cache hit")
        else:
            historical_prices = self._fetch_fy_prices(ticker, profile.periods)
            self._cache.store_prices(ticker, profile.periods, historical_prices)
        current_price = self._fetch_current_price(ticker)
        print(f"    Current price: ${current_price:.2f}" if current_price else "    Current price: N/A")

        # ── Step 3: Fetch filings for MD&A / guidance extraction ──────────
        print(f"[3/5] Fetching filings for MD&A and guidance extraction...")
        guidance_filings = self._fetch_guidance_filings(ticker) # last 4 10-Qs + 10-K
        filing_obj       = guidance_filings[0] if guidance_filings else None

        # ── Step 3b: Fetch Basel III capital ratios from FR Y-9C (financials only) ──
        fr_y9c_data = None
        if ticker.upper() in RSSD_MAP:
            print(f"[3b/5] Fetching FR Y-9C Basel III capital ratios...")
            try:
                fr_y9c_data = self._fry9c.fetch(ticker.upper(), profile.periods)
                found = sum(1 for v in fr_y9c_data.values() if any(v.values()))
                print(f"    Basel III data found for {found}/{len(profile.periods)} periods")
            except Exception as e:
                print(f"    Warning: FR Y-9C fetch failed ({e}) — will use XBRL fallback")
                fr_y9c_data = None

        # ── Step 3c: Fetch most recent 8-K earnings press release for guidance ──
        print(f"[3c/5] Fetching latest 8-K earnings release for guidance...")
        _filing_date_str = str(filing_obj.filing_date) if filing_obj else "unknown"
        _narr_hit, earnings_text, _cached_8k_date = self._cache.get_narrative(
            ticker, _filing_date_str
        )
        if _narr_hit:
            print(f"    8-K narrative: cache hit ({len(earnings_text):,} chars)" if earnings_text
                  else "    8-K narrative: cache hit (none)")
        else:
            earnings_text = self._fetch_latest_8k_narrative(ticker)
            # Resolve 8-K date for storage
            _cached_8k_date = None
            try:
                _8k_filings = edgar.Company(ticker).get_filings(form="8-K")
                if _8k_filings:
                    _cached_8k_date = str(list(_8k_filings)[0].filing_date)
            except Exception:
                pass
            self._cache.store_narrative(ticker, _filing_date_str, "8-K",
                                        earnings_text, _cached_8k_date)
            if earnings_text:
                print(f"    8-K narrative: {len(earnings_text):,} chars")
            else:
                print(f"    8-K not found — will fall back to 10-K MD&A")

        # Clean HTML entities from earnings_text (8-K press release)
        if earnings_text:
            import re as _re
            for _ in range(2):
                earnings_text = _re.sub(r'&amp;', '&', earnings_text)
            earnings_text = _re.sub(r'&nbsp;', ' ', earnings_text)
            earnings_text = _re.sub(r'&quot;', '"', earnings_text)
            earnings_text = _re.sub(r'&#\d+;', ' ', earnings_text)
            earnings_text = _re.sub(r';(?=[\s,.()\'"\-]|$)', '', earnings_text)

        # ── Step 3c2: Fetch all guidance sources independently ───────────────
        print(f"[3c2/5] Fetching guidance sources (transcript + EDGAR exhibits)...")
        # Motley Fool transcript (earnings call)
        fool_result  = self._transcript.fetch(ticker)
        # EDGAR 99.1 (press release) + 99.2 (prepared remarks if filed)
        edgar_all    = self._edgar_transcript.fetch_all(ticker)
        ex99_1       = edgar_all.get("ex99_1", {})
        ex99_2       = edgar_all.get("ex99_2", {})
        print(f"    Fool transcript: {'ok — ' + fool_result['source'] if fool_result['available'] else 'unavailable'}")
        print(f"    EDGAR 99.1:      {'ok — ' + ex99_1['source'] if ex99_1.get('available') else 'unavailable'}")
        print(f"    EDGAR 99.2:      {'ok — ' + ex99_2['source'] if ex99_2.get('available') else 'unavailable'}")

        # ── Step 3d: Fetch institutional ownership (13-F / yfinance) ──
        print(f"[3d/5] Fetching institutional ownership data...")
        _shares = profile.income_statement.diluted_shares[0] if profile.periods else None
        ownership_data = self._ownership.fetch(ticker.upper(), shares_outstanding=_shares)
        if ownership_data:
            inst_pct = ownership_data.get("institutional_pct")
            pct_str  = f"{inst_pct*100:.1f}%" if inst_pct is not None else "N/A"
            print(f"    Institutional ownership: {pct_str}")
        else:
            print(f"    Ownership data unavailable — will show N/A")

        # ── Step 3e: Fetch insider activity (SEC Form 4) ──
        print(f"[3e/5] Fetching insider transaction data...")
        insider_data = self._insider.fetch(ticker.upper())
        if insider_data:
            print(f"    Insider activity: {insider_data['buy_count']} buyers, "
                  f"{insider_data['sell_count']} sellers "
                  f"(last {insider_data['lookback_days']} days)")
        else:
            print(f"    Insider activity unavailable — will show N/A")

        # ── Step 3f: Fetch short interest (yfinance, FINRA-sourced) ──
        print(f"[3f/5] Fetching short interest data...")
        short_interest_data = self._short_interest.fetch(ticker.upper())
        if short_interest_data:
            dtc = short_interest_data.get("days_to_cover")
            dtc_str = f"{dtc:.2f}x" if dtc is not None else "N/A"
            print(f"    Short interest: {dtc_str} days to cover "
                  f"(as of {short_interest_data.get('_as_of', 'unknown')})")
        else:
            print(f"    Short interest data unavailable — will show N/A")

        # ── Step 3g: Fetch peer comparison (yfinance, industry-derived) ──
        print(f"[3g/5] Fetching peer comparison data...")
        peer_comparison_data = self._peer_comparison.fetch(ticker.upper())
        if peer_comparison_data:
            print(f"    Peer comparison: {len(peer_comparison_data['peer_tickers'])} peers "
                  f"({peer_comparison_data['industry']}, source: "
                  f"{peer_comparison_data['peer_source']})")
        else:
            print(f"    Peer comparison data unavailable — will show N/A")

        # ── Step 3h: Fetch company overview (yfinance summary + best-effort segment revenue) ──
        print(f"[3h/5] Fetching company overview...")
        company_overview_data = self._company_overview.fetch(ticker.upper())
        if company_overview_data.get("summary"):
            print(f"    Business summary: {len(company_overview_data['summary'])} chars")
        else:
            print(f"    Business summary unavailable")
        if company_overview_data.get("segments"):
            print(f"    Segment revenue: {len(company_overview_data['segments'])} segments "
                  f"(experimental — verify before trusting)")
        else:
            print(f"    Segment revenue unavailable — will omit segment table")

        # ── Step 3i: Fetch analyst price targets (yfinance) ──
        print(f"[3i/5] Fetching analyst price targets...")
        analyst_targets_data = self._analyst_targets.fetch(ticker.upper())
        if analyst_targets_data:
            print(f"    Analyst targets: low={analyst_targets_data.get('low')} "
                  f"mean={analyst_targets_data.get('mean')} "
                  f"high={analyst_targets_data.get('high')}")
        else:
            print(f"    Analyst price targets unavailable — will show N/A")

        # ── Step 3j: Fetch price context (52w range, SMAs, volume) ──
        print(f"[3j/5] Fetching price context...")
        price_context_data = self._price_context.fetch(ticker.upper())
        if price_context_data:
            pos = price_context_data.get("position_pct")
            print(f"    Price context: ${price_context_data.get('current')} | "
                  f"52w low=${price_context_data.get('low_52w')} "
                  f"high=${price_context_data.get('high_52w')} | "
                  f"position {pos*100:.0f}% of range" if pos is not None else
                  f"    Price context: fetched")
        else:
            print(f"    Price context unavailable — will show N/A")

        # ── Step 3k: Fetch estimate revisions (yfinance earnings_trend) ──
        print(f"[3k/5] Fetching estimate revisions...")
        estimate_revisions_data = self._estimate_revisions.fetch(ticker.upper())
        if estimate_revisions_data:
            n = len(estimate_revisions_data.get("periods", {}))
            print(f"    Estimate revisions: {n} period(s) resolved")
        else:
            print(f"    Estimate revisions unavailable — will show N/A")

        # ── Step 3l: Fetch momentum / relative strength ──
        print(f"[3l/5] Fetching momentum & relative strength...")
        momentum_data = self._momentum.fetch(ticker.upper(), sector=sector)
        if momentum_data:
            p12    = momentum_data["periods"].get("12m", {})
            vs_spy = p12.get("vs_spy")
            rank   = p12.get("peer_rank")
            if vs_spy is not None and rank is not None:
                print(f"    Momentum: 12m vs SPY {vs_spy*100:+.1f}% | "
                      f"peer rank {rank:.0f}th ({len(momentum_data['_peers'])} peers)")
            else:
                print(f"    Momentum: fetched")
        else:
            print(f"    Momentum: unavailable")

        # ── Step 3e: Fetch 10-K debt note for credit quality extraction ──────
        print(f"[3e/5] Fetching 10-K debt note for credit rating extraction...")
        debt_data = fetch_debt_note(ticker, identity=self._edgar_identity)
        if debt_data:
            n_tranches = len(debt_data.get("tranches", []))
            total      = debt_data.get("total_debt_m")
            total_str  = f"  total=${total:,.0f}M" if total else ""
            print(f"    Debt note: {n_tranches} tranches parsed{total_str}")
        else:
            print(f"    Debt note: not found — debt schedule will show N/A")

        # ── Step 3f: Fetch FRED market data for credit spread ────────────────
        print(f"[3f/5] Fetching FRED risk-free rate and credit spreads...")
        _rf = self._fred.get_risk_free_rate()
        # We'll fetch the rating-specific OAS after rating extraction in RiskAgent.
        # Pass risk-free + full spread curve to RiskAgent so it can map internally.
        _all_spreads = self._fred.get_all_spreads()
        fred_data = {
            "risk_free":   _rf,
            "all_spreads": _all_spreads,
        }
        if _rf:
            print(f"    10Y UST: {_rf*100:.2f}%")
        else:
            print(f"    FRED: unavailable (check FRED_API_KEY in .env)")

        # ── Step 4: Run agents ──
        print(f"[4/5] Running analyst agents...")
        guidance_analysis = self._guidance.analyse(
            earnings_text=earnings_text,
            filing_obj=filing_obj,
            filing_objs=guidance_filings,
            fool_transcript=fool_result,
            ex99_1=ex99_1,
            ex99_2=ex99_2,
            filing_date=_cached_8k_date,
        )
        track_analysis = self._guidance_tracker.analyse(
            ticker, profile, self._transcript,
            cache=self._cache,
            earnings_text=earnings_text or "",
        )
        execution_quality     = _compute_execution_quality(track_analysis)
        communication_quality = _compute_communication_quality(
            guidance_analysis, track_analysis
        )
        management_quality    = _compute_management_quality(
            track_analysis, execution_quality, communication_quality
        )
        fundamental = self._fundamental.analyze(profile, fr_y9c_data=fr_y9c_data, ownership=ownership_data,
                                                insider_activity=insider_data, short_interest=short_interest_data)
        risk        = self._risk.assess(
            profile,
            market_cap     = processor.market_cap,
            debt_data      = debt_data,
            fred_data      = self._build_fred_data(fred_data),
        )
        self._last_risk = risk          # exposed for validation scripts
        valuation   = self._valuation.value(
            profile,
            historical_prices=historical_prices,
            current_price=current_price,
            fred_data=fred_data,
            fundamental=fundamental,
        )
        self._last_valuation = valuation   # exposed for validation scripts
        commentary  = self._commentary.narrate(
            profile, fundamental, risk, valuation,
            earnings_text=earnings_text,
        )

        print(f"    Narrative lines:    {len(commentary['narrative'])}")
        print(f"    Guidance sentences: {len(commentary['management_guidance'])}")
        print(f"    Red flags:          {len(commentary['flags'])}")

        # ── Step 4 diagnostic: full agent output to console ──────────────────
        _tone = guidance_analysis.get("tone", {})
        print("\n── Tone ──────────────────────────────────────────────")
        print(f"    Label : {_tone.get('label','N/A')}")
        print(f"    Score : {_tone.get('score', 0.0):.4f}")
        print(f"    Signals: {_tone.get('signals', [])}")

        print("\n── Trend Commentary ──────────────────────────────────")
        for line in commentary.get("narrative", []):
            print(f"    • {line}")

        print("\n── Red Flags ─────────────────────────────────────────")
        for flag in commentary.get("flags", []):
            print(f"    ■ {flag}")

        print("\n── Management Guidance ───────────────────────────────")
        _ga = guidance_analysis
        _src = _ga.get("fool_source") or _ga.get("source","N/A")
        print(f"    Source: {_src}")
        for cat, bullets in _ga.get("fool_cats", {}).items():
            if bullets:
                print(f"    [{cat}]")
                for b in bullets[:2]:
                    print(f"      – {b[:120]}")
        for cat, bullets in _ga.get("fool_backward_cats", {}).items():
            if bullets:
                print(f"    [actuals/{cat}]")
                for b in bullets[:2]:
                    print(f"      – {b[:120]}")

        print("\n── Valuation ─────────────────────────────────────────")
        _mr = fundamental["periods"][0] if fundamental.get("periods") else None
        if _mr:
            print(f"    P/E (current): {valuation['pe_current'].get(_mr,'N/A')}")
            print(f"    EV/EBITDA:     {valuation['ev_ebitda'].get(_mr,'N/A')}")
            print(f"    Valuation flags: {valuation.get('flags',[])}")
        print()

        # ── Step 4b: Credibility-adjusted tone ───────────────────────────────
        # When GuidanceTracker has miss history, penalise Confident tone score
        # and add a contrast note to Trend Commentary.
        guidance_analysis = _apply_tone_credibility_adjustment(
            guidance_analysis, track_analysis, fundamental, commentary
        )

        # ── Step 5: Render PDF ──
        if skip_render:
            print(f"[5/5] PDF rendering skipped (--no-pdf mode)")
            print(f"{'='*60}\n")
            return None

        print(f"[5/5] Rendering PDF...")
        import datetime
        # Default output: Reports/ folder at the project root
        if output_dir is None:
            output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Reports")
        os.makedirs(output_dir, exist_ok=True)
        filename    = f"{ticker.upper()}_equity_brief_{datetime.date.today()}.pdf"
        output_path = os.path.join(output_dir, filename)

        self._renderer.render(
            ticker            = ticker.upper(),
            sector            = sector,
            profile           = profile,
            fundamental       = fundamental,
            risk              = risk,
            valuation         = valuation,
            commentary        = commentary,
            guidance_analysis = guidance_analysis,
            track_analysis    = track_analysis,
            execution_quality     = execution_quality,
            communication_quality = communication_quality,
            management_quality    = management_quality,
            ownership         = ownership_data,
            insider_activity  = insider_data,
            short_interest    = short_interest_data,
            peer_comparison   = peer_comparison_data,
            company_overview  = company_overview_data,
            analyst_targets   = analyst_targets_data,
            price_context     = price_context_data,
            estimate_revisions = estimate_revisions_data,
            momentum          = momentum_data,
            author            = author,
            output_path       = output_path,
        )

        print(f"\n  PDF saved: {output_path}")
        print(f"{'='*60}\n")
        return output_path

    def run_data_only(self,
                      ticker: str,
                      sector: str = "General") -> dict:
        """
        Runs steps 1–4 of the pipeline (data + agents) without rendering a PDF.
        Returns a dict containing all agent outputs keyed by agent name.
        Used by the DataFrame validation batch to collect metrics without
        hitting the 50-PDF image cap.

        Returns
        -------
        {
            "ticker":      str,
            "sector":      str,
            "market_cap":  float | None,
            "periods":     list[str],
            "fundamental": dict,   # FundamentalAgent output
            "risk":        dict,   # RiskAgent output
            "valuation":   dict,   # ValuationAgent output
            "commentary":  dict,   # TrendCommentaryAgent output
            "guidance":    dict,   # GuidanceAgent output
        }
        Raises RuntimeError if data ingestion fails.
        """
        # ── Step 1: Load financial data ──────────────────────────────────────
        # processor = RobustDataProcessor(ticker, sector=sector, cache=self._cache)   # ← old: edgartools single-filing (3 yrs)
        # if not processor.load_data():                                                # ← old
        processor = FactsDataProcessor(ticker, sector=sector)
        if not processor.load_data(max_years=5):
            raise RuntimeError(f"Data ingestion failed for {ticker}.")

        profile = CompanyFinancialProfile(
            ticker             = ticker,
            sector             = sector,
            market_cap         = processor.market_cap,
            financials_payload = processor.financials,
        )
        if not profile.periods:
            raise RuntimeError(f"No financial periods parsed for {ticker}.")

        # ── Step 2: Fetch prices ──────────────────────────────────────────────
        _price_hit, _cached_prices = self._cache.get_prices(ticker, profile.periods)
        if _price_hit:
            historical_prices = _cached_prices
        else:
            historical_prices = self._fetch_fy_prices(ticker, profile.periods)
            self._cache.store_prices(ticker, profile.periods, historical_prices)
        current_price = self._fetch_current_price(ticker)

        # ── Step 3: Fetch filings ─────────────────────────────────────────────
        filing_obj       = guidance_filings[0] if guidance_filings else None
        _filing_date_str = str(filing_obj.filing_date) if filing_obj else "unknown"
        _narr_hit, earnings_text, _8k_date = self._cache.get_narrative(
            ticker, _filing_date_str
        )
        if not _narr_hit:
            earnings_text = self._fetch_latest_8k_narrative(ticker)
            _8k_date = None
            try:
                _8k_filings = edgar.Company(ticker).get_filings(form="8-K")
                if _8k_filings:
                    _8k_date = str(list(_8k_filings)[0].filing_date)
            except Exception:
                pass
            self._cache.store_narrative(ticker, _filing_date_str, "8-K",
                                        earnings_text, _8k_date)

        # ── Step 3c2: Fetch all guidance sources independently ───────────────
        fool_result = self._transcript.fetch(ticker)
        edgar_all   = self._edgar_transcript.fetch_all(ticker)
        ex99_1      = edgar_all.get("ex99_1", {})
        ex99_2      = edgar_all.get("ex99_2", {})

        # ── Step 4: Run agents ────────────────────────────────────────────────
        guidance_analysis = self._guidance.analyse(
            earnings_text=earnings_text,
            filing_obj=filing_obj,
            filing_objs=guidance_filings,
            fool_transcript=fool_result,
            ex99_1=ex99_1,
            ex99_2=ex99_2,
            filing_date=_8k_date,
        )
        _shares = profile.income_statement.diluted_shares[0] if profile.periods else None
        ownership_data = self._ownership.fetch(ticker.upper(), shares_outstanding=_shares)
        insider_data   = self._insider.fetch(ticker.upper())
        short_interest_data = self._short_interest.fetch(ticker.upper())
        peer_comparison_data = self._peer_comparison.fetch(ticker.upper())
        company_overview_data = self._company_overview.fetch(ticker.upper())
        analyst_targets_data  = self._analyst_targets.fetch(ticker.upper())
        price_context_data       = self._price_context.fetch(ticker.upper())
        estimate_revisions_data  = self._estimate_revisions.fetch(ticker.upper())
        fundamental = self._fundamental.analyze(profile, fr_y9c_data=None, ownership=ownership_data,
                                                insider_activity=insider_data, short_interest=short_interest_data)
        risk        = self._risk.assess(profile, market_cap=processor.market_cap)
        valuation   = self._valuation.value(
            profile,
            historical_prices=historical_prices,
            current_price=current_price,
        )
        commentary  = self._commentary.narrate(
            profile, fundamental, risk, valuation,
            filing_obj=filing_obj,
            earnings_text=earnings_text,
        )

        return {
            "ticker":      ticker.upper(),
            "sector":      sector,
            "market_cap":  processor.market_cap,
            "periods":     profile.periods,
            "fundamental": fundamental,
            "risk":        risk,
            "valuation":   valuation,
            "commentary":  commentary,
            "guidance":    guidance_analysis,
            "ownership":   ownership_data,
            "insider":     insider_data,
            "short_interest": short_interest_data,
            "peer_comparison": peer_comparison_data,
            "company_overview": company_overview_data,
            "analyst_targets":  analyst_targets_data,
            "price_context":    price_context_data,
            "estimate_revisions": estimate_revisions_data,
        }

    # ─────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────

    def _build_fred_data(self, fred_data: dict) -> dict:
        """
        Takes the raw fred_data dict (risk_free + all_spreads) from run()
        and resolves the rating-specific OAS after credit rating extraction.
        Returns a flat dict ready for RiskAgent: {risk_free, oas_spread, oas_label}.

        Note: RiskAgent calls _compute_credit_quality which extracts the rating
        first, then uses the rating to pick the right spread from all_spreads.
        We pass all_spreads through so RiskAgent can do the lookup internally.
        """
        return fred_data   # RiskAgent receives {risk_free, all_spreads} and maps internally

    def _fetch_fy_prices(self, ticker: str, periods: list) -> list:
        """
        Returns a list of closing prices at each FY-end date,
        same order and length as periods (most recent first).
        Falls back to None for any date that can't be fetched.
        """
        prices = []
        try:
            yfobj = yf.Ticker(ticker)
            hist  = yfobj.history(period="10y")
            hist.index = hist.index.tz_localize(None) if hist.index.tz else hist.index

            for period_str in periods:
                date_str = period_str[:10]  # "2025-09-27"
                try:
                    import pandas as pd
                    target = pd.Timestamp(date_str)
                    # Get the closest trading day at or before the FY-end
                    subset = hist[hist.index <= target]
                    if not subset.empty:
                        prices.append(float(subset["Close"].iloc[-1]))
                    else:
                        prices.append(None)
                except Exception:
                    prices.append(None)
        except Exception as e:
            print(f"    Warning: could not fetch historical prices ({e})")
            prices = [None] * len(periods)
        return prices

    def _fetch_current_price(self, ticker: str) -> float | None:
        try:
            info = yf.Ticker(ticker).info
            return info.get("currentPrice") or info.get("regularMarketPrice")
        except Exception:
            return None

    def _fetch_latest_filing(self, ticker: str):
        """
        Returns the most recent filing object (10-Q preferred, else 10-K/20-F).
        Used for cache key derivation and TrendCommentaryAgent MD&A extraction.
        Returns None gracefully if unavailable.
        """
        filings = self._fetch_guidance_filings(ticker)
        return filings[0] if filings else None

    def _fetch_guidance_filings(self, ticker: str) -> list:
        """
        Returns up to 4 most recent 10-Qs + latest 10-K / 20-F for guidance
        extraction. Most recent first. GuidanceAgent concatenates MD&A text
        from all of them, giving a full year of quarterly outlook language plus
        the annual strategic view.

        Returns an empty list if nothing is available.
        """
        results = []
        try:
            company = edgar.Company(ticker)

            # ── Last 4 10-Qs ─────────────────────────────────────────────────
            try:
                filings_q = company.get_filings(form="10-Q", amendments=False)
                for f in list(filings_q)[:8]:   # scan up to 8 to get 4 clean ones
                    if f.form == "10-Q":
                        results.append(f)
                    if len(results) >= 4:
                        break
            except Exception:
                pass

            # ── Latest 10-K / 20-F ───────────────────────────────────────────
            try:
                annual = company.latest("10-K")
            except Exception:
                try:
                    annual = company.latest("20-F")
                except Exception:
                    annual = None

            if annual is not None:
                # Reject amendments
                if "A" in str(annual.form) or "/" in str(annual.form):
                    filings_a = company.get_filings(
                        form=["10-K", "20-F"], amendments=False
                    )
                    for f in filings_a:
                        if f.form in ["10-K", "20-F"]:
                            annual = f
                            break
                    else:
                        annual = None
                if annual:
                    results.append(annual)

            dates = [str(f.filing_date) for f in results]
            print(f"    Filings for guidance: {dates}")
            return results

        except Exception as e:
            print(f"    Warning: could not fetch filings for guidance ({e})")
            return []

    def _fetch_latest_8k_narrative(self, ticker: str) -> str | None:
        """
        Fetches the most recent earnings press release (8-K Exhibit 99.1) from EDGAR.

        Banks file their quarterly earnings release as an 8-K within days of the
        earnings call. The exhibit contains the CFO/CEO narrative with actual
        guidance figures, NII outlook, capital deployment plans, and analyst Q&A
        excerpts — far more useful than 10-K MD&A boilerplate.

        Returns the plain text of the narrative, or None if unavailable.
        """
        try:
            company = edgar.Company(ticker)
            # Get recent 8-K filings — earnings releases are typically the first
            # 8-K filed after quarter-end
            filings = company.get_filings(form="8-K")
            if not filings:
                return None

            # Try the 3 most recent 8-Ks to find an earnings release
            # (not all 8-Ks are earnings releases — some are governance, dividends, etc.)
            for filing in list(filings)[:5]:
                try:
                    doc = filing.obj()
                    if doc is None:
                        continue

                    # Extract text from the filing
                    text = ""
                    if hasattr(doc, 'text'):
                        text = doc.text() if callable(doc.text) else str(doc.text)
                    elif hasattr(filing, 'text'):
                        text = filing.text() if callable(filing.text) else str(filing.text)

                    if not text:
                        continue

                    # Confirm it's a quarterly earnings release (not governance/dividend 8-K)
                    # Require BOTH a quarter/earnings keyword AND a minimum length to
                    # exclude short governance filings (dividends, director appointments, etc.)
                    text_lower = text.lower()
                    has_quarter = any(phrase in text_lower for phrase in [
                        'fourth quarter', 'third quarter', 'second quarter', 'first quarter',
                        'q4 ', 'q3 ', 'q2 ', 'q1 ',
                        'full-year results', 'full year results', 'annual results',
                        'quarterly results', 'earnings results',
                    ])
                    has_financials = any(phrase in text_lower for phrase in [
                        'net income', 'earnings per share', 'diluted eps',
                        'return on equity', 'return on tangible',
                        'net revenue', 'total revenue',
                    ])
                    # Must be substantial text (>3000 chars) and contain both signals
                    if not (has_quarter and has_financials and len(text) > 3000):
                        continue

                    # Return the first 25,000 chars — enough for the full narrative
                    # while avoiding appendix tables
                    return text[:25000]

                except Exception:
                    continue

            return None

        except Exception as e:
            print(f"    Warning: could not fetch 8-K narrative ({e})")
            return None

    # ── PDF stitching ─────────────────────────────────────────────────────────

    @staticmethod
    def stitch_pdfs(pdf_paths: list[str],
                    output_path: str,
                    add_bookmarks: bool = True) -> str:
        """
        Merge multiple equity brief PDFs into a single file.

        Parameters
        ----------
        pdf_paths    : ordered list of PDF file paths to merge
        output_path  : destination path for the stitched PDF
        add_bookmarks: if True, adds a named bookmark at each report's first page
                       using the ticker derived from the filename

        Returns
        -------
        output_path on success, raises on failure
        """
        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            try:
                from PyPDF2 import PdfWriter, PdfReader
            except ImportError:
                raise ImportError(
                    "PDF stitching requires pypdf or PyPDF2. "
                    "Install with: pip install pypdf"
                )

        writer = PdfWriter()

        for pdf_path in pdf_paths:
            if not os.path.exists(pdf_path):
                print(f"    Warning: skipping missing file {pdf_path}")
                continue

            reader = PdfReader(pdf_path)

            # Bookmark at the first page of each report
            if add_bookmarks:
                # Derive ticker from filename e.g. "JPM_equity_brief_2026-06-11.pd"\nfname   = os.path.basename(pdf_path)
                ticker  = fname.split("_")[0].upper()
                page_no = len(writer.pages)
                writer.add_outline_item(ticker, page_no)

            for page in reader.pages:
                writer.add_page(page)

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "wb") as f:
            writer.write(f)

        n = len([p for p in pdf_paths if os.path.exists(p)])
        print(f"  Stitched {n} reports → {output_path}")
        return output_path