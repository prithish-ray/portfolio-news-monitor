"""
NSE India direct scraper.

Filings page (HTML):
  https://www.nseindia.com/companies-listing/corporate-filings-announcements?symbol=SGFIN&tabIndex=equity

Correct API endpoint (confirmed from NSE's own JS bundle):
  https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=SGFIN
  (The old /api/corp-announcements endpoint is dead — 404.)

Requires curl_cffi for Chrome TLS fingerprint impersonation.
NSE blocks plain requests/urllib because they don't match Chrome's TLS handshake.
Install once: pip install curl_cffi

Falls back to Tavily if curl_cffi is not installed or if the API fails.
"""
import logging
import time
from datetime import datetime, timedelta

logger        = logging.getLogger(__name__)

NSE_BASE      = "https://www.nseindia.com"
NSE_API_URL   = NSE_BASE + "/api/corporate-announcements"   # ← corrected endpoint
NSE_ARCH_BASE = "https://nsearchives.nseindia.com"

try:
    from curl_cffi import requests as _cffi_requests
    _HAS_CURL_CFFI = True
    logger.debug("curl_cffi available — NSE will use Chrome impersonation")
except ImportError:
    import requests as _cffi_requests          # plain requests as fallback name
    _HAS_CURL_CFFI = False
    logger.warning(
        "curl_cffi not installed — NSE direct API will likely be blocked. "
        "Install with: pip install curl_cffi"
    )


def _filings_page_url(symbol):
    return (
        NSE_BASE
        + "/companies-listing/corporate-filings-announcements"
        + "?symbol=" + symbol
        + "&tabIndex=equity"
    )


def _make_session(symbol):
    """
    Return a session that NSE will accept.
    With curl_cffi: full Chrome TLS fingerprint impersonation (JA3/ALPN).
    Without curl_cffi: plain requests with browser headers (usually blocked).
    """
    if _HAS_CURL_CFFI:
        sess = _cffi_requests.Session(impersonate="chrome124")
    else:
        import requests
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept":          "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })

    # Warm-up: visit root then the specific filings page to set nsit cookie
    warm_ups = [
        NSE_BASE + "/",
        _filings_page_url(symbol),
    ]
    for url in warm_ups:
        try:
            sess.get(url, timeout=15)
            time.sleep(0.5)
        except Exception as exc:
            logger.debug("NSE warm-up GET %s failed: %s", url, exc)

    return sess


def _parse_date(item):
    """
    Return (datetime, str) from an NSE announcement dict.
    NSE API returns:
      sort_date:   "2026-04-29 17:30:24"  (datetime string)
      an_dt:       "29-Apr-2026 17:30:24" (human-readable string)
      exchdisstime:"29-Apr-2026 17:30:25" (exchange dissemination time)
    Returns (None, '') on failure.
    """
    # Priority 1: sort_date — "YYYY-MM-DD HH:MM:SS" format (current NSE API)
    sort_raw = (item.get("sort_date") or "").strip()
    if sort_raw:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(sort_raw, fmt)
                return dt, dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

    # Priority 2: an_dt / exchdisstime — "DD-Mon-YYYY HH:MM:SS" format
    for field in ("an_dt", "exchdisstime", "exchDissTime"):
        raw = (item.get(field) or "").strip()
        if not raw:
            continue
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt, dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

    return None, ""


def _build_url(item, symbol):
    """Return the best URL for an announcement (direct PDF link preferred)."""
    attchmnt = (item.get("attchmntFile") or "").strip()
    if attchmnt:
        if attchmnt.startswith("http"):
            return attchmnt
        if not attchmnt.startswith("/"):
            attchmnt = "/" + attchmnt
        return NSE_ARCH_BASE + attchmnt
    return _filings_page_url(symbol)


def _is_json(resp):
    ct = resp.headers.get("Content-Type", "")
    return "json" in ct


class NSESearcher:
    def __init__(self, tavily_client=None):
        self._tavily = tavily_client

    def search(self, ticker, days_back=30, exclude_urls=None, progress_cb=None):
        """
        Fetch recent corporate announcements from NSE India.
        ticker: full ticker e.g. SGFIN.NS or just SGFIN
        Returns list of filing dicts.
        """
        symbol       = ticker.split(".")[0].upper()
        exclude_urls = exclude_urls or set()
        cutoff       = datetime.now() - timedelta(days=days_back)

        if progress_cb:
            progress_cb("Fetching NSE India announcements for " + symbol + "...")
        logger.info("NSE direct: fetching for %s (last %d days)", symbol, days_back)

        out = self._fetch_direct(symbol, cutoff, exclude_urls)

        if out:
            logger.info("NSE direct: %d filing(s) for %s", len(out), symbol)
            if progress_cb:
                progress_cb("Found " + str(len(out)) + " NSE filing(s) for " + symbol + ".")
            return out

        # Direct path failed or returned nothing — try Tavily fallback
        if not _HAS_CURL_CFFI:
            logger.warning(
                "NSE direct failed for %s — install curl_cffi to fix: pip install curl_cffi",
                symbol,
            )
        else:
            logger.warning("NSE direct returned 0 results for %s — trying Tavily fallback", symbol)

        if progress_cb:
            progress_cb("NSE direct empty — trying Tavily search for " + symbol + "...")

        out = self._fetch_tavily(symbol, ticker, days_back, exclude_urls)
        logger.info("NSE Tavily fallback: %d filing(s) for %s", len(out), symbol)
        if progress_cb:
            progress_cb("Found " + str(len(out)) + " NSE filing(s) for " + symbol + " (via search).")
        return out

    # ── Direct NSE API ────────────────────────────────────────────────────────
    def _fetch_direct(self, symbol, cutoff, exclude_urls):
        """Call /api/corporate-announcements directly. Returns [] on failure."""
        for attempt in (1, 2):
            sess = _make_session(symbol)
            try:
                resp = sess.get(
                    NSE_API_URL,
                    params={"index": "equities", "symbol": symbol},
                    headers={"Referer": _filings_page_url(symbol)},
                    timeout=20,
                )
                logger.debug(
                    "NSE API attempt %d for %s: status=%d ct=%s",
                    attempt, symbol, resp.status_code,
                    resp.headers.get("Content-Type", "?"),
                )

                if resp.status_code in (401, 403, 429):
                    logger.warning("NSE API %d on attempt %d for %s", resp.status_code, attempt, symbol)
                    time.sleep(2)
                    continue

                if resp.status_code == 404:
                    logger.error(
                        "NSE API endpoint returned 404 for %s — endpoint may have changed again", symbol
                    )
                    return []

                resp.raise_for_status()

                if not _is_json(resp):
                    logger.warning(
                        "NSE API returned non-JSON for %s — likely bot-blocked (ct=%s). "
                        "Ensure curl_cffi is installed: pip install curl_cffi",
                        symbol, resp.headers.get("Content-Type", "?"),
                    )
                    return []

                data = resp.json()

            except Exception as exc:
                logger.warning("NSE API attempt %d failed for %s: %s", attempt, symbol, exc)
                time.sleep(1)
                continue

            if not isinstance(data, list):
                logger.warning("NSE API unexpected body type for %s: %s", symbol, type(data))
                return []

            out = []
            for item in data:
                dt, date_str = _parse_date(item)
                if dt and dt < cutoff:
                    continue            # outside the requested window

                title = (
                    item.get("attchmntText")
                    or item.get("desc")
                    or "NSE Announcement"
                ).strip()

                url = _build_url(item, symbol)
                if url in exclude_urls:
                    continue

                content = (item.get("attchmntText") or item.get("desc") or "").strip()

                out.append({
                    "title":          title,
                    "url":            url,
                    "content":        content[:1200],
                    "published_date": date_str,
                    "source":         "NSE India",
                    "score":          1.0,
                })

            return out

        return []   # both attempts failed

    # ── Tavily fallback ───────────────────────────────────────────────────────
    def _fetch_tavily(self, symbol, ticker, days_back, exclude_urls):
        """Search nseindia.com via Tavily when the direct API is unavailable."""
        if not self._tavily:
            return []

        yr      = str(datetime.now().year)
        queries = [
            symbol + " corporate announcement filing " + yr,
            symbol + " board meeting results disclosure " + yr,
        ]
        out  = []
        seen = set(exclude_urls)

        for q in queries:
            try:
                data = self._tavily.search_filings(
                    q,
                    max_results=5,
                    days=days_back,
                    include_domains=["nseindia.com", "nsearchives.nseindia.com"],
                )
                for r in data.get("results", []):
                    url = r.get("url", "")
                    if url in seen:
                        continue
                    seen.add(url)
                    out.append({
                        "title":          r.get("title", ""),
                        "url":            url,
                        "content":        (r.get("content") or "")[:1200],
                        "published_date": r.get("published_date", ""),
                        "source":         "NSE India",
                        "score":          r.get("score", 0),
                    })
            except Exception as exc:
                logger.warning("NSE Tavily fallback failed for %s: %s", symbol, exc)

        return out
