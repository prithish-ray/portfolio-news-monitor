"""
Financial Modeling Prep (FMP) API client — free-tier only.
Docs: https://site.financialmodelingprep.com/developer/docs

Only /stable/search-symbol is used — confirmed free on all plans.
All paid endpoints (profile, quote, etc.) are excluded.
"""
import logging
import requests
from .cache_manager import CacheManager

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/stable"


class FMPClient:
    def __init__(self, api_key):
        self._key          = api_key
        self._search_cache = CacheManager(ttl=3600)   # 1 h
        self._sess         = requests.Session()
        self._sess.headers.update({"User-Agent": "PortfolioMonitor/1.0"})

    # ── Internal ──────────────────────────────────────────────────────────────
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

