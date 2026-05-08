"""
Financial Modeling Prep (FMP) API client — stable endpoints.
Docs: https://site.financialmodelingprep.com/developer/docs

Stable API uses:
  GET https://financialmodelingprep.com/stable/<endpoint>?symbol=TICKER&apikey=KEY

search-symbol is free-tier compatible; profile may require a paid plan.
"""
import logging
import requests
from .cache_manager import CacheManager

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/stable"

# ISO-2 country codes to full names
COUNTRY_CODES = {
    "US": "United States",   "GB": "United Kingdom",  "UK": "United Kingdom",
    "FR": "France",          "DE": "Germany",          "JP": "Japan",
    "CN": "China",           "CA": "Canada",           "AU": "Australia",
    "IN": "India",           "NL": "Netherlands",      "CH": "Switzerland",
    "SE": "Sweden",          "DK": "Denmark",          "SG": "Singapore",
    "KR": "South Korea",     "BR": "Brazil",           "ES": "Spain",
    "IT": "Italy",           "NO": "Norway",           "FI": "Finland",
    "BE": "Belgium",         "PT": "Portugal",         "IE": "Ireland",
    "PL": "Poland",          "ZA": "South Africa",     "HK": "Hong Kong",
    "NZ": "New Zealand",     "MX": "Mexico",           "AR": "Argentina",
    "CL": "Chile",           "IL": "Israel",           "AE": "United Arab Emirates",
    "SA": "Saudi Arabia",    "RU": "Russia",           "TW": "Taiwan",
    "TH": "Thailand",        "ID": "Indonesia",        "MY": "Malaysia",
    "PH": "Philippines",     "AT": "Austria",          "GR": "Greece",
    "CZ": "Czech Republic",  "HU": "Hungary",          "RO": "Romania",
    "LU": "Luxembourg",      "MT": "Malta",            "CY": "Cyprus",
}


class FMPClient:
    def __init__(self, api_key):
        self._key = api_key
        self._profile_cache    = CacheManager(ttl=86400)  # 24 h
        self._quote_cache      = CacheManager(ttl=300)    # 5 min
        self._search_cache     = CacheManager(ttl=3600)   # 1 h
        self._sess = requests.Session()
        self._sess.headers.update({"User-Agent": "PortfolioMonitor/1.0"})

    # ── Internal ──────────────────────────────────────────────────────────────
    def _get(self, path, symbol, extra=None):
        """
        GET {FMP_BASE}{path}?symbol={symbol}&apikey=...
        Returns a list on success, [] on any error.
        """
        params = {"symbol": symbol, "apikey": self._key}
        if extra:
            params.update(extra)
        try:
            r = self._sess.get(FMP_BASE + path, params=params, timeout=12)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "Error Message" in data:
                logger.warning("FMP error (%s %s): %s", path, symbol, data["Error Message"])
                return []
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
            return []
        except Exception as exc:
            logger.warning("FMP request failed (%s %s): %s", path, symbol, exc)
            return []

    def _get_query(self, path, query):
        """
        GET {FMP_BASE}{path}?query={query}&apikey=...
        Used for search endpoints that take a query string, not a symbol.
        """
        params = {"query": query, "apikey": self._key}
        try:
            r = self._sess.get(FMP_BASE + path, params=params, timeout=12)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "Error Message" in data:
                logger.warning("FMP search error (%s): %s", path, data["Error Message"])
                return []
            if isinstance(data, list):
                return data
            return []
        except Exception as exc:
            logger.warning("FMP search failed (%s %s): %s", path, query, exc)
            return []

    # ── Symbol search (free tier) ─────────────────────────────────────────────
    def search_symbol(self, query):
        """
        Search for matching symbols using the free-tier search-symbol endpoint.
        Returns list of dicts: {symbol, name, currency, stockExchange, exchangeShortName}
        Results are cached 1 hour.
        """
        key = "fmp_search:" + query.upper()
        cached = self._search_cache.get(key)
        if cached is not None:
            return cached

        results = self._get_query("/search-symbol", query)
        # Normalise fields
        out = []
        for r in results:
            sym = (r.get("symbol") or "").strip()
            if not sym:
                continue
            out.append({
                "symbol":        sym,
                "name":          (r.get("name") or sym).strip(),
                "currency":      (r.get("currency") or "").strip(),
                "exchange":      (r.get("exchangeShortName") or "").strip(),
                "exchange_full": (r.get("stockExchange") or "").strip(),
            })
        self._search_cache.set(key, out)
        return out

    # ── Profile ───────────────────────────────────────────────────────────────
    def get_profile(self, symbol):
        """
        Return a normalised company profile dict, or {} on failure.
        Keys: ticker, name, sector, industry, country, website,
              exchange, exchange_full, currency, market_cap, description, employees, source
        May fail on free-tier accounts (403 / Error Message).
        """
        key    = symbol.upper()
        cached = self._profile_cache.get("fmp_profile:" + key)
        if cached:
            return cached

        rows = self._get("/profile", key)
        if not rows:
            logger.warning("FMP: no profile data for %s (may be paywalled)", key)
            return {}

        p            = rows[0]
        country_code = (p.get("country") or "").upper().strip()
        country      = COUNTRY_CODES.get(country_code, country_code or "Unknown")

        employees = p.get("fullTimeEmployees") or 0
        try:
            employees = int(str(employees).replace(",", "").strip())
        except (ValueError, TypeError):
            employees = 0

        result = {
            "ticker":        key,
            "name":          (p.get("companyName") or key).strip(),
            "sector":        (p.get("sector")   or "Unknown").strip() or "Unknown",
            "industry":      (p.get("industry") or "Unknown").strip() or "Unknown",
            "country":       country,
            "website":       (p.get("website")  or "").strip(),
            "exchange":      (p.get("exchangeShortName") or "").strip(),
            "exchange_full": (p.get("exchangeFullName")  or "").strip(),
            "currency":      (p.get("currency") or "USD").strip(),
            "market_cap":    int(p.get("mktCap") or 0),
            "description":   (p.get("description") or "")[:400].strip(),
            "employees":     employees,
            "source":        "fmp",
        }
        self._profile_cache.set("fmp_profile:" + key, result)
        return result

    # ── Quote ─────────────────────────────────────────────────────────────────
    def get_quote(self, symbol):
        """
        Return {ticker, last_price, prev_close, change_pct, currency} or {} on failure.
        change_pct is already in percent (e.g. 1.5 means +1.5%).
        """
        key    = symbol.upper()
        cached = self._quote_cache.get("fmp_quote:" + key)
        if cached:
            return cached

        rows = self._get("/quote", key)
        if not rows:
            logger.warning("FMP: no quote data for %s", key)
            return {}

        q = rows[0]
        try:
            last  = round(float(q.get("price")             or 0), 2)
            prev  = round(float(q.get("previousClose")      or 0), 2)
            chpct = round(float(q.get("changesPercentage")  or 0), 2)
        except (TypeError, ValueError):
            return {}

        result = {
            "ticker":     key,
            "last_price": last,
            "prev_close": prev,
            "change_pct": chpct,
            "currency":   (q.get("currency") or "USD").strip(),
        }
        self._quote_cache.set("fmp_quote:" + key, result)
        return result
