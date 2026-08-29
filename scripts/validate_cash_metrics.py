"""
validate_cash_metrics.py -- Validate FCF/CFO/CapEx/Net Income/EV-derived
metrics against yfinance consensus, for the S&P 500 (or MAG7, or a single
ticker) universe.

CRITICAL RULE -- source tracking
---------------------------------
A metric resolved via this pipeline's yfinance last-resort fallback (see
core.orchestrator._resolve_cash_metrics_and_sources and the three fallback
functions it documents in core/agents.py) is EXCLUDED from the accuracy
comparison against yfinance consensus -- diffing a yfinance-sourced
pipeline value against yfinance-sourced consensus is circular and would
produce a false ✅. Those tickers/metrics are reported separately as SKIP,
not folded into the match statistics.

Reuses core.orchestrator.EquityAnalystOrchestrator.run_data_only(), which
now returns "cash_metrics" (raw dollar/count values) and "data_sources"
(where each came from: xbrl / yfinance / derived / none) alongside its
existing output.

Usage
-----
    python scripts/validate_cash_metrics.py                       # sp500 (ratings.csv), default
    python scripts/validate_cash_metrics.py --universe sp500
    python scripts/validate_cash_metrics.py --universe mag7
    python scripts/validate_cash_metrics.py --ticker AAPL
    python scripts/validate_cash_metrics.py --delay 3
    python scripts/validate_cash_metrics.py --xbrl-only

Output
------
Live progress table on stdout, a summary (match counts + MAE per metric,
❌ tickers, yfinance-fallback usage rates), and a full CSV report at
scripts/cash_metrics_validation_report.csv.
"""

import os
import sys
import csv
import time
import argparse

_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _PROJECT_ROOT)

import yfinance as yf

from core.orchestrator import EquityAnalystOrchestrator
from runners.main import TICKERS as MAG7_TICKERS, EDGAR_IDENTITY

_DEFAULT_RATINGS_CSV = os.path.join(_PROJECT_ROOT, "ratings.csv")
_REPORT_PATH         = os.path.join(_SCRIPT_DIR, "cash_metrics_validation_report.csv")

_METRICS = ["net_income", "cfo", "capex", "fcf", "ev_fcf", "ev_cfo", "fcf_ev", "cfo_ev", "cfo_share"]
_PP_METRICS = {"fcf_ev", "cfo_ev"}  # these are already percentages -- delta expressed in pp, not %-of-%


# ── Universe loading (same pattern as validate_gross_margin.py) ────────────────

def _load_tickers_from_csv(path: str) -> list[str]:
    tickers = []
    seen = set()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = (row.get("ticker") or "").strip().upper()
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)
    return tickers


def _resolve_universe(args) -> tuple[list[str], str]:
    if args.ticker:
        return [args.ticker.upper()], f"single ticker ({args.ticker.upper()})"
    if args.universe == "mag7":
        return list(MAG7_TICKERS.keys()), "mag7 (runners/main.py TICKERS)"
    return _load_tickers_from_csv(_DEFAULT_RATINGS_CSV), "sp500 (ratings.csv)"


# ── Comparison ──────────────────────────────────────────────────────────────────

def compare(pipe_val, yf_val, source: str, metric_name: str) -> dict:
    """
    Never raises. SKIP when source == 'yfinance' (circular vs. consensus).
    ✅ within ±2%, ⚠️ within ±5%, ❌ beyond -- except fcf_ev/cfo_ev, which
    are already percentages, so their own delta is expressed in percentage
    points (pp) rather than percent-of-a-percent.
    """
    if source == "yfinance":
        return {"delta": None, "match": "SKIP",
                "reason": "yfinance fallback -- circular comparison excluded"}
    # Pipeline returned an explanatory string (e.g. "N/A (neg FCF)" /
    # "N/A (neg CFO)" from ValuationAgent's EV/FCF, EV/CFO guard against
    # a negative-FCF/CFO multiple) rather than a number -- not comparable,
    # but distinct from a plain missing value, so surface the reason.
    if isinstance(pipe_val, str):
        return {"delta": None, "match": "SKIP",
                "reason": f"pipeline returned: {pipe_val}"}
    if pipe_val is None or yf_val is None:
        return {"delta": None, "match": "N/A", "reason": "missing"}
    try:
        if metric_name in _PP_METRICS:
            delta = pipe_val - yf_val
        else:
            if yf_val == 0:
                return {"delta": None, "match": "N/A", "reason": "yfinance value is zero"}
            delta = (pipe_val - yf_val) / abs(yf_val) * 100
    except Exception:
        return {"delta": None, "match": "N/A", "reason": "compare error"}

    ad = abs(delta)
    if ad <= 2:
        match = "✅"
    elif ad <= 5:
        match = "⚠️"
    else:
        match = "❌"
    return {"delta": delta, "match": match, "reason": ""}


def _note_for(metric: str, source: str, delta) -> str:
    if source == "yfinance":
        return "excluded: yfinance fallback"
    if delta is None:
        return ""
    if delta is not None and abs(delta) > 20:
        return "flag: large discrepancy -- manual review"
    if abs(delta) > 5 and "ev" in metric:
        return "investigate: possible lease liability definition gap"
    if abs(delta) > 5 and metric == "capex":
        return "investigate: possible E&P segment capex"
    if abs(delta) > 5 and metric == "net_income":
        return "investigate: NCI or discontinued ops"
    return ""


# ── Per-ticker test ──────────────────────────────────────────────────────────────

def _yfinance_consensus_cash_metrics(tk) -> dict:
    """
    Consensus net_income/cfo/capex/fcf/ev_*/cfo_share from yfinance. Split
    out of test_ticker() -- unmodified -- so scripts/validate_universe.py
    can call it on a Ticker object it already fetched, without a second
    yfinance round-trip for the same statements.
    """
    net_income_yf = cfo_yf = capex_yf = fcf_yf = None
    shares_yf = mktcap_yf = debt_yf = cash_yf = ev_yf = None
    ev_fcf_yf = ev_cfo_yf = fcf_ev_yf = cfo_ev_yf = cfo_share_yf = None
    try:
        financials = tk.financials
        cashflow   = tk.cashflow
        balance    = tk.balance_sheet
        info       = tk.info
        if financials is not None and not financials.empty:
            latest = financials.columns[0]
            net_income_yf = financials.loc["Net Income", latest] if "Net Income" in financials.index else None
        if cashflow is not None and not cashflow.empty:
            latest_cf = cashflow.columns[0]
            cfo_yf = cashflow.loc["Operating Cash Flow", latest_cf] if "Operating Cash Flow" in cashflow.index else None
            capex_yf = (abs(cashflow.loc["Capital Expenditure", latest_cf])
                       if "Capital Expenditure" in cashflow.index else None)
        if cfo_yf is not None and capex_yf is not None:
            fcf_yf = cfo_yf - capex_yf
        shares_yf = info.get("sharesOutstanding")
        mktcap_yf = info.get("marketCap")
        if balance is not None and not balance.empty:
            latest_bs = balance.columns[0]
            debt_yf = balance.loc["Total Debt", latest_bs] if "Total Debt" in balance.index else None
            cash_yf = balance.loc["Cash And Cash Equivalents", latest_bs] if "Cash And Cash Equivalents" in balance.index else None
        if mktcap_yf is not None:
            ev_yf = mktcap_yf + (debt_yf or 0) - (cash_yf or 0)

        ev_fcf_yf = ev_yf / fcf_yf if (fcf_yf and fcf_yf > 0 and ev_yf) else None
        ev_cfo_yf = ev_yf / cfo_yf if (cfo_yf and ev_yf) else None
        fcf_ev_yf = (fcf_yf / ev_yf * 100) if (fcf_yf is not None and ev_yf) else None
        cfo_ev_yf = (cfo_yf / ev_yf * 100) if (cfo_yf is not None and ev_yf) else None
        cfo_share_yf = (cfo_yf / shares_yf) if (cfo_yf is not None and shares_yf) else None
    except Exception:
        pass  # partial/missing yfinance data handled per-metric as "missing" below

    return {
        "net_income": net_income_yf, "cfo": cfo_yf, "capex": capex_yf, "fcf": fcf_yf,
        "ev_fcf": ev_fcf_yf, "ev_cfo": ev_cfo_yf, "fcf_ev": fcf_ev_yf,
        "cfo_ev": cfo_ev_yf, "cfo_share": cfo_share_yf,
    }


def evaluate_cash_metrics(ticker: str, sector: str, result: dict,
                          yf_consensus: dict, xbrl_only: bool) -> list | None:
    """
    Pure classification step: compares the pipeline's cash_metrics/valuation
    figures (already resolved, in `result` from run_data_only()) against
    already-fetched yfinance consensus values. Split out of test_ticker()
    -- unmodified below this point -- so a shared multi-check loader
    (scripts/validate_universe.py) can call it on data it already loaded,
    without a second run_data_only() or yfinance call. Returns None if
    --xbrl-only was set and any of the 10 tracked cash_metrics sources is
    'yfinance' (strictest mode -- drop the whole ticker rather than mix
    partially-fallback-sourced figures into the run).
    """
    cm = result.get("cash_metrics") or {}
    ds = result.get("data_sources") or {}
    valuation = result.get("valuation") or {}

    if xbrl_only and any(ds.get(k) == "yfinance" for k in ds):
        return None

    ev_fcf_pipe    = (valuation.get("ev_fcf") or {}).get("current")
    ev_cfo_pipe    = (valuation.get("ev_cfo") or {}).get("current")
    fcf_ev_pipe    = (valuation.get("fcf_ev") or {}).get("current")
    cfo_ev_pipe    = (valuation.get("cfo_ev") or {}).get("current")
    cfo_share_pipe = (valuation.get("cfo_per_share") or {}).get("current")

    net_income_yf = yf_consensus.get("net_income")
    cfo_yf        = yf_consensus.get("cfo")
    capex_yf      = yf_consensus.get("capex")
    fcf_yf        = yf_consensus.get("fcf")
    ev_fcf_yf     = yf_consensus.get("ev_fcf")
    ev_cfo_yf     = yf_consensus.get("ev_cfo")
    fcf_ev_yf     = yf_consensus.get("fcf_ev")
    cfo_ev_yf     = yf_consensus.get("cfo_ev")
    cfo_share_yf  = yf_consensus.get("cfo_share")

    rows_spec = [
        ("net_income", cm.get("net_income"), net_income_yf, ds.get("net_income", "none")),
        ("cfo",        cm.get("cfo"),        cfo_yf,         ds.get("cfo", "none")),
        ("capex",      cm.get("capex"),      capex_yf,       ds.get("capex", "none")),
        ("fcf",        cm.get("fcf"),        fcf_yf,         ds.get("fcf", "none")),
        # ev_fcf/ev_cfo/fcf_ev/cfo_ev depend only on EV (derived) + CFO/FCF
        # (xbrl-derived) -- never routed through the yfinance fallback --
        # so their source tracks EV's ('derived' unless unresolved).
        ("ev_fcf",     ev_fcf_pipe, ev_fcf_yf, ds.get("ev", "none")),
        ("ev_cfo",     ev_cfo_pipe, ev_cfo_yf, ds.get("ev", "none")),
        ("fcf_ev",     fcf_ev_pipe, fcf_ev_yf, ds.get("ev", "none")),
        ("cfo_ev",     cfo_ev_pipe, cfo_ev_yf, ds.get("ev", "none")),
        # cfo_share depends on diluted_shares -- if THAT fell back to
        # yfinance, cfo_share is circular too.
        ("cfo_share",  cfo_share_pipe, cfo_share_yf, ds.get("diluted_shares", "none")),
    ]

    out = []
    for metric, pipe_v, yf_v, source in rows_spec:
        c = compare(pipe_v, yf_v, source, metric)
        out.append({
            "ticker": ticker, "sector": sector or "", "metric": metric,
            "pipeline_val": pipe_v, "yfinance_val": yf_v,
            "delta_pct": round(c["delta"], 2) if c["delta"] is not None else None,
            "match": c["match"], "source": source,
            "notes": _note_for(metric, source, c["delta"]) or c["reason"],
        })
    return out


def test_ticker(orch: EquityAnalystOrchestrator, ticker: str, xbrl_only: bool) -> list | None:
    """
    Standalone entry point (used when this script runs on its own): fetches
    yfinance sector/consensus and runs the pipeline itself, then delegates
    the actual comparison to evaluate_cash_metrics().
    """
    # yfinance sector, same convention as validate_gross_margin.py -- the
    # pipeline needs the real sector passed in for correct sector routing
    # (an unrelated bug found/fixed earlier in this project's validation
    # work: passing sector=None silently disables sector-aware logic).
    try:
        sector = yf.Ticker(ticker).info.get("sector")
    except Exception:
        sector = None

    try:
        result = orch.run_data_only(ticker, sector=sector)
    except Exception as e:
        return [{
            "ticker": ticker, "sector": sector or "", "metric": m,
            "pipeline_val": None, "yfinance_val": None, "delta_pct": None,
            "match": "❌", "source": "none", "notes": f"pipeline error: {e}",
        } for m in _METRICS]

    yf_consensus = _yfinance_consensus_cash_metrics(yf.Ticker(ticker))
    return evaluate_cash_metrics(ticker, sector, result, yf_consensus, xbrl_only)


# ── Reporting ──────────────────────────────────────────────────────────────────

def _fmt_val(v, metric):
    if v is None:
        return "N/A"
    if isinstance(v, str):
        return v  # already formatted, e.g. "N/A (neg FCF)" / "N/A (neg CFO)"
    try:
        if metric in ("net_income", "cfo", "capex", "fcf"):
            return f"${v/1e9:.3f}B"
        if metric in ("ev_fcf", "ev_cfo"):
            return f"{v:.2f}x"
        if metric in ("fcf_ev", "cfo_ev"):
            return f"{v:.2f}%"
        if metric == "cfo_share":
            return f"${v:.2f}"
        return str(v)
    except (TypeError, ValueError):
        return str(v)


def _print_progress_row(ticker: str, rows: list) -> None:
    by_metric = {r["metric"]: r for r in rows}
    ni  = by_metric.get("net_income", {})
    cfo = by_metric.get("cfo", {})
    cpx = by_metric.get("capex", {})
    fcf = by_metric.get("fcf", {})
    evf = by_metric.get("ev_fcf", {})
    status = "OK" if all(r["match"] in ("✅", "⚠️", "SKIP") for r in rows) else "CHECK"
    # _fmt_val() already guards against strings/format errors internally
    # (never raises), but keep this call site defensive too -- a crash in
    # one ticker's progress print should never take down an sp500-sized run.
    try:
        evf_str = _fmt_val(evf.get("pipeline_val"), "ev_fcf")
    except Exception:
        evf_str = "ERR"
    print(f"{ticker:<8} {_fmt_val(ni.get('pipeline_val'),'net_income'):>12} "
         f"{_fmt_val(cfo.get('pipeline_val'),'cfo'):>12} "
         f"{_fmt_val(cpx.get('pipeline_val'),'capex'):>12} "
         f"{_fmt_val(fcf.get('pipeline_val'),'fcf'):>12} "
         f"{evf_str:>10}  {status}")


def _print_summary(all_rows: list) -> None:
    print("\n" + "=" * 90)
    print("VALIDATION SUMMARY (XBRL-sourced only)")
    print("=" * 90)
    print("Disclaimer: Metrics resolved via yfinance fallback are excluded from accuracy")
    print("statistics -- comparing yfinance against yfinance is circular and produces")
    print("misleading results.\n")

    header = f"{'Metric':<14}{'Tested':>8}{'✅≤2%':>8}{'⚠️2-5%':>8}{'❌>5%':>8}{'SKIP(yf)':>10}{'MAE':>10}"
    print(header)
    print("-" * 90)
    for metric in _METRICS:
        rows = [r for r in all_rows if r["metric"] == metric]
        skip = [r for r in rows if r["match"] == "SKIP"]
        tested = [r for r in rows if r["match"] in ("✅", "⚠️", "❌")]
        ok   = [r for r in tested if r["match"] == "✅"]
        warn = [r for r in tested if r["match"] == "⚠️"]
        bad  = [r for r in tested if r["match"] == "❌"]
        deltas = [abs(r["delta_pct"]) for r in tested if r["delta_pct"] is not None]
        mae = sum(deltas) / len(deltas) if deltas else None
        mae_str = f"{mae:.2f}{'pp' if metric in _PP_METRICS else '%'}" if mae is not None else "N/A"
        print(f"{metric:<14}{len(tested):>8}{len(ok):>8}{len(warn):>8}{len(bad):>8}{len(skip):>10}{mae_str:>10}")

    print("\n" + "=" * 90)
    print("TICKERS WITH ❌ (any metric >5% delta)")
    print("=" * 90)
    bad_rows = [r for r in all_rows if r["match"] == "❌"]
    if not bad_rows:
        print("  (none)")
    else:
        print(f"{'ticker':<8}{'metric':<14}{'pipeline':>14}{'yfinance':>14}{'delta':>10}  notes")
        for r in bad_rows:
            d_str = f"{r['delta_pct']:+.2f}{'pp' if r['metric'] in _PP_METRICS else '%'}" if r['delta_pct'] is not None else "N/A"
            print(f"{r['ticker']:<8}{r['metric']:<14}"
                 f"{_fmt_val(r['pipeline_val'], r['metric']):>14}"
                 f"{_fmt_val(r['yfinance_val'], r['metric']):>14}"
                 f"{d_str:>10}  {r['notes']}")

    print("\n" + "=" * 90)
    print("YFINANCE FALLBACK USAGE")
    print("=" * 90)
    tickers = sorted(set(r["ticker"] for r in all_rows))
    n = len(tickers)
    per_ticker_source = {}
    for r in all_rows:
        per_ticker_source.setdefault(r["ticker"], {})[r["metric"]] = r["source"]
    for metric in _METRICS:
        yf_count = sum(1 for t, srcs in per_ticker_source.items() if srcs.get(metric) == "yfinance")
        print(f"  {metric:<16} {yf_count:>4}/{n} tickers ({yf_count/n*100:.1f}%)" if n else f"  {metric}: N/A")


def _write_csv_report(all_rows: list) -> None:
    with open(_REPORT_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "sector", "metric", "pipeline_val", "yfinance_val",
                   "delta_pct", "match", "source", "notes"])
        for r in all_rows:
            w.writerow([r["ticker"], r["sector"], r["metric"],
                       r["pipeline_val"] if r["pipeline_val"] is not None else "",
                       r["yfinance_val"] if r["yfinance_val"] is not None else "",
                       r["delta_pct"] if r["delta_pct"] is not None else "",
                       r["match"], r["source"], r["notes"]])
    print(f"\nFull results saved to: {_REPORT_PATH}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Validate FCF/CFO/CapEx/Net Income/EV-derived metrics against yfinance consensus")
    parser.add_argument("--universe", choices=["sp500", "mag7"], default="sp500",
                        help="Ticker universe to test (default: sp500, from ratings.csv)")
    parser.add_argument("--ticker", default=None,
                        help="Single ticker to test -- overrides --universe")
    parser.add_argument("--delay", type=float, default=3,
                        help="Seconds to sleep between tickers (default: 3)")
    parser.add_argument("--xbrl-only", action="store_true",
                        help="Skip tickers where ANY of the 10 tracked metrics is yfinance-sourced (strictest mode)")
    args = parser.parse_args()

    start_time = time.time()
    orch = EquityAnalystOrchestrator(edgar_identity=EDGAR_IDENTITY)

    tickers, universe_label = _resolve_universe(args)
    n = len(tickers)

    print(f"Universe: {universe_label}  |  {n} ticker(s) to test\n")
    print(f"{'Ticker':<8} {'Net Income':>12} {'CFO':>12} {'CapEx':>12} {'FCF':>12} {'EV/FCF':>10}  Status")
    print("-" * 90)

    all_rows = []
    skipped = 0
    for idx, ticker in enumerate(tickers, 1):
        rows = test_ticker(orch, ticker, args.xbrl_only)
        if rows is None:
            skipped += 1
            print(f"{ticker:<8} skipped (--xbrl-only, yfinance fallback present)")
        else:
            all_rows.extend(rows)
            _print_progress_row(ticker, rows)
        if idx < n:
            time.sleep(args.delay)

    if skipped:
        print(f"\n({skipped} ticker(s) skipped via --xbrl-only, excluded from totals)")

    _print_summary(all_rows)
    _write_csv_report(all_rows)

    elapsed = time.time() - start_time
    mins, secs = divmod(int(elapsed), 60)
    print(f"Total runtime: {mins}m {secs}s")


if __name__ == "__main__":
    main()
