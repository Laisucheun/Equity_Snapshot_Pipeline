"""
transcript_loader.py — Motley Fool earnings call transcript fetcher

Uses fool_transcripts.db (built by fool_transcript_db.py) to look up
transcript URLs by ticker. One GET per ticker — no probing, no Cloudflare.

Build the DB once:
    python fool_transcript_db.py --build

Update monthly:
    python fool_transcript_db.py --update

The DB is a SQLite file with columns: ticker, date, quarter, fiscal_year, url.
Most recent entry per ticker is used by default.
"""

import os
import re
import sqlite3
import logging
import requests

logger = logging.getLogger(__name__)

_DB_PATH         = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fool_transcripts.db")
_REQUEST_TIMEOUT = 15
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _clean_text(text: str) -> str:
    """Remove HTML entity artifacts from extracted text."""
    for _ in range(2):
        text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&nbsp;',  ' ',  text)
    text = re.sub(r'&quot;',  '"',  text)
    text = re.sub(r'&#\d+;', ' ',  text)
    text = re.sub(r';(?=[\s,.()\'\"-]|$)', '', text)
    text = re.sub(r'[ \t]+',  ' ',  text)
    return text


def _unavailable(reason: str) -> dict:
    return {"available": False, "source": "Motley Fool transcript",
            "url": None, "text": "", "error": reason}


class TranscriptLoader:
    """
    Fetches Motley Fool earnings call transcripts using the local SQLite DB.
    Build the DB with: python fool_transcript_db.py --build
    """

    def __init__(self, db_path: str = _DB_PATH):
        self._cache:   dict[str, dict] = {}
        self._db_path  = db_path
        self._db_ok    = os.path.exists(db_path)
        if not self._db_ok:
            logger.warning(
                "TranscriptLoader: DB not found at %s — "
                "run 'python fool_transcript_db.py --build' to create it", db_path
            )

    def fetch(self, ticker: str) -> dict:
        ticker = ticker.upper()
        if ticker in self._cache:
            return self._cache[ticker]

        if not self._db_ok:
            result = _unavailable(
                "fool_transcripts.db not found. "
                "Run: python fool_transcript_db.py --build"
            )
            self._cache[ticker] = result
            return result

        url = self._lookup_url(ticker)
        if not url:
            result = _unavailable(f"{ticker} not found in transcript DB")
            self._cache[ticker] = result
            return result

        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
            if resp.status_code == 200:
                text, label = self._extract_guidance_text(resp.text, ticker)
                if text and text.strip():
                    result = {
                        "available": True,
                        "source":    f"Motley Fool transcript ({label})",
                        "url":       url,
                        "text":      text,
                        "error":     None,
                    }
                    self._cache[ticker] = result
                    return result
            result = _unavailable(f"HTTP {resp.status_code} fetching {url}")
            self._cache[ticker] = result
            return result

        except Exception as e:
            result = _unavailable(str(e))
            self._cache[ticker] = result
            return result

    def _lookup_url(self, ticker: str) -> str | None:
        """Look up most recent transcript URL for ticker from DB."""
        try:
            with sqlite3.connect(self._db_path) as con:
                row = con.execute(
                    "SELECT url FROM transcripts WHERE ticker=? ORDER BY date DESC LIMIT 1",
                    (ticker,)
                ).fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.warning("TranscriptLoader: DB lookup error — %s", e)
            return None

    def get_history(self, ticker: str) -> list[dict]:
        """Return all transcript records for a ticker, most recent first."""
        try:
            with sqlite3.connect(self._db_path) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT ticker, date, quarter, fiscal_year, url "
                    "FROM transcripts WHERE ticker=? ORDER BY date DESC",
                    (ticker.upper(),)
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    # ── Text extraction ───────────────────────────────────────────────────────



    @staticmethod
    def _extract_guidance_text(html: str, ticker: str) -> tuple[str, str]:
        """
        Extract guidance text from a Motley Fool transcript page.

        Strategy:
          1. Strip JS/JSON blobs (Next.js hydration, __next_f.push, etc.)
          2. Strip HTML tags
          3. Extract TAKEAWAYS section → split into forward/backward bullets
          4. Extract SUMMARY paragraph → copy verbatim as narrative block

        The Fool's editorial team surfaces the important stuff in TAKEAWAYS
        and SUMMARY. No need to hunt through PREPARED REMARKS.
        """
        # ── Step 1: Strip JS/JSON blobs BEFORE HTML stripping ─────────────────
        # Next.js embeds hydration JSON: self.__next_f.push([...]) etc.
        html = re.sub(r'self\.__next_f\.push\(\[.{0,2000}?\]\)', ' ', html, flags=re.DOTALL)
        html = re.sub(r'\(self\.__next_f\s*=\s*self\.__next_f[^)]+\)', ' ', html)
        # Remove <script> blocks entirely
        html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        # Remove <style> blocks
        html = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        # Remove JSON-like blobs (long strings with {" patterns)
        html = re.sub(r'\{["\'][\w$]+["\']\s*:[^}]{0,500}\}', ' ', html)

        # ── Step 2: Strip HTML ────────────────────────────────────────────────
        # Insert newlines at block boundaries BEFORE stripping so structure survives
        # e.g. </p><ul><li> compact HTML collapses to one line without this
        text = re.sub(r'</p>', '\n\n', html, flags=re.IGNORECASE)
        text = re.sub(r'</li>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<li[^>]*>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'</?(?:ul|ol|p|div|section|article|h[1-6]|blockquote)[^>]*>', '\n', text, flags=re.IGNORECASE)
        # Remove inline formatting tags without spaces (avoids "31. 5%" splits)
        text = re.sub(r'<(?:strong|b|em|i|span|a|code)[^>]*>|</(?:strong|b|em|i|span|a|code)>', '', text)
        text = re.sub(r'<[^>]+>', ' ', text)
        # Entity decoding — multiple passes for &amp;amp; chains
        for _ in range(3):
            text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&nbsp;',  ' ',  text)
        text = re.sub(r'&#160;',  ' ',  text)
        text = re.sub(r'&lt;',    '<',  text)
        text = re.sub(r'&gt;',    '>',  text)
        text = re.sub(r'&#\d+;',  ' ',  text)
        text = re.sub(r'&quot;',  '"',  text)
        text = re.sub(r'&apos;',  "'",  text)
        # Stray semicolons from entity artifacts
        text = re.sub(r';(?=[\s,.()\'"\\-]|$)', '', text)
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()

        # ── Step 3: Remove remaining noise lines ──────────────────────────────
        # Drop lines that look like JS/JSON artifacts
        clean_lines = []
        for line in text.split('\n'):
            stripped = line.strip()
            # Skip lines with JSON-like patterns
            if re.search(r'__next_f|\\u003c|\\u0022|self\.__|\["\\$|push\(\[|initialParams', stripped):
                continue
            # Skip very long lines with no spaces (minified JS)
            if len(stripped) > 200 and stripped.count(' ') < 5:
                continue
            clean_lines.append(line)
        text = '\n'.join(clean_lines)

        label = _detect_quarter_label(text, ticker)
        parts = []

        # Known section end anchors
        _END = (
            r'(?:Summary|Risks?|Industry\s+Glossary|'
            r'Full\s+(?:Conference\s+Call\s+)?Transcript|'
            r'Questions?\s+and\s+Answers?|'
            r'Analyst:|Operator:)'
        )

        # ── Step 3: TAKEAWAYS → split bullets ─────────────────────────────────
        m_tk = re.search(
            rf'\bTakeaways?\b\s*(.+?)(?=\b{_END}\b|\Z)',
            text, re.IGNORECASE | re.DOTALL
        )
        if m_tk:
            content = m_tk.group(1).strip()
            if len(content) > 60:
                # Split "Label -- content." into one bullet per line
                content = re.sub(r'(?<=[."])\s+(?=[A-Z])', '\n', content)
                parts.append(f'TAKEAWAYS:\n{content}')

        # ── Step 4: SUMMARY — prose narrative + forward-looking sub-bullets ────
        m_sm = re.search(
            r'\bSummary\b\s*(.+?)(?=\bIndustry\s+Glossary\b|\bFull\s+(?:Conference\s+Call\s+)?Transcript\b|\Z)',
            text, re.IGNORECASE | re.DOTALL
        )
        if m_sm:
            content = m_sm.group(1).strip()
            if len(content) > 60 and not re.search(r'__next|\\u00|push\(\[', content):
                # Split on blank lines — handles \n \n from HTML strip
                paras = [p.strip() for p in re.split(r'\n[ \t]*\n', content) if p.strip()]

                # Fallback: if still one block (compact HTML, no blank lines),
                # split on sentence boundaries. Each '. [Capital]' becomes a new line,
                # then re-split on blank lines. The prose paragraph is the first block
                # and the editorial bullets follow as individual lines.
                if len(paras) == 1 and len(paras[0]) > 300:
                    # Insert newlines at sentence boundaries first
                    split_content = re.sub(r'(?<=\.)\s+(?=[A-Z])', '\n', paras[0])
                    lines = [l.strip() for l in split_content.split('\n') if l.strip()]
                    if len(lines) > 1:
                        # Find where the prose ends and bullets begin:
                        # The prose paragraph is consecutive sentences that form a
                        # coherent block. Bullets are shorter, standalone sentences.
                        # Heuristic: the prose paragraph ends when we hit a sentence
                        # that starts with a proper noun or "The company" / "Updated"
                        # after at least 2 prior sentences.
                        prose_end = len(lines)  # default: all prose
                        for i in range(1, len(lines)):
                            # A new "bullet" sentence typically starts fresh subject
                            # and is not a continuation clause
                            if (i >= 1 and
                                re.match(r'^(?:Management|The company|Tailored|Updated|[A-Z][a-z]+(?:\'s)?(?:\s+[A-Z]|\s+pipeline|\s+innovation|\s+guidance))', lines[i])):
                                prose_end = i
                                break
                        if prose_end < len(lines):
                            paras = [' '.join(lines[:prose_end]), '\n'.join(lines[prose_end:])]

                # First paragraph → prose narrative
                summary_prose = paras[0] if paras else content
                if len(summary_prose) > 800:
                    summary_prose = summary_prose[:800].rsplit('.', 1)[0] + '.'
                parts.append(f'SUMMARY_PROSE:\n{summary_prose}')
                # Remaining paragraphs → bullet lines for classifier
                if len(paras) > 1:
                    bullet_block = '\n'.join(paras[1:]).strip()
                    if len(bullet_block) > 40:
                        parts.append(f'SUMMARY_BULLETS:\n{bullet_block}')

        if parts:
            return _clean_text('\n\n'.join(parts)), label

        return '', ''





def _detect_quarter_label(text: str, ticker: str) -> str:
    m = re.search(r'Q([1-4])\s*(?:FY|fiscal\s*year\s*)?(\d{4})\s*[Ee]arnings', text)
    if m:
        return f"{ticker} Q{m.group(1)} FY{m.group(2)}"
    m = re.search(r'(?:first|second|third|fourth)\s+quarter[^,\.]{0,20}(\d{4})', text, re.IGNORECASE)
    if m:
        q_map = {"first": 1, "second": 2, "third": 3, "fourth": 4}
        word  = re.search(r'(first|second|third|fourth)', m.group(), re.IGNORECASE).group().lower()
        return f"{ticker} Q{q_map[word]} FY{m.group(1)}"
    return f"{ticker} (latest)"
