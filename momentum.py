"""
momentum.py — Relative strength / momentum score vs. SPY, sector ETF, and peers

Returns price returns over 1/3/6/12 months for the subject ticker, benchmarked
against SPY (market), the sector SPDR ETF, and ranked vs. industry peers.

Source: yfinance Ticker.history(period="5y") — daily close prices.
2y fetch ensures 12m (252 trading day) window always has headroom.
Peer list: yf.Industry(industryKey).top_companies — same approach as peer_comparator.py.

Period definitions (trading days):
    1m  = 21   3m  = 63   6m  = 126   12m = 252

Return formula: (close[-1] - close[-N]) / close[-N]  — arithmetic, not log.

Peer rank: percentile of subject return vs peers (subject excluded from peer list,
included in rank calculation). 100th = beat all peers. Pure ordinal rank, no weights.

Returned dict — public interface
----------------------------------
{
    "ticker": "MU",
    "sector": "Technology",
    "etf":    "XLK",          # None if sector not in map
    "periods": {
        "1m": {
            "stock_return":  0.142,
            "spy_return":    0.031,
            "etf_return":    0.048,   # None if no ETF
            "vs_spy":        0.111,
            "vs_etf":        0.094,   # None if no ETF
            "peer_rank":     83.3,    # None if <2 peers have data
            "peer_count":    6,
        },
        "3m":  { ... },
        "6m":  { ... },
        "12m": { ... },
    },
    "_peers":  ["NVDA", "AVGO", "AMD", "INTC", "TXN", "MRVL"],
    "_source": "yfinance history(period=5y)",
    "_as_of":  "2026-07-03",
}
# Empty dict {} on failure — never raises.
# Individual period fields are None when insufficient price history exists.
"""

import logging
import datetime

logger = logging.getLogger(__name__)

# Trading-day lookback windows
_PERIODS = {
    "1m":  21,
    "3m":  63,
    "6m":  126,
    "12m": 252,
    "3y":  756,
    "5y":  1260,
}

_MAX_PEERS = 6

# Sector string (from main.py / orchestrator) → SPDR ETF ticker
_SECTOR_ETF = {
    "Technology":             "XLK",
    "Healthcare":             "XLV",
    "Consumer Staples":       "XLP",
    "Consumer Discretionary": "XLY",
    "Financials":             "XLF",
    "Financial Technology":   "XLF",
    "Industrials":            "XLI",
    "Energy":                 "XLE",
    "Materials":              "XLB",
    "Utilities":              "XLU",
    "Communication Services": "XLC",
    "Real Estate":            "XLRE",
    "General":                None,
}


class MomentumLoader:
    """Fetches relative strength / momentum data for a ticker."""

    def fetch(self, ticker: str, sector: str = "General") -> dict:
        """Never raises. Returns {} when no usable price data found."""
        ticker = ticker.upper()
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("MomentumLoader: yfinance not installed")
            return {}

        etf = _SECTOR_ETF.get(sector)

        # ── Fetch price histories ─────────────────────────────────────────────
        tickers_to_fetch = [ticker, "SPY"] + ([etf] if etf else [])

        # Peers via yf.Industry
        peers, industry_key = self._get_peers(ticker, yf)
        tickers_to_fetch += peers

        # Single bulk download — one API call for all names
        try:
            raw = yf.download(
                tickers_to_fetch,
                period="5y",   # 5y ensures all windows including 3y/5y have headroom
                auto_adjust=True,
                progress=False,
            )
        except Exception as e:
            logger.warning("MomentumLoader: download failed for %s — %s", ticker, e)
            return {}

        # yf.download returns MultiIndex when >1 ticker, single-level when 1
        if raw is None or raw.empty:
            return {}

        # Extract close series per ticker
        def get_close(t):
            try:
                if hasattr(raw.columns, "levels"):   # MultiIndex
                    return raw["Close"][t].dropna()
                else:
                    return raw["Close"].dropna()
            except (KeyError, TypeError):
                return None

        stock_close = get_close(ticker)
        if stock_close is None or len(stock_close) < 21:
            logger.warning("MomentumLoader: insufficient price history for %s", ticker)
            return {}

        spy_close = get_close("SPY")
        etf_close = get_close(etf) if etf else None

        # ── Compute returns ───────────────────────────────────────────────────
        periods_out = {}
        for label, n_days in _PERIODS.items():
            stock_ret = _period_return(stock_close, n_days)
            spy_ret   = _period_return(spy_close,   n_days)
            etf_ret   = _period_return(etf_close,   n_days) if etf_close is not None else None

            # Peer returns for rank
            peer_rets = []
            for p in peers:
                pc = get_close(p)
                pr = _period_return(pc, n_days)
                if pr is not None:
                    peer_rets.append(pr)

            peer_rank = None
            if stock_ret is not None and len(peer_rets) >= 2:
                beats = sum(1 for r in peer_rets if stock_ret > r)
                peer_rank = round(beats / len(peer_rets) * 100, 1)

            periods_out[label] = {
                "stock_return": stock_ret,
                "spy_return":   spy_ret,
                "etf_return":   etf_ret,
                "vs_spy":       round(stock_ret - spy_ret, 4)
                                if stock_ret is not None and spy_ret is not None else None,
                "vs_etf":       round(stock_ret - etf_ret, 4)
                                if stock_ret is not None and etf_ret is not None else None,
                "peer_rank":    peer_rank,
                "peer_count":   len(peer_rets),
            }

        return {
            "ticker":  ticker,
            "sector":  sector,
            "etf":     etf,
            "periods": periods_out,
            "_peers":  peers,
            "_source": "yfinance history(period=5y)",
            "_as_of":  datetime.date.today().isoformat(),
        }

    # ── Peer fetch ────────────────────────────────────────────────────────────

    def _get_peers(self, ticker: str, yf) -> tuple[list[str], str]:
        """Returns (peer_ticker_list, industry_key). Never raises."""
        try:
            info = yf.Ticker(ticker).info
            industry_key = info.get("industryKey", "")
            if not industry_key:
                return [], ""
            ind = yf.Industry(industry_key)
            top = ind.top_companies
            if top is None or top.empty:
                return [], industry_key
            peers = [t for t in top.index.tolist() if t.upper() != ticker][:_MAX_PEERS]
            return peers, industry_key
        except Exception as e:
            logger.debug("MomentumLoader: peer fetch failed for %s — %s", ticker, e)
            return [], ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _period_return(close, n_days: int):
    """Return arithmetic return over last n_days trading days.
    For long windows (3y/5y): if full history unavailable, use earliest
    available price as start rather than returning None."""
    if close is None or len(close) < 2:
        return None
    try:
        # Use earliest available price when history shorter than requested window
        idx   = -n_days if len(close) >= n_days else 0
        start = float(close.iloc[idx])
        end   = float(close.iloc[-1])
        if start == 0:
            return None
        return round((end - start) / start, 4)
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MU"
    sector = sys.argv[2] if len(sys.argv) > 2 else "Technology"

    loader = MomentumLoader()
    result = loader.fetch(ticker, sector)
    if result:
        print(f"\n{ticker} — momentum ({result['sector']} / ETF: {result['etf']})")
        print(f"Peers: {', '.join(result['_peers'])}\n")
        print(f"{'Period':<6} {'Stock':>8} {'SPY':>8} {'ETF':>8} "
              f"{'vs SPY':>8} {'vs ETF':>8} {'PeerRank':>10}")
        print("─" * 66)
        for label, p in result["periods"].items():
            def fmt(v): return f"{v*100:+.1f}%" if v is not None else "N/A"
            rank = f"{p['peer_rank']:.0f}th" if p["peer_rank"] is not None else "N/A"
            print(f"{label:<6} {fmt(p['stock_return']):>8} {fmt(p['spy_return']):>8} "
                  f"{fmt(p['etf_return']):>8} {fmt(p['vs_spy']):>8} "
                  f"{fmt(p['vs_etf']):>8} {rank:>10}")
    else:
        print(f"No momentum data for {ticker}.")
