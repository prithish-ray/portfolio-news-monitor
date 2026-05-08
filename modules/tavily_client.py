"""
Tavily search client with request caching.
News results cached 30 min; filing results 15 min.
Supports include_domains for exchange/site-scoped searches.
"""
import logging
from tavily import TavilyClient as _Tavily
from .cache_manager import CacheManager

logger = logging.getLogger(__name__)


class TavilyClient:
    def __init__(self, api_key):
        self._client        = _Tavily(api_key=api_key)
        self._cache_news    = CacheManager(ttl=1800)   # 30 min
        self._cache_filings = CacheManager(ttl=900)    # 15 min

    def search(self, query, max_results=5, search_depth="basic",
               topic="general", include_raw_content=False,
               days=7, use_cache=True, include_domains=None):
        domains_key = ",".join(sorted(include_domains or []))
        cache_key = "search:" + query + ":" + str(max_results) + ":" + search_depth + ":" + str(days) + ":" + domains_key
        cache = self._cache_filings if search_depth == "advanced" else self._cache_news

        if use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                logger.debug("Tavily cache hit: %s", query[:60])
                return cached

        kwargs = dict(
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            topic=topic,
            include_raw_content=include_raw_content,
            days=days,
        )
        if include_domains:
            kwargs["include_domains"] = include_domains

        try:
            result = self._client.search(**kwargs)
        except Exception as exc:
            logger.error("Tavily search error for '%s': %s", query, exc)
            result = {"results": [], "error": str(exc)}

        if use_cache:
            cache.set(cache_key, result)
        return result

    def search_news(self, query, max_results=5, days=7):
        """Convenience wrapper for news-topic search."""
        return self.search(query=query, max_results=max_results,
                           search_depth="basic", topic="news", days=days)

    def search_filings(self, query, max_results=5, days=7, include_domains=None):
        """Deep filing search, optionally scoped to specific domains."""
        return self.search(query=query, max_results=max_results,
                           search_depth="advanced", days=days,
                           include_domains=include_domains)
