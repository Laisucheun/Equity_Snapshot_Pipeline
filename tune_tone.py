"""
tune_tone.py — Tone lexicon validation corpus builder (sector-aware, 2000 transcripts)

Stratified sampling across 9 GICS sectors. Scrapes SUMMARY_PROSE from
Motley Fool transcript DB, scores tone, outputs TSV for lexicon tuning.

Usage:
    python tune_tone.py                          # 2000 stratified transcripts
    python tune_tone.py --n 200                  # quick pilot
    python tune_tone.py --ticker AAPL MSFT LLY   # specific tickers
    python tune_tone.py --sector Technology Energy  # specific sectors
    python tune_tone.py --review                 # flag mismatches
    python tune_tone.py --resume                 # skip already-done URLs
    python tune_tone.py --output corpus.tsv      # custom output

Output TSV columns:
    ticker | sector | date | quarter | fiscal_year |
    tone_label | tone_score | signals | summary

Review mode adds: expected_tone | mismatch

Resume: reads <output>.checkpoint to skip completed URLs.

Runtime: 2000 × 2.0s ≈ 67 minutes. Safe to Ctrl-C and resume.
"""

import os
import re
import sys
import csv
import json
import time
import sqlite3
import argparse
import requests
import logging
from collections import Counter, defaultdict

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

_DB_PATH         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fool_transcripts.db")
_DEFAULT_N       = 2000
_REQUEST_DELAY   = 2.0
_REQUEST_TIMEOUT = 15
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Sector targets — stratified allocation across 9 GICS sectors ──────────────
# Proportions loosely reflect S&P 500 sector weights.
# Adjusted so sector-specific tone patterns are well-represented.
_SECTOR_TARGETS = {
    "Technology":             280,
    "Healthcare":             240,
    "Consumer Discretionary": 200,
    "Consumer Staples":       160,
    "Financials":             200,
    "Industrials":            200,
    "Energy":                 200,
    "Communication Services": 160,
    "Utilities":              120,
    "Materials":              120,
    "Real Estate":            120,
}
# Total = 2000

# ── Ticker → sector map (S&P 500 + common large-caps) ────────────────────────
# Covers the vast majority of transcripts in the Fool DB.
# Unknown tickers fall back to "Unknown" and are sampled from remainder budget.
_TICKER_SECTOR = {
    # Technology
    "AAPL":"Technology","MSFT":"Technology","NVDA":"Technology","AVGO":"Technology",
    "ORCL":"Technology","AMD":"Technology","QCOM":"Technology","TXN":"Technology",
    "MU":"Technology","AMAT":"Technology","LRCX":"Technology","KLAC":"Technology",
    "ADI":"Technology","MCHP":"Technology","CDNS":"Technology","SNPS":"Technology",
    "FTNT":"Technology","PANW":"Technology","CRWD":"Technology","ZBRA":"Technology",
    "HPQ":"Technology","HPE":"Technology","DELL":"Technology","STX":"Technology",
    "WDC":"Technology","NTAP":"Technology","JNPR":"Technology","CSCO":"Technology",
    "ACN":"Technology","IBM":"Technology","INTU":"Technology","NOW":"Technology",
    "ADBE":"Technology","CRM":"Technology","WDAY":"Technology","TEAM":"Technology",
    "ZM":"Technology","DDOG":"Technology","SNOW":"Technology","MDB":"Technology",
    "OKTA":"Technology","HUBS":"Technology","COUP":"Technology","VEEV":"Technology",
    "FISV":"Technology","FIS":"Technology","GPN":"Technology","PYPL":"Technology",
    "SQ":"Technology","MA":"Technology","V":"Technology","AXP":"Technology",
    "INTC":"Technology","AEHR":"Technology","SMCI":"Technology","MRVL":"Technology",
    "ON":"Technology","MPWR":"Technology","ENPH":"Technology","SEDG":"Technology",
    "BB":"Technology","SONY":"Technology","TSM":"Technology","ASML":"Technology",
    "SAP":"Technology","BWXT":"Technology",
    # Healthcare
    "LLY":"Healthcare","JNJ":"Healthcare","UNH":"Healthcare","ABBV":"Healthcare",
    "MRK":"Healthcare","TMO":"Healthcare","ABT":"Healthcare","DHR":"Healthcare",
    "BMY":"Healthcare","AMGN":"Healthcare","GILD":"Healthcare","BIIB":"Healthcare",
    "REGN":"Healthcare","VRTX":"Healthcare","MRNA":"Healthcare","BNTX":"Healthcare",
    "PFE":"Healthcare","AZN":"Healthcare","NVO":"Healthcare","RHHBY":"Healthcare",
    "CVS":"Healthcare","CI":"Healthcare","HUM":"Healthcare","ELV":"Healthcare",
    "MCK":"Healthcare","CAH":"Healthcare","ABC":"Healthcare","BDX":"Healthcare",
    "SYK":"Healthcare","ZBH":"Healthcare","EW":"Healthcare","ISRG":"Healthcare",
    "MDT":"Healthcare","BSX":"Healthcare","DXCM":"Healthcare","PODD":"Healthcare",
    "IDXX":"Healthcare","WAT":"Healthcare","IQV":"Healthcare","CRL":"Healthcare",
    "HOLX":"Healthcare","TECH":"Healthcare","MTD":"Healthcare","A":"Healthcare",
    # Consumer Discretionary
    "AMZN":"Consumer Discretionary","TSLA":"Consumer Discretionary",
    "HD":"Consumer Discretionary","MCD":"Consumer Discretionary",
    "NKE":"Consumer Discretionary","SBUX":"Consumer Discretionary",
    "LOW":"Consumer Discretionary","TJX":"Consumer Discretionary",
    "BKNG":"Consumer Discretionary","MAR":"Consumer Discretionary",
    "HLT":"Consumer Discretionary","GM":"Consumer Discretionary",
    "F":"Consumer Discretionary","RIVN":"Consumer Discretionary",
    "LCID":"Consumer Discretionary","CMG":"Consumer Discretionary",
    "YUM":"Consumer Discretionary","QSR":"Consumer Discretionary",
    "WEN":"Consumer Discretionary","DPZ":"Consumer Discretionary",
    "DKNG":"Consumer Discretionary","LVS":"Consumer Discretionary",
    "MGM":"Consumer Discretionary","WYNN":"Consumer Discretionary",
    "CCL":"Consumer Discretionary","RCL":"Consumer Discretionary",
    "NCLH":"Consumer Discretionary","DHI":"Consumer Discretionary",
    "LEN":"Consumer Discretionary","PHM":"Consumer Discretionary",
    "TOL":"Consumer Discretionary","NVR":"Consumer Discretionary",
    "ETSY":"Consumer Discretionary","EBAY":"Consumer Discretionary",
    "W":"Consumer Discretionary","RH":"Consumer Discretionary",
    "BBY":"Consumer Discretionary","TGT":"Consumer Discretionary",
    "DG":"Consumer Discretionary","DLTR":"Consumer Discretionary",
    # Consumer Staples
    "WMT":"Consumer Staples","COST":"Consumer Staples","PG":"Consumer Staples",
    "KO":"Consumer Staples","PEP":"Consumer Staples","PM":"Consumer Staples",
    "MO":"Consumer Staples","MDLZ":"Consumer Staples","KHC":"Consumer Staples",
    "GIS":"Consumer Staples","K":"Consumer Staples","CAG":"Consumer Staples",
    "SJM":"Consumer Staples","HSY":"Consumer Staples","MKC":"Consumer Staples",
    "CPB":"Consumer Staples","HRL":"Consumer Staples","CLX":"Consumer Staples",
    "CHD":"Consumer Staples","EL":"Consumer Staples","COTY":"Consumer Staples",
    "KR":"Consumer Staples","SFM":"Consumer Staples","ADM":"Consumer Staples",
    "BG":"Consumer Staples","INGR":"Consumer Staples","TAP":"Consumer Staples",
    "STZ":"Consumer Staples","BF.B":"Consumer Staples","DEO":"Consumer Staples",
    # Financials
    "JPM":"Financials","BAC":"Financials","WFC":"Financials","GS":"Financials",
    "MS":"Financials","C":"Financials","USB":"Financials","PNC":"Financials",
    "TFC":"Financials","COF":"Financials","DFS":"Financials","SYF":"Financials",
    "AIG":"Financials","MET":"Financials","PRU":"Financials","AFL":"Financials",
    "ALL":"Financials","PGR":"Financials","CB":"Financials","TRV":"Financials",
    "HIG":"Financials","WRB":"Financials","BRK.B":"Financials","BLK":"Financials",
    "SCHW":"Financials","IBKR":"Financials","HOOD":"Financials","ICE":"Financials",
    "CME":"Financials","NDAQ":"Financials","CBOE":"Financials","SPGI":"Financials",
    "MCO":"Financials","FDS":"Financials","MSCI":"Financials","BX":"Financials",
    "KKR":"Financials","APO":"Financials","CG":"Financials","ARES":"Financials",
    # Industrials
    "CAT":"Industrials","DE":"Industrials","HON":"Industrials","RTX":"Industrials",
    "LMT":"Industrials","GD":"Industrials","NOC":"Industrials","BA":"Industrials",
    "LHX":"Industrials","TDG":"Industrials","HEI":"Industrials","HEICO":"Industrials",
    "GE":"Industrials","GEV":"Industrials","EMR":"Industrials","ETN":"Industrials",
    "ROK":"Industrials","AME":"Industrials","PH":"Industrials","IR":"Industrials",
    "XYL":"Industrials","RXO":"Industrials","CTAS":"Industrials","RSG":"Industrials",
    "WM":"Industrials","VRSK":"Industrials","FAST":"Industrials","GWW":"Industrials",
    "MSC":"Industrials","WSO":"Industrials","FDX":"Industrials","UPS":"Industrials",
    "CHRW":"Industrials","EXPD":"Industrials","KEX":"Industrials","R":"Industrials",
    "URI":"Industrials","AGCO":"Industrials","PCAR":"Industrials","CMI":"Industrials",
    # Energy
    "XOM":"Energy","CVX":"Energy","COP":"Energy","EOG":"Energy","PXD":"Energy",
    "OXY":"Energy","DVN":"Energy","FANG":"Energy","MPC":"Energy","PSX":"Energy",
    "VLO":"Energy","HES":"Energy","APA":"Energy","HAL":"Energy","SLB":"Energy",
    "BKR":"Energy","NOV":"Energy","FTI":"Energy","CIVI":"Energy","PR":"Energy",
    "SM":"Energy","MTDR":"Energy","CRSO":"Energy","CHK":"Energy","AR":"Energy",
    "RRC":"Energy","EQT":"Energy","CNX":"Energy","LNG":"Energy","CQP":"Energy",
    "MPLX":"Energy","EPD":"Energy","ET":"Energy","KMI":"Energy","WMB":"Energy",
    # Communication Services
    "GOOG":"Communication Services","GOOGL":"Communication Services",
    "META":"Communication Services","NFLX":"Communication Services",
    "DIS":"Communication Services","CMCSA":"Communication Services",
    "T":"Communication Services","VZ":"Communication Services",
    "TMUS":"Communication Services","CHTR":"Communication Services",
    "PARA":"Communication Services","WBD":"Communication Services",
    "FOX":"Communication Services","FOXA":"Communication Services",
    "NWSA":"Communication Services","NYT":"Communication Services",
    "IAC":"Communication Services","MTCH":"Communication Services",
    "SNAP":"Communication Services","PINS":"Communication Services",
    "RDDT":"Communication Services","SPOT":"Communication Services",
    "LYV":"Communication Services","SIRI":"Communication Services",
    "LUMN":"Communication Services","ZAYO":"Communication Services",
    # Utilities
    "NEE":"Utilities","DUK":"Utilities","SO":"Utilities","D":"Utilities",
    "AEP":"Utilities","EXC":"Utilities","SRE":"Utilities","PCG":"Utilities",
    "XEL":"Utilities","ES":"Utilities","WEC":"Utilities","ED":"Utilities",
    "PPL":"Utilities","FE":"Utilities","ETR":"Utilities","EIX":"Utilities",
    "AES":"Utilities","NRG":"Utilities","VST":"Utilities","CEG":"Utilities",
    "AWK":"Utilities","WTRG":"Utilities","CWT":"Utilities","SJW":"Utilities",
    # Materials
    "LIN":"Materials","APD":"Materials","SHW":"Materials","ECL":"Materials",
    "FCX":"Materials","NEM":"Materials","GOLD":"Materials","AEM":"Materials",
    "WPM":"Materials","PAAS":"Materials","CRS":"Materials","ATI":"Materials",
    "AA":"Materials","CENX":"Materials","X":"Materials","CLF":"Materials",
    "NUE":"Materials","STLD":"Materials","RS":"Materials","CMC":"Materials",
    "MLM":"Materials","VMC":"Materials","CRH":"Materials","EXP":"Materials",
    "PKG":"Materials","IP":"Materials","WRK":"Materials","SEE":"Materials",
    # Real Estate
    "AMT":"Real Estate","PLD":"Real Estate","CCI":"Real Estate","EQIX":"Real Estate",
    "SPG":"Real Estate","O":"Real Estate","VICI":"Real Estate","WELL":"Real Estate",
    "DLR":"Real Estate","PSA":"Real Estate","EXR":"Real Estate","AVB":"Real Estate",
    "EQR":"Real Estate","MAA":"Real Estate","UDR":"Real Estate","CPT":"Real Estate",
    "ESS":"Real Estate","NNN":"Real Estate","STAG":"Real Estate","REXR":"Real Estate",
    "FR":"Real Estate","EGP":"Real Estate","TRNO":"Real Estate","LXP":"Real Estate",
}


def _infer_sector(ticker: str) -> str:
    """Look up sector from map; fall back to 'Unknown'."""
    return _TICKER_SECTOR.get(ticker.upper(), "Unknown")


# ── Tone scorer loader ─────────────────────────────────────────────────────────

def _load_scorer():
    import types
    mod = types.ModuleType("agents_tone")
    mod.__file__ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents.py")
    with open(mod.__file__) as f:
        src = f.read()
    cutoff = src.find("\ndef _split_sentences")
    exec(compile(src[:cutoff], mod.__file__, "exec"), mod.__dict__)
    return mod._score_tone


# ── HTML extraction ────────────────────────────────────────────────────────────

def _extract_summary(html: str) -> str | None:
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>",   " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"self\.__next_f\.push\(\[.{0,2000}?\]\)", " ", html, flags=re.DOTALL)

    text = re.sub(r"</p>",    "\n\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</li>",   "\n",   text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ",    text)

    for _ in range(3):
        text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    m = re.search(
        r"\bSummary\b\s*(.+?)(?=\bIndustry\s+Glossary\b"
        r"|\bFull\s+(?:Conference\s+Call\s+)?Transcript\b|\Z)",
        text, re.IGNORECASE | re.DOTALL
    )
    if not m:
        return None

    content = m.group(1).strip()
    if len(content) < 60 or re.search(r"__next|\\u00|push\(\[", content):
        return None

    paras = [p.strip() for p in re.split(r"\n[ \t]*\n", content) if p.strip()]
    if not paras:
        return None

    prose = paras[0]
    prose = re.sub(r"\s*\([A-Z]{1,5}\s*[+-]\d+\.?\d*%\)\s*", " ", prose).strip()
    if len(prose) > 800:
        prose = prose[:800].rsplit(".", 1)[0] + "."

    return prose if len(prose) > 60 else None


# ── Heuristic expected tone (sanity check only) ────────────────────────────────

def _heuristic_tone(prose: str) -> str:
    pl = prose.lower()
    pos = sum(1 for w in ["record", "strong", "growth", "accelerat", "outperform",
                           "exceed", "momentum", "confident", "robust", "deliver",
                           "expansion", "opportunity", "beat", "solid"]
              if w in pl)
    neg = sum(1 for w in ["decline", "headwind", "challeng", "pressur", "uncertain",
                           "tariff", "difficult", "miss", "loss", "weak", "soften",
                           "deteriorat", "concern", "volatile"]
              if w in pl)
    if pos > neg + 1:
        return "Confident"
    if neg > pos + 1:
        return "Cautious"
    return "Neutral"


# ── Stratified DB sampling ─────────────────────────────────────────────────────

def _stratified_sample(db_path: str, targets: dict, tickers: list | None,
                        sectors: list | None) -> list[dict]:
    """
    Sample transcripts stratified by sector.

    Strategy:
      1. Pull all records from DB (ticker, date, url)
      2. Assign sector via _infer_sector()
      3. For each sector, randomly sample up to target count
      4. Remaining budget fills from "Unknown" tickers
    """
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}\nRun: python fool_transcript_db.py --build")
        sys.exit(1)

    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        if tickers:
            placeholders = ",".join("?" * len(tickers))
            rows = con.execute(
                f"SELECT ticker, date, quarter, fiscal_year, url FROM transcripts "
                f"WHERE ticker IN ({placeholders}) ORDER BY date DESC",
                [t.upper() for t in tickers]
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT ticker, date, quarter, fiscal_year, url "
                "FROM transcripts ORDER BY RANDOM()"
            ).fetchall()

    # Group by sector
    by_sector: dict[str, list] = defaultdict(list)
    for r in rows:
        rec = dict(r)
        rec["sector"] = _infer_sector(rec["ticker"])
        by_sector[rec["sector"]].append(rec)

    # Print DB sector coverage
    print("\n  DB sector coverage:")
    for s in sorted(by_sector):
        print(f"    {s:<28s} {len(by_sector[s]):>6} transcripts")
    print()

    # Filter to requested sectors
    active_targets = {
        s: n for s, n in targets.items()
        if not sectors or s in sectors
    }

    sampled = []
    import random as _random

    for sector, target in active_targets.items():
        pool = by_sector.get(sector, [])
        _random.shuffle(pool)
        chosen = pool[:target]
        sampled.extend(chosen)
        print(f"  Sampled {len(chosen):>4} / {target} from {sector}")

    # Fill remainder from Unknown if not filtering by sector
    if not sectors:
        remaining = sum(active_targets.values()) - len(sampled)
        if remaining > 0 and by_sector.get("Unknown"):
            pool = by_sector["Unknown"]
            _random.shuffle(pool)
            extra = pool[:remaining]
            sampled.extend(extra)
            print(f"  Sampled {len(extra):>4} from Unknown (fill)")

    _random.shuffle(sampled)
    return sampled


# ── Checkpoint helpers ─────────────────────────────────────────────────────────

def _load_checkpoint(path: str) -> set:
    """Return set of already-processed URLs."""
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_checkpoint(path: str, done: set):
    with open(path, "w") as f:
        json.dump(list(done), f)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sector-aware tone corpus builder")
    parser.add_argument("--n",       type=int,   default=_DEFAULT_N)
    parser.add_argument("--ticker",  nargs="+",  help="Specific tickers only")
    parser.add_argument("--sector",  nargs="+",  help="Specific sectors only")
    parser.add_argument("--output",  default="tone_corpus.tsv")
    parser.add_argument("--review",  action="store_true", help="Flag heuristic mismatches")
    parser.add_argument("--resume",  action="store_true", help="Skip already-done URLs")
    parser.add_argument("--delay",   type=float, default=_REQUEST_DELAY)
    args = parser.parse_args()

    checkpoint_path = args.output + ".checkpoint"

    print(f"\nLoading tone scorer from agents.py...")
    try:
        _score_tone = _load_scorer()
        print("  Scorer loaded OK")
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    # Scale targets to --n
    total_target = sum(_SECTOR_TARGETS.values())
    scaled = {s: max(1, round(n / total_target * args.n))
              for s, n in _SECTOR_TARGETS.items()}

    records = _stratified_sample(_DB_PATH, scaled, args.ticker, args.sector)
    print(f"\nTotal queued: {len(records)} transcripts")
    eta_min = len(records) * args.delay / 60
    print(f"Estimated time: {eta_min:.0f} minutes at {args.delay}s delay\n")

    # Resume — skip completed URLs
    done_urls = set()
    existing_results = []
    if args.resume and os.path.exists(args.output):
        done_urls = _load_checkpoint(checkpoint_path)
        # Re-read existing TSV rows
        try:
            with open(args.output, newline="", encoding="utf-8") as f:
                existing_results = list(csv.DictReader(f, delimiter="\t"))
            print(f"  Resuming — {len(done_urls)} URLs already done, "
                  f"{len(existing_results)} rows loaded\n")
        except Exception:
            existing_results = []

    session  = requests.Session()
    results  = list(existing_results)
    ok = fail = skip = 0

    fields = ["ticker", "sector", "date", "quarter", "fiscal_year",
              "tone_label", "tone_score", "signals", "summary"]
    if args.review:
        fields += ["expected", "mismatch"]

    # Open TSV in append mode when resuming, write mode otherwise
    write_mode = "a" if (args.resume and existing_results) else "w"
    tsv_file = open(args.output, write_mode, newline="", encoding="utf-8")
    writer   = csv.DictWriter(tsv_file, fieldnames=fields,
                               delimiter="\t", extrasaction="ignore")
    if write_mode == "w":
        writer.writeheader()

    try:
        for i, rec in enumerate(records, 1):
            ticker = rec["ticker"]
            url    = rec["url"]

            if url in done_urls:
                continue

            date = rec["date"]
            sector = rec.get("sector", _infer_sector(ticker))
            q    = rec.get("quarter") or "?"
            fy   = rec.get("fiscal_year") or "?"

            print(f"  [{i:>4}/{len(records)}] {ticker:6s} {sector[:22]:22s} {date}  ",
                  end="", flush=True)

            try:
                resp = session.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
                if resp.status_code == 403:
                    print("403 — rate limited, pausing 30s")
                    time.sleep(30)
                    fail += 1
                    continue
                if resp.status_code != 200:
                    print(f"HTTP {resp.status_code}")
                    fail += 1
                    time.sleep(args.delay)
                    continue

                prose = _extract_summary(resp.text)
                if not prose:
                    print("no summary")
                    skip += 1
                    done_urls.add(url)
                    time.sleep(args.delay)
                    continue

                tone_label, tone_score, signals = _score_tone(prose)
                expected = _heuristic_tone(prose) if args.review else ""
                mismatch = (expected and expected != tone_label) if args.review else False

                row = {
                    "ticker":      ticker,
                    "sector":      sector,
                    "date":        date,
                    "quarter":     q,
                    "fiscal_year": fy,
                    "tone_label":  tone_label,
                    "tone_score":  f"{tone_score:+.3f}",
                    "signals":     ", ".join(signals),
                    "summary":     prose[:300].replace("\t", " "),
                    "expected":    expected,
                    "mismatch":    "YES" if mismatch else "",
                }
                writer.writerow(row)
                tsv_file.flush()
                results.append(row)
                done_urls.add(url)
                ok += 1

                flag = " ← MISMATCH" if mismatch else ""
                print(f"{tone_label:10s} ({tone_score:+.3f})  {signals}{flag}")

            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"ERROR: {e}")
                fail += 1

            time.sleep(args.delay)

    except KeyboardInterrupt:
        print("\n\n  Interrupted — saving checkpoint...")
    finally:
        tsv_file.close()
        _save_checkpoint(checkpoint_path, done_urls)

    # ── Summary ───────────────────────────────────────────────────────────────
    total = len([r for r in results if r.get("tone_label")])
    print(f"\n{'═'*65}")
    print(f"  Done — {ok} new  |  {skip} no summary  |  {fail} errors  "
          f"|  {total} total in TSV")
    print(f"  Output  → {args.output}")
    print(f"  Resume  → python tune_tone.py --resume --output {args.output}")

    if not results:
        print("═"*65)
        return

    # Overall tone distribution
    scored = [r for r in results if r.get("tone_label")]
    conf  = sum(1 for r in scored if r["tone_label"] == "Confident")
    neut  = sum(1 for r in scored if r["tone_label"] == "Neutral")
    caut  = sum(1 for r in scored if r["tone_label"] == "Cautious")
    n     = len(scored)

    print(f"\n  Overall tone distribution ({n} transcripts):")
    print(f"    Confident : {conf:>5}  ({conf/n*100:.1f}%)")
    print(f"    Neutral   : {neut:>5}  ({neut/n*100:.1f}%)")
    print(f"    Cautious  : {caut:>5}  ({caut/n*100:.1f}%)")

    # Per-sector breakdown
    by_sector = defaultdict(lambda: Counter())
    for r in scored:
        by_sector[r.get("sector","Unknown")][r["tone_label"]] += 1

    print(f"\n  Per-sector tone (Confident% / Neutral% / Cautious%):")
    print(f"  {'Sector':<28} {'N':>5}  {'Conf':>6}  {'Neut':>6}  {'Caut':>6}")
    print("  " + "─"*55)
    for sector in sorted(by_sector):
        c = by_sector[sector]
        sn = sum(c.values())
        if sn == 0:
            continue
        print(f"  {sector:<28} {sn:>5}  "
              f"{c['Confident']/sn*100:>5.0f}%  "
              f"{c['Neutral']/sn*100:>5.0f}%  "
              f"{c['Cautious']/sn*100:>5.0f}%")

    # Mismatch rate
    if args.review:
        mm = sum(1 for r in scored if r.get("mismatch") == "YES")
        print(f"\n  Mismatch rate: {mm}/{n} ({mm/n*100:.1f}%)")

    # Top signals overall
    all_signals: list[str] = []
    for r in scored:
        all_signals.extend(s.strip() for s in r.get("signals","").split(",") if s.strip())
    top = Counter(all_signals).most_common(20)
    print(f"\n  Top 20 signals across corpus:")
    for word, count in top:
        pct = count / n * 100
        print(f"    {word:<22s} {count:>5}x  ({pct:.0f}%)")

    # Per-sector top signals (useful for detecting sector-specific noise)
    print(f"\n  Top 5 signals per sector:")
    sector_signals = defaultdict(list)
    for r in scored:
        s = r.get("sector", "Unknown")
        sector_signals[s].extend(
            sig.strip() for sig in r.get("signals","").split(",") if sig.strip()
        )
    for sector in sorted(sector_signals):
        top5 = Counter(sector_signals[sector]).most_common(5)
        words = ", ".join(f"{w}({c})" for w, c in top5)
        print(f"    {sector:<28s} {words}")

    print(f"\n{'═'*65}\n")


if __name__ == "__main__":
    main()
