"""
peer_comparator.py — Relative valuation vs. industry peers (yfinance)

Architecture
------------
Peer selection (in priority order):
  1. Auto-derive  : subject's `industryKey` (yfinance .info) →
                     yf.Industry(industryKey).top_companies — a real,
                     market-cap-ranked peer list, no manual scanning or
                     prebuilt cache required. Requires a reasonably recent
                     yfinance version; wrapped in try/except since this is
                     a newer yfinance API surface.
  2. CSV fallback : peers.csv (ticker,peer1,peer2,...) — used only when
                     auto-derive returns fewer than _MIN_PEERS, or fails
                     outright (old yfinance version, API error, ticker not
                     covered by Industry/Sector data).
  Both can contribute — if auto-derive returns some but not enough peers,
  CSV entries are appended (deduped) rather than discarded.

Metrics — IMPORTANT scope note
--------------------------------
ALL values in this module, including the SUBJECT company's own row, are
sourced from yfinance .info — not from this pipeline's own XBRL-derived
figures used elsewhere in the report (Section 1-3). This is a deliberate
choice for internal consistency *within the peer table*: comparing the
subject's rigorously-tagged XBRL P/E against peers' yfinance P/E would mix
two different computation methodologies in the same column. The trade-off
is that the subject's number in this table may differ slightly from the
subject's "official" number elsewhere in the report (e.g. different price
basis, different EBITDA definition). The renderer must footnote this
clearly — it is not a bug if the two numbers don't match exactly.

Metrics compared: trailing P/E, EV/EBITDA, P/B, ROE, revenue growth.

Cache : SQLite (peer_metrics_cache.db) — one row per (ticker, fetch_date),
        same-day reuse to avoid re-fetching the same names repeatedly
        across runs/tickers that share peers.

Returned dict — public interface
---------------------------------
{
    "industry":      "Semiconductors",
    "peer_tickers":  ["WDC", "STX", "SNDK", ...],
    "peer_source":   "auto" | "csv_fallback" | "auto+csv_fallback",
    "metrics": {
        "pe_trailing": {
            "subject": 26.11, "peer_median": 62.96, "percentile": 83.0,
            # ^ percentile is inverted for pe_trailing/ev_ebitda/pb — a
            # CHEAPER multiple than peers scores a HIGHER percentile, so
            # "higher percentile" means "more favorable" consistently
            # across every metric in this table (matches roe/rev_growth,
            # where high percentile already meant "better").
            "peer_values": {"WDC": 18.2, "STX": 21.4, ...},
        },
        "ev_ebitda":   {...},
        "pb":          {...},
        "roe":         {...},
        "rev_growth":  {...},
    },
    "_source": "yfinance .info (all names, including subject) — not this "
               "report's primary XBRL-sourced figures",
}
# Empty dict {} returned when no usable peer data found — never raises.
"""

import csv
import logging
import os
import pathlib
import sqlite3
import datetime
import json

logger = logging.getLogger(__name__)

_MIN_PEERS  = 3    # below this, auto-derive results get supplemented by CSV
_MAX_PEERS  = 6    # cap — keeps the table readable and limits yfinance calls

# Session-scoped peer validation results: {TICKER: (is_valid, reason)}.
# Peers recur constantly across a batch run (every semiconductor name shares
# most of its peer set), and validation costs a .info fetch plus a CIK
# lookup, so the answer is memoised for the life of the process. Deliberately
# NOT persisted to the SQLite cache: listing status and SEC-filer status can
# change between sessions, and a stale "invalid" would silently exclude a
# name for a whole day.
_PEER_VALIDATION_CACHE: dict[str, tuple[bool, str]] = {}


def _cik_lookup(ticker: str):
    """
    Resolve a ticker to an SEC CIK using the same path the main ingestion
    pipeline uses, so "is an SEC filer" means exactly what it means
    everywhere else in this report.

    No circular import: data.facts_processor imports only stdlib and
    third-party packages, and nothing under market/ is imported by it.

    Returns (cik_or_None, lookup_available). lookup_available is False when
    the CIK mapping itself could not be loaded (e.g. set_identity() was
    never called, as happens when this module is run standalone) -- in that
    case the caller must NOT treat a None CIK as "not a filer", or every
    peer would be dropped for an environmental reason.
    """
    try:
        from data.facts_processor import _get_cik, _cik_cache, _load_cik_mapping
    except Exception as e:
        logger.debug("PeerComparisonLoader: CIK lookup unavailable — %s", e)
        return None, False

    try:
        cik = _get_cik(ticker)
    except Exception as e:
        logger.debug("PeerComparisonLoader: CIK lookup failed for %s — %s", ticker, e)
        return None, False

    # _get_cik populates the module-level cache on first call; an empty cache
    # afterwards means the mapping never loaded, not that the ticker is absent.
    if not _cik_cache:
        return None, False
    return cik, True


def _has_usgaap_facts(cik: str) -> tuple[bool, bool]:
    """
    Does this CIK have US-GAAP XBRL financial data in the SEC company facts
    API? Returns (has_facts, check_available).

    Holding a CIK is not the same as having comparable financials. Foreign
    issuers registered with the SEC receive a CIK and a companyfacts entry,
    but file under IFRS via 20-F/6-K -- or file no XBRL financial data at
    all -- so their us-gaap section is empty. Confirmed live: SK hynix
    (CIK 0002120882) returns a valid facts blob with ZERO us-gaap concepts,
    where every genuine domestic peer returns 400-800.

    This is the gate that actually matches what the report needs, and it is
    the same bar the subject company must clear to be processed at all --
    the orchestrator already refuses 20-F filers outright.
    """
    try:
        from data.facts_processor import _get_facts
    except Exception:
        return False, False
    try:
        facts = _get_facts(cik)
    except Exception as e:
        logger.debug("PeerComparisonLoader: facts fetch failed for %s — %s", cik, e)
        return False, False
    if facts is None:
        # Distinguish "no such filing data" (a real negative) from a
        # transport failure, which _get_facts also reports as None. Treated
        # as a real negative: a CIK the facts API does not serve has nothing
        # to compare either way.
        return False, True
    return bool(facts.get("facts", {}).get("us-gaap")), True


def _safe_float(value, ticker: str = "", field: str = "") -> float | None:
    """
    Coerce a yfinance .info value to float. Some tickers (e.g. TSLA, KMX)
    intermittently return non-numeric placeholders ("Infinity", "N/A", "")
    for fields like trailingPE/enterpriseToEbitda instead of a float or
    None. Left uncoerced, a string like that reaches sorted()/comparisons
    alongside real floats in fetch() and raises TypeError. Returns None on
    any coercion failure instead of raising.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.debug("PeerComparisonLoader: %s field %s -- could not coerce "
                     "%r to float, dropping", ticker, field, value)
        return None

# yfinance .info key -> our metric name
_METRIC_KEYS = {
    "pe_trailing": "trailingPE",
    "ev_ebitda":   "enterpriseToEbitda",
    "pb":          "priceToBook",
    "roe":         "returnOnEquity",
    "rev_growth":  "revenueGrowth",
}

# Metrics where a LOWER raw value is the more attractive/favorable reading
# (cheaper multiple). Percentile is inverted for these so that, across the
# whole table, "higher percentile" always means "more favorable" — without
# this, a cheap P/E would show a LOW percentile (since percentile was
# tracking raw magnitude), which reads backwards next to ROE/Revenue
# Growth where high percentile already means "better."
_LOWER_IS_BETTER = {"pe_trailing", "ev_ebitda", "pb"}


class PeerComparisonLoader:
    """
    Fetches relative-valuation peer data for a ticker.

    Parameters
    ----------
    peers_csv_path : Path to peers.csv (ticker,peer1,peer2,...).
                      Defaults to peers.csv next to this file. Optional —
                      if missing, only auto-derive is used.
    db_path        : Path to peer_metrics_cache.db.
    max_peers      : Cap on peer count (default 6).
    """

    def __init__(self, peers_csv_path: str | None = None,
                 db_path: str | None = None, max_peers: int = _MAX_PEERS):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._csv_path  = peers_csv_path or os.path.join(here, "peers.csv")
        self._db_path   = db_path or os.path.join(here, "peer_metrics_cache.db")
        self._max_peers = max_peers
        self._csv_map   = self._load_csv()
        self._init_db()

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, ticker: str) -> dict:
        """
        Fetch peer comparison data for a ticker. Never raises.
        Returns {} when no usable peer data is found.
        """
        ticker = ticker.upper()
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("PeerComparisonLoader: yfinance not installed")
            return {}

        subject_metrics = self._get_metrics(ticker, yf)
        if not subject_metrics:
            logger.warning("PeerComparisonLoader: no .info data for subject %s", ticker)
            return {}

        peer_tickers, industry, source, all_auto = self._select_peers(
            ticker, subject_metrics, yf)
        if not peer_tickers:
            logger.warning("PeerComparisonLoader: no peers found for %s", ticker)
            return {}

        # ── Validate before comparing ────────────────────────────────────────
        # A single bogus member materially moves a six-name median and every
        # percentile derived from it, so candidates are filtered before any
        # metric is computed.
        n_derived = len(peer_tickers)
        peer_tickers, dropped = self._validate_peers(peer_tickers, yf, ticker)
        peer_basis = "peer_set"

        # Too few names left to take a meaningful median over. Rather than
        # reporting a "peer median" of one or two companies, widen to the
        # full industry constituent list and take the median across whatever
        # validates. yfinance exposes no direct industry-median endpoint, so
        # this is the industry median as computed from its own constituents.
        if len(peer_tickers) < _MIN_PEERS and all_auto:
            already = set(peer_tickers) | {p for p, _ in dropped}
            extra = [p for p in all_auto if p.upper() not in already][:20]
            if extra:
                more_valid, more_dropped = self._validate_peers(extra, yf, ticker)
                n_derived += len(extra)
                peer_tickers = peer_tickers + more_valid
                dropped += more_dropped
                if peer_tickers:
                    peer_basis = "industry_median"

        if len(peer_tickers) < _MIN_PEERS:
            logger.warning(
                "PeerComparisonLoader: only %d valid peer(s) for %s after "
                "validation — suppressing peer table rather than reporting a "
                "median over too few names", len(peer_tickers), ticker)
            return {}

        peer_metrics = {}
        for p in peer_tickers:
            m = self._get_metrics(p, yf)
            if m:
                peer_metrics[p] = m

        if not peer_metrics:
            logger.warning("PeerComparisonLoader: no peer .info data resolved for %s", ticker)
            return {}

        metrics_out = {}
        for name, key in _METRIC_KEYS.items():
            # Re-coerce even though _get_metrics() already does -- values
            # can arrive here from the same-day SQLite cache, which may
            # hold entries written before this coercion existed.
            subject_val = _safe_float(subject_metrics.get(key), ticker, key)
            peer_vals = {}
            for p, m in peer_metrics.items():
                v = _safe_float(m.get(key), p, key)
                if v is not None:
                    peer_vals[p] = v
            if subject_val is None or not peer_vals:
                continue
            values = list(peer_vals.values())
            median = sorted(values)[len(values) // 2] if len(values) % 2 else \
                     (sorted(values)[len(values)//2 - 1] + sorted(values)[len(values)//2]) / 2
            group = values + [subject_val]
            if name in _LOWER_IS_BETTER:
                # Lower raw value = more favorable = higher percentile.
                percentile = sum(1 for v in group if v >= subject_val) / len(group) * 100
            else:
                percentile = sum(1 for v in group if v <= subject_val) / len(group) * 100
            metrics_out[name] = {
                "subject":     subject_val,
                "peer_median": median,
                "percentile":  percentile,
                "peer_values": peer_vals,
            }

        if not metrics_out:
            return {}

        return {
            "industry":     industry,
            "peer_tickers": list(peer_metrics.keys()),
            "peer_source":  source,
            "peer_basis":   peer_basis,
            "n_derived":    n_derived,
            "n_valid":      len(peer_metrics),
            "dropped":      dropped,
            "metrics":      metrics_out,
            "_source": ("yfinance .info (all names, including subject) — "
                       "not this report's primary XBRL-sourced figures"),
        }

    # ── Peer selection ───────────────────────────────────────────────────────

    def _select_peers(self, ticker: str, subject_metrics: dict, yf) -> tuple[list, str, str, list]:
        """
        Returns (peer_tickers, industry_label, source_label, all_auto).

        all_auto is the UNCAPPED auto-derived industry constituent list,
        kept so the caller can widen the sample if validation leaves too
        few names to take a median over.
        """
        industry_key = subject_metrics.get("_industryKey", "")
        industry_label = subject_metrics.get("_industry", "") or industry_key

        auto_peers: list[str] = []
        if industry_key:
            try:
                ind = yf.Industry(industry_key)
                top = ind.top_companies
                if top is not None and not top.empty:
                    auto_peers = [t for t in top.index.tolist() if t.upper() != ticker]
            except Exception as e:
                logger.debug("PeerComparisonLoader: yf.Industry failed for %s (%s) — %s",
                            ticker, industry_key, e)

        csv_peers = self._csv_map.get(ticker, [])

        if len(auto_peers) >= _MIN_PEERS:
            peers = auto_peers[:self._max_peers]
            source = "auto"
        elif auto_peers or csv_peers:
            # Merge, dedup, preserve order: auto first, then CSV fill-in
            seen = set()
            merged = []
            for t in auto_peers + csv_peers:
                t = t.upper()
                if t != ticker and t not in seen:
                    seen.add(t)
                    merged.append(t)
            peers = merged[:self._max_peers]
            source = "auto+csv_fallback" if auto_peers else "csv_fallback"
        else:
            peers, source = [], "none"

        return peers, industry_label, source, auto_peers

    # ── Peer validation ──────────────────────────────────────────────────────

    def _validate_peer(self, peer: str, yf, subject: str = "") -> tuple[bool, str]:
        """
        Is this candidate a usable peer for THIS report's comparisons?

        Three gates, cheapest first:
          1. yfinance returns a non-empty .info with a usable marketCap --
             rules out symbols that don't resolve to a live listing at all.
          2. The symbol resolves to a CIK, i.e. it is registered with the SEC.
          3. That CIK actually has US-GAAP XBRL financial data.

        Gate 3 exists because gate 2 is not sufficient on its own: a foreign
        issuer can hold a CIK and still have no us-gaap facts (see
        _has_usgaap_facts). Together they exclude foreign listings that are
        genuine competitors -- SK hynix, TSMC's local line, Samsung. That is
        correct for this pipeline rather than merely convenient: every figure
        in the comparison is XBRL-derived from SEC filings, so such a name
        could never be computed on the same basis as the rest of the table.
        The reason is surfaced in the report footnote rather than the name
        vanishing silently.

        Results are memoised per session in _PEER_VALIDATION_CACHE.
        """
        peer = peer.upper()
        if peer in _PEER_VALIDATION_CACHE:
            return _PEER_VALIDATION_CACHE[peer]

        result = (True, "")
        metrics = self._get_metrics(peer, yf)

        if not metrics:
            result = (False, "no yfinance data")
        else:
            # Absent key (rather than None) means the row predates marketCap
            # being cached; don't fail a peer on a cache-vintage artifact.
            mcap = metrics.get("_marketCap", "__ABSENT__")
            if mcap is None or (isinstance(mcap, (int, float)) and mcap <= 0):
                result = (False, "no market cap — not a live listing")
            else:
                cik, available = _cik_lookup(peer)
                if available and not cik:
                    result = (False, "not an SEC filer (no CIK)")
                elif cik:
                    has_facts, checkable = _has_usgaap_facts(cik)
                    if checkable and not has_facts:
                        result = (False, "no US-GAAP XBRL data (foreign filer) — "
                                         "nothing to compare on the same basis")

        _PEER_VALIDATION_CACHE[peer] = result
        return result

    def _validate_peers(self, candidates: list, yf, subject: str) -> tuple[list, list]:
        """Returns (valid_peers, [(peer, reason), ...])."""
        valid, dropped = [], []
        for p in candidates:
            ok, reason = self._validate_peer(p, yf, subject)
            if ok:
                valid.append(p)
            else:
                dropped.append((p, reason))
                print(f"[{subject}] peer dropped: {p} — {reason}")
        return valid, dropped

    # ── yfinance .info fetch + cache ─────────────────────────────────────────

    def _get_metrics(self, ticker: str, yf) -> dict | None:
        cached = self._read_cache(ticker)
        if cached is not None:
            return cached

        try:
            info = yf.Ticker(ticker).info
        except Exception as e:
            logger.debug("PeerComparisonLoader: .info fetch failed for %s — %s", ticker, e)
            return None
        if not info:
            return None

        metrics = {key: _safe_float(info.get(key), ticker, key) for key in _METRIC_KEYS.values()}
        metrics["_industryKey"] = info.get("industryKey", "")
        metrics["_industry"]    = info.get("industry", "")
        # Cached alongside the metrics so peer validation reuses this same
        # .info fetch rather than issuing a second one per candidate.
        metrics["_marketCap"]   = _safe_float(info.get("marketCap"), ticker, "marketCap")
        # Only cache if at least one real metric resolved — avoids caching
        # a dead/delisted ticker's empty result and treating it as final.
        if any(metrics.get(k) is not None for k in _METRIC_KEYS.values()):
            self._write_cache(ticker, metrics)
        return metrics

    # ── SQLite cache (same-day reuse) ────────────────────────────────────────

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self._db_path) as con:
                con.execute("""
                    CREATE TABLE IF NOT EXISTS metrics_cache (
                        ticker     TEXT NOT NULL,
                        fetch_date TEXT NOT NULL,
                        metrics_json TEXT,
                        PRIMARY KEY (ticker, fetch_date)
                    )
                """)
                con.commit()
        except Exception as e:
            logger.warning("PeerComparisonLoader: DB init failed — %s", e)

    def _read_cache(self, ticker: str) -> dict | None:
        today = datetime.date.today().isoformat()
        try:
            with sqlite3.connect(self._db_path) as con:
                row = con.execute(
                    "SELECT metrics_json FROM metrics_cache WHERE ticker=? AND fetch_date=?",
                    (ticker, today)
                ).fetchone()
                return json.loads(row[0]) if row else None
        except Exception:
            return None

    def _write_cache(self, ticker: str, metrics: dict) -> None:
        today = datetime.date.today().isoformat()
        try:
            with sqlite3.connect(self._db_path) as con:
                con.execute(
                    "INSERT OR REPLACE INTO metrics_cache (ticker, fetch_date, metrics_json) "
                    "VALUES (?, ?, ?)",
                    (ticker, today, json.dumps(metrics))
                )
                con.commit()
        except Exception as e:
            logger.warning("PeerComparisonLoader: cache write failed for %s — %s", ticker, e)

    # ── peers.csv ─────────────────────────────────────────────────────────────

    def _load_csv(self) -> dict[str, list[str]]:
        """
        Loads peers.csv: ticker,peer1,peer2,peer3,...
        Optional file — returns {} if missing (auto-derive only).
        """
        path = pathlib.Path(self._csv_path)
        result: dict[str, list[str]] = {}
        if not path.exists():
            return result
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    if not row or not row[0].strip():
                        continue
                    if row[0].strip().startswith("#"):
                        continue
                    ticker = row[0].strip().upper()
                    peers  = [p.strip().upper() for p in row[1:] if p.strip()]
                    if peers:
                        result[ticker] = peers
        except Exception as e:
            logger.warning("PeerComparisonLoader: peers.csv load error — %s", e)
        return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MU"

    loader = PeerComparisonLoader()
    result = loader.fetch(ticker)
    if result:
        print(f"\n{ticker} — peer comparison ({result['industry']}, "
              f"source: {result['peer_source']})")
        print(f"Peers: {', '.join(result['peer_tickers'])}\n")
        for name, m in result["metrics"].items():
            print(f"{name:<14} subject={m['subject']:.2f}  "
                  f"peer_median={m['peer_median']:.2f}  "
                  f"percentile={m['percentile']:.0f}")
    else:
        print(f"No peer comparison data found for {ticker}.")
