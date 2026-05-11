"""
Exchange-aware filings searcher.

Routing priority:
  1. Ticker suffix (.NS → NSE, .BO → BSE, .L → LSE, .T → JPX, etc.)  — most reliable
  2. FMP exchange short code                                            — fallback
  3. No suffix + no recognised code                                    → assume US

Sources per route:
  US       → SEC EDGAR
  LSE/AIM  → Investegate.co.uk  (company name, no ticker)
  NSE      → nseindia.com        (base ticker, no .NS suffix)
  BSE      → bseindia.com        (base ticker, no .BO suffix)
  JPX      → release.tdnet.info  (5-digit TSE code, free, no API key)
  LOCAL    → generic exchange phrase search
  ALL      → company IR website  (open web search)

Flags:
  do_exchange  — run exchange-specific source (SEC, NSE, BSE, Investegate, TDnet, LOCAL)
  do_ir        — run IR website search
"""
import logging
import re
from datetime import datetime
from .rag_processor import RAGProcessor
from .tdnet_searcher import TDnetSearcher
from .nse_searcher import NSESearcher

logger       = logging.getLogger(__name__)
MAX_RESULTS  = 5
CURRENT_YEAR = datetime.now().year

US_EXCHANGES = {
    "NASDAQ", "NYSE", "AMEX", "NYSE MKT", "NYSE ARCA",
    "NASDAQ GM", "NASDAQ GS", "NASDAQ CM", "OTC", "OTCMKTS", "BATS",
}

# FMP may use different short codes for the same exchange
NSE_CODES = {"NSE", "NSI", "NSEI", "NSB"}
BSE_CODES = {"BSE", "BSI", "BSEX"}
LSE_CODES = {"LSE", "LON", "AIM"}

# Ticker suffix → routing key (checked before FMP exchange code)
SUFFIX_ROUTES = {
    ".NS": "NSE",
    ".BO": "BSE",
    ".L":  "LSE",
    ".AX": "LOCAL",
    ".TO": "LOCAL",
    ".V":  "LOCAL",
    ".HK": "LOCAL",
    ".T":  "JPX",
    ".SS": "LOCAL",
    ".SZ": "LOCAL",
    ".KS": "LOCAL",
    ".DE": "LOCAL",
    ".PA": "LOCAL",
    ".AS": "LOCAL",
    ".MI": "LOCAL",
    ".MC": "LOCAL",
    ".SW": "LOCAL",
    ".ST": "LOCAL",
    ".CO": "LOCAL",
    ".HE": "LOCAL",
    ".OL": "LOCAL",
    ".SA": "LOCAL",
    ".JO": "LOCAL",
    ".WA": "LOCAL",
    ".IR": "LOCAL",
    ".BR": "LOCAL",
}

# Generic exchange hint phrases (used by LOCAL route)
EXCHANGE_HINTS = {
    "LSE":      ["London Stock Exchange", "Regulatory News Service", "RNS"],
    "AIM":      ["AIM London", "Regulatory News Service", "RNS"],
    "XETRA":    ["DGAP", "Deutsche Boerse", "Bundesanzeiger"],
    "HKE":      ["HKEX", "Hong Kong Stock Exchange announcement"],
    "SGX":      ["SGX", "Singapore Exchange"],
    "KRX":      ["Korea Exchange", "KRX"],
    "JPX":      ["Tokyo Stock Exchange", "JPX"],
    "WSE":      ["GPW Warsaw", "Warsaw Stock Exchange"],
    "JSE":      ["Johannesburg Stock Exchange", "JSE"],
    "TSX":      ["Toronto Stock Exchange", "SEDAR"],
    "TSXV":     ["TSX Venture", "SEDAR"],
    "ASX":      ["ASX announcement", "ASX filing"],
    "EURONEXT": ["Euronext", "AMF filing"],
}


# ── Routing helper ────────────────────────────────────────────────────────────

def _route_exchange(ticker, exchange):
    """
    Return routing key: US | LSE | NSE | BSE | LOCAL
    Ticker suffix is checked first; FMP exchange code is the fallback.
    """
    t = ticker.upper()
    e = (exchange or "").upper().strip()

    for suffix, route in SUFFIX_ROUTES.items():
        if t.endswith(suffix.upper()):
            return route

    if e in US_EXCHANGES:
        return "US"
    if e in NSE_CODES:
        return "NSE"
    if e in BSE_CODES:
        return "BSE"
    if e in LSE_CODES:
        return "LSE"

    # No suffix, no recognised code → plain ticker is US-listed
    if "." not in t:
        return "US"

    return "LOCAL"


# ── Name/domain helpers ───────────────────────────────────────────────────────

def _base_ticker(ticker):
    """SGFIN.NS → SGFIN"""
    return ticker.split(".")[0]


def _search_name(name):
    """Strip trailing corporate suffixes: '4imprint Group plc' → '4imprint'"""
    clean = re.sub(
        r'\s+(plc|PLC|Inc\.?|Ltd\.?|Limited|Corp\.?|Corporation|'
        r'Group|Holdings?|AG|SA|NV|SE|GmbH|AB|ASA|Oyj)\s*$',
        '', name.strip(), flags=re.IGNORECASE,
    ).strip()
    return clean or name



# ── Main class ────────────────────────────────────────────────────────────────

class FilingsSearcher:
    def __init__(self, tavily_client, groq_client):
        self.tavily  = tavily_client
        self.groq    = groq_client
        self.rag     = RAGProcessor()
        self.tdnet   = TDnetSearcher()
        self.nse     = NSESearcher(tavily_client=tavily_client)

    # ── Core fetch helper ─────────────────────────────────────────────────────
    def _fetch(self, query, days_back, exclude_urls,
               max_r=MAX_RESULTS, source_label="Filing", include_domains=None):
        out  = []
        data = self.tavily.search_filings(
            query, max_results=max_r, days=days_back,
            include_domains=include_domains,
        )
        for r in data.get("results", []):
            url = r.get("url", "")
            if url in exclude_urls:
                continue
            content = r.get("content", "") or r.get("raw_content", "") or ""
            if url.lower().endswith(".pdf") and not content.strip():
                rag_text = self.rag.process_pdf_url(url, source_label)
                if rag_text:
                    content = rag_text
            out.append({
                "title":          r.get("title", ""),
                "url":            url,
                "content":        content[:1200],
                "published_date": r.get("published_date", ""),
                "source":         source_label,
                "score":          r.get("score", 0),
            })
        return out

    # ── SEC EDGAR (US only) ───────────────────────────────────────────────────
    def _search_edgar(self, info, days_back, exclude_urls):
        name   = info["name"]
        ticker = info["ticker"]
        out = []
        for q in [
            '"' + name + '" site:sec.gov 8-K 6-K press release filing ' + str(CURRENT_YEAR),
            ticker + " " + name + " SEC filing investor relations " + str(CURRENT_YEAR),
        ]:
            out.extend(self._fetch(q, days_back, exclude_urls, source_label="SEC / EDGAR"))
        return out

    # ── TDnet / JPX (Japan) ───────────────────────────────────────────────────
    def _search_tdnet(self, info, days_back, exclude_urls, progress_cb=None):
        """Scrape release.tdnet.info — no API key required."""
        return self.tdnet.search(
            info["ticker"],
            days_back=days_back,
            exclude_urls=exclude_urls,
            progress_cb=progress_cb,
        )

    # ── Investegate (LSE / AIM) ───────────────────────────────────────────────
    def _search_investegate(self, info, days_back, exclude_urls):
        sname = _search_name(info["name"])
        out = []
        for q in [
            sname + " regulatory news announcement " + str(CURRENT_YEAR),
            sname + " results dividend acquisition announcement",
        ]:
            out.extend(self._fetch(
                q, days_back, exclude_urls,
                source_label="Investegate (LSE)",
                include_domains=["investegate.co.uk"],
            ))
        return out

    # ── NSE India ─────────────────────────────────────────────────────────────
    def _search_nse(self, info, days_back, exclude_urls, progress_cb=None):
        """Fetch corporate announcements directly from NSE India API."""
        return self.nse.search(
            info["ticker"],
            days_back=days_back,
            exclude_urls=exclude_urls,
            progress_cb=progress_cb,
        )

    # ── BSE India ─────────────────────────────────────────────────────────────
    def _search_bse(self, info, days_back, exclude_urls):
        bticker = _base_ticker(info["ticker"])
        out = []
        for q in [
            bticker + " BSE corporate announcement filing " + str(CURRENT_YEAR),
            info["name"] + " BSE disclosure results board " + str(CURRENT_YEAR),
        ]:
            out.extend(self._fetch(
                q, days_back, exclude_urls,
                source_label="BSE India",
                include_domains=["bseindia.com"],
            ))
        return out

    # ── Generic non-US exchange (LOCAL fallback) ──────────────────────────────
    def _search_local_exchange(self, info, days_back, exclude_urls):
        name          = info["name"]
        ticker        = info["ticker"]
        exchange      = (info.get("exchange")      or "").upper().strip()
        exchange_full = (info.get("exchange_full") or exchange or "").strip()
        hints = EXCHANGE_HINTS.get(exchange, [])
        if not hints:
            hints = [exchange_full] if exchange_full else ["stock exchange filing"]
        out = []
        for hint in hints[:2]:
            out.extend(self._fetch(
                '"' + name + '" ' + hint + ' filing announcement ' + str(CURRENT_YEAR),
                days_back, exclude_urls,
                source_label=exchange_full or exchange or "Exchange Filing",
            ))
        out.extend(self._fetch(
            ticker + ' "' + (exchange_full or exchange) + '" results earnings ' + str(CURRENT_YEAR),
            days_back, exclude_urls,
            source_label=exchange_full or exchange or "Exchange Filing",
        ))
        return out

    # ── Main entry point ──────────────────────────────────────────────────────
    def search_all(self, company_info, progress_cb=None, days_back=30,
                   exclude_urls=None, do_exchange=True, do_ir=False):
        """
        Search filings.
        do_exchange — search exchange-specific source (SEC, NSE, BSE, Investegate, LOCAL)
        do_ir       — search company IR website
        """
        exclude_urls = exclude_urls or set()
        all_filings  = []
        ticker       = company_info.get("ticker", "")
        exchange     = company_info.get("exchange", "")
        route        = _route_exchange(ticker, exchange)

        logger.info("Filing route for %s (exchange=%r): %s", ticker, exchange, route)

        def _emit(msg):
            if progress_cb:
                progress_cb(msg)

        # ── Exchange-specific filings ──
        if do_exchange:
            if route == "US":
                _emit("Searching SEC/EDGAR for " + company_info["name"] + "...")
                try:
                    r = self._search_edgar(company_info, days_back, exclude_urls)
                    all_filings.extend(r)
                    _emit("Found " + str(len(r)) + " SEC/EDGAR filing(s).")
                except Exception as exc:
                    logger.error("EDGAR search error: %s", exc)

            elif route == "LSE":
                _emit("Searching Investegate for " + company_info["name"] + "...")
                try:
                    r = self._search_investegate(company_info, days_back, exclude_urls)
                    all_filings.extend(r)
                    _emit("Found " + str(len(r)) + " Investegate result(s).")
                except Exception as exc:
                    logger.error("Investegate search error: %s", exc)

            elif route == "NSE":
                _emit("Searching NSE India for " + _base_ticker(ticker) + "...")
                try:
                    def _np(msg): _emit("   📋 " + msg)
                    r = self._search_nse(company_info, days_back, exclude_urls, progress_cb=_np)
                    all_filings.extend(r)
                    _emit("Found " + str(len(r)) + " NSE India filing(s).")
                except Exception as exc:
                    logger.error("NSE search error: %s", exc)

            elif route == "BSE":
                _emit("Searching BSE India for " + _base_ticker(ticker) + "...")
                try:
                    r = self._search_bse(company_info, days_back, exclude_urls)
                    all_filings.extend(r)
                    _emit("Found " + str(len(r)) + " BSE India filing(s).")
                except Exception as exc:
                    logger.error("BSE search error: %s", exc)

            elif route == "JPX":
                _emit("Searching TDnet (JPX) for " + ticker + "...")
                try:
                    def _tp(msg): _emit("   📋 " + msg)
                    r = self._search_tdnet(company_info, days_back, exclude_urls, progress_cb=_tp)
                    all_filings.extend(r)
                    _emit("Found " + str(len(r)) + " TDnet filing(s).")
                except Exception as exc:
                    logger.error("TDnet search error: %s", exc)

            else:
                exch_label = company_info.get("exchange_full") or exchange or "local exchange"
                _emit("Searching " + exch_label + " filings for " + company_info["name"] + "...")
                try:
                    r = self._search_local_exchange(company_info, days_back, exclude_urls)
                    all_filings.extend(r)
                    _emit("Found " + str(len(r)) + " " + exch_label + " filing(s).")
                except Exception as exc:
                    logger.error("Local exchange search error: %s", exc)

        return all_filings
