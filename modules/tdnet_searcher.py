"""
TDnet scraper for Japanese stock filings (TSE / JPX).

Uses the public Company Announcements Service at release.tdnet.info.
No API key required — data is freely accessible.

Ticker → code mapping:
  7203.T  →  "72030"   (4-digit code + trailing "0", giving the 5-digit TSE code)
  6758.T  →  "67580"   (Sony)

Search endpoint accepts the 5-digit code in the free-text field "q".
The response is a standard HTML table — no JavaScript rendering required.

PDF documents return 403 for direct access (same-origin browser policy),
so filing titles are used as the content field.  The English titles from
TDnet are descriptive enough for LLM analysis (e.g. "FY2026 Consolidated
Financial Results (IFRS)").
"""
import logging
import re
import requests
from datetime import datetime, timedelta
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

TDNET_SEARCH_URL = "https://www.release.tdnet.info/onsf/TDJFSearch_e/TDJFSearch_e"
TDNET_BASE_URL   = "https://www.release.tdnet.info"
MAX_RESULTS      = 10
_SESS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PortfolioMonitor/1.0)",
    "Referer":    "https://www.release.tdnet.info/onsf/TDJFSearch_e/I_head",
}


# ── Ticker → 5-digit TSE code ─────────────────────────────────────────────────

def ticker_to_code(ticker):
    """
    Convert a Yahoo-Finance-style .T ticker to the 5-digit TSE code.
      "7203.T"  →  "72030"
      "6758.T"  →  "67580"
    Returns None if the base part is not numeric.
    """
    base = ticker.split(".")[0].strip()
    if not base.isdigit():
        return None
    # TSE codes are 4 digits; TDnet expects 5 digits (trailing "0")
    return (base + "0")[:5] if len(base) <= 4 else base[:5]


# ── HTML parser for TDnet search results ─────────────────────────────────────

class _FilingParser(HTMLParser):
    """
    Extracts filing rows from the TDnet search result table.
    Each <tr> contains <td class="time|code|companyname|sector|title"> cells.
    The title cell wraps an <a href="/inbs/ek/XXXXXXXXXXXXXXXX.pdf">.
    """

    def __init__(self):
        super().__init__()
        self.filings      = []
        self._in_tr       = False
        self._current_row = {}
        self._current_cls = None
        self._buf         = []
        self._pending_href = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._in_tr        = True
            self._current_row  = {}
            self._current_cls  = None
            self._buf          = []
            self._pending_href = None
        elif tag == "td" and self._in_tr:
            self._current_cls  = attrs.get("class", "")
            self._buf          = []
            self._pending_href = None
        elif tag == "a" and self._in_tr:
            href = attrs.get("href", "")
            if href.endswith(".pdf"):
                self._pending_href = href

    def handle_data(self, data):
        if self._in_tr and self._current_cls is not None:
            stripped = data.strip()
            if stripped:
                self._buf.append(stripped)

    def handle_endtag(self, tag):
        if tag == "td" and self._in_tr:
            text = " ".join(self._buf).strip()
            cls  = self._current_cls
            if cls == "time":
                self._current_row["time"] = text
            elif cls == "code":
                self._current_row["code"] = text
            elif cls == "companyname":
                self._current_row["name"] = text
            elif cls == "sector":
                self._current_row["sector"] = text
            elif cls == "title":
                self._current_row["title"] = text
                if self._pending_href:
                    self._current_row["url"] = self._pending_href
            self._current_cls  = None
            self._buf          = []
            self._pending_href = None

        elif tag == "tr" and self._in_tr:
            row = self._current_row
            # Only keep rows that look like real filing entries
            if row.get("title") and row.get("time") and row.get("code", "").isdigit():
                self.filings.append(dict(row))
            self._in_tr       = False
            self._current_row = {}


# ── Main searcher class ───────────────────────────────────────────────────────

class TDnetSearcher:
    """
    Scrapes the TDnet public search endpoint for filings of a .T-suffix ticker.
    Thread-safe: each instance maintains its own requests.Session.
    """

    def __init__(self):
        self._sess = requests.Session()
        self._sess.headers.update(_SESS_HEADERS)

    def search(self, ticker, days_back=7, exclude_urls=None, progress_cb=None):
        """
        Fetch recent TDnet filings for *ticker* (e.g. "7203.T").

        Returns a list of dicts compatible with FilingsSearcher output:
          {title, url, content, published_date, source, score}

        content is set to the English filing title (PDF direct-download
        is blocked by same-origin policy on the TDnet server).
        """
        exclude_urls = exclude_urls or set()

        code = ticker_to_code(ticker)
        if not code:
            logger.warning("TDnet: cannot derive TSE code from ticker '%s'", ticker)
            return []

        today     = datetime.now()
        date_from = (today - timedelta(days=days_back)).strftime("%Y%m%d")
        date_to   = today.strftime("%Y%m%d")

        if progress_cb:
            progress_cb("Searching TDnet for code " + code + " (" + ticker + ")...")

        try:
            resp = self._sess.post(
                TDNET_SEARCH_URL,
                data={"t0": date_from, "t1": date_to, "q": code, "p": "1"},
                timeout=20,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("TDnet request failed for %s: %s", ticker, exc)
            return []

        parser = _FilingParser()
        try:
            parser.feed(resp.text)
        except Exception as exc:
            logger.warning("TDnet parse error for %s: %s", ticker, exc)
            return []

        results = []
        for row in parser.filings[:MAX_RESULTS]:
            raw_url  = row.get("url", "")
            full_url = (TDNET_BASE_URL + raw_url) if raw_url else ""

            if full_url in exclude_urls:
                continue

            title    = row.get("title", "").strip()
            raw_time = row.get("time", "")          # "2026/05/08 13:55"
            iso_date = raw_time[:10].replace("/", "-") if raw_time else ""
            # Build a content string: title + sector context
            sector   = row.get("sector", "")
            content  = title
            if sector:
                content = "[" + sector + "] " + title

            results.append({
                "title":          title,
                "url":            full_url,
                "content":        content,
                "published_date": iso_date,
                "source":         "TDnet (JPX)",
                "score":          1.0,
            })

        logger.info(
            "TDnet: %d filing(s) found for %s (code %s, last %d days)",
            len(results), ticker, code, days_back,
        )
        if progress_cb:
            progress_cb("Found " + str(len(results)) + " TDnet filing(s) for " + ticker + ".")

        return results
