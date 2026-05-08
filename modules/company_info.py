"""
Fetches company metadata via FMP (Financial Modeling Prep).
Search flow:
  1. FMP search-symbol (free tier) — resolves ticker to name + exchange
  2. FMP profile (bonus; may be paywalled on free tier)
  3. Tavily web search fallback if name is still a placeholder
  4. LLM-derived industry from description (always overrides FMP industry)
"""
import logging
import re
from .cache_manager import CacheManager

logger = logging.getLogger(__name__)

_SKIP_NAMES = {
    "london stock exchange", "new york stock exchange", "nasdaq", "euronext",
    "yahoo finance", "google finance", "bloomberg", "reuters", "marketwatch",
    "investing.com", "share price", "stock price", "stock analysis", "cnbc",
    "financial times", "wall street journal", "morningstar", "seeking alpha",
    "motley fool", "zacks", "barron", "simply wall st", "macrotrends",
    "annual report", "investor relations", "press release",
}

_CORP_RE = re.compile(
    r'\b(plc|PLC|Inc\.?|Ltd\.?|Limited|Corp\.?|Corporation|Group|Holdings?'
    r'|AG|SA|NV|SE|GmbH|AB|ASA|Oyj|SPA|S\.A\.|N\.V\.)\b'
)


def _empty_info(ticker, error=""):
    return {
        "ticker": ticker.upper(), "name": ticker,
        "sector": "Unknown", "industry": "Unknown", "country": "Unknown",
        "website": "", "exchange": "", "exchange_full": "", "currency": "USD",
        "market_cap": 0, "description": "", "employees": 0,
        "source": "fallback", "error": error,
    }


def _is_skip(name):
    return any(s in name.lower() for s in _SKIP_NAMES)


def _extract_name_from_tavily(results, ticker_upper):
    ticker_base = ticker_upper.split(".")[0]
    candidates  = []
    for r in results[:6]:
        title = (r.get("title") or "").strip()
        if not title:
            continue
        for pat in [
            r'([A-Z][^(\n]{2,60}?)\s*\(\s*' + re.escape(ticker_upper) + r'\s*\)',
            r'([A-Z][^(\n]{2,60}?)\s*\(\s*' + re.escape(ticker_base)  + r'\s*\)',
        ]:
            m = re.search(pat, title, re.IGNORECASE)
            if m:
                name = m.group(1).strip().rstrip(" -|")
                if len(name) > 2 and not _is_skip(name):
                    candidates.append((10, name))
                    break
        for seg in re.split(r"[|\-–—·]", title):
            seg = seg.strip()
            if len(seg) < 4 or len(seg) > 70 or _is_skip(seg):
                continue
            if seg.upper() in (ticker_upper, ticker_base):
                continue
            weight = 3 if _CORP_RE.search(seg) else 1
            candidates.append((weight, seg))
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]


class CompanyInfoFetcher:
    def __init__(self, tavily_client=None, fmp_client=None, groq_client=None):
        self._tavily = tavily_client
        self._fmp    = fmp_client
        self._groq   = groq_client
        self._cache  = CacheManager(ttl=86400)

    # ── Candidate search (for disambiguation UI) ──────────────────────────────
    def get_candidates(self, query):
        """
        Search for matching companies using FMP search-symbol (free tier).
        Returns list of {symbol, name, exchange, exchange_full, currency}.
        Returns [] if FMP unavailable or no results.
        """
        if not self._fmp:
            return []
        try:
            results = self._fmp.search_symbol(query)
            return [
                {
                    "symbol":        r["symbol"],
                    "name":          r["name"],
                    "exchange":      r.get("exchange", ""),
                    "exchange_full": r.get("exchange_full", ""),
                    "currency":      r.get("currency", ""),
                }
                for r in results[:10]
                if r.get("symbol")
            ]
        except Exception as exc:
            logger.warning("get_candidates failed for '%s': %s", query, exc)
            return []

    # ── Main info fetch ───────────────────────────────────────────────────────
    def get_info(self, ticker_or_name):
        key    = ticker_or_name.strip().upper()
        cached = self._cache.get("company:" + key)
        if cached:
            return cached

        info = _empty_info(key)

        if self._fmp:
            # 1a. search-symbol → always works on free tier
            candidates = self._fmp.search_symbol(key)
            if candidates:
                # Prefer exact match, fall back to first result
                best = next(
                    (c for c in candidates if c["symbol"].upper() == key),
                    candidates[0],
                )
                info.update({
                    "ticker":        best["symbol"],
                    "name":          best["name"],
                    "exchange":      best.get("exchange", ""),
                    "exchange_full": best.get("exchange_full", ""),
                    "currency":      best.get("currency", "USD") or "USD",
                    "source":        "fmp_search",
                })
                logger.info("FMP search-symbol for %s → %s (%s)",
                            key, best["symbol"], best["name"])

            # 1b. Profile endpoint (may be paywalled on free tier — handle silently)
            symbol_to_fetch = info.get("ticker", key)
            profile = self._fmp.get_profile(symbol_to_fetch)
            if profile:
                # Merge: profile wins for numeric/detail fields; keep search name if profile name is placeholder
                for field in ["sector", "industry", "country", "website",
                              "market_cap", "description", "employees",
                              "exchange", "exchange_full", "currency"]:
                    val = profile.get(field)
                    if val and val != "Unknown":
                        info[field] = val
                if profile.get("name") and profile["name"] != symbol_to_fetch:
                    info["name"] = profile["name"]
                info["source"] = "fmp"
                logger.info("FMP profile for %s: name='%s'", key, info["name"])

        # 2. Name still a placeholder? Try Tavily
        name_is_placeholder = (
            info.get("name", key) == key
            or info.get("name", key) == ticker_or_name.strip()
        )
        if name_is_placeholder and self._tavily:
            logger.info("FMP name placeholder for %s — trying Tavily", key)
            info = self._tavily_lookup(ticker_or_name.strip(), key, info)

        # 3. LLM-derived industry from description
        description = info.get("description", "")
        if description and self._groq:
            derived = self._derive_industry(description, info.get("name", key))
            if derived and derived != "Unknown":
                info["industry"] = derived
                logger.info("LLM industry for %s: '%s'", key, derived)

        # 4. Light Tavily enrichment if sector/country still unknown
        elif self._tavily and self._needs_enrichment(info):
            info = self._tavily_enrich(ticker_or_name.strip(), info)

        self._cache.set("company:" + key, info)
        return info

    # ── LLM industry derivation ───────────────────────────────────────────────
    def _derive_industry(self, description, name):
        prompt = (
            "From the company description below, identify the industry in 2-5 words.\n"
            "Reply with ONLY the industry name — no punctuation, no explanation.\n\n"
            "Description: " + description[:400]
        )
        try:
            raw = self._groq.query(
                prompt,
                system_prompt="Reply with only the industry name (2-5 words).",
                max_tokens=20,
            )
            industry = raw.strip().strip('"').strip("'").rstrip(".")
            if industry and len(industry) < 80 and "\n" not in industry:
                return industry
        except Exception as exc:
            logger.warning("Industry LLM failed for %s: %s", name, exc)
        return "Unknown"

    # ── Tavily fallback (no FMP data) ─────────────────────────────────────────
    def _tavily_lookup(self, raw_input, key, existing):
        try:
            query   = '"' + raw_input + '" stock company profile'
            result  = self._tavily.search(query, max_results=6, search_depth="basic")
            results = result.get("results", [])
            name    = _extract_name_from_tavily(results, key)
            country = existing.get("country", "Unknown")
            snippets = " ".join(r.get("content", "") for r in results)[:2000]
            if country == "Unknown":
                for c in ["United States", "United Kingdom", "France", "Germany",
                          "Japan", "China", "Canada", "Australia", "India",
                          "Netherlands", "Switzerland", "Sweden", "Denmark",
                          "Singapore", "South Korea", "Brazil", "Spain", "Italy",
                          "Norway", "Finland", "Belgium", "Ireland", "Poland"]:
                    if c.lower() in snippets.lower():
                        country = c
                        break
            update = {"country": country, "source": "tavily_fallback"}
            if name:
                update["name"] = name
            return {**existing, **update}
        except Exception as exc:
            logger.warning("Tavily lookup failed for '%s': %s", raw_input, exc)
            return existing

    @staticmethod
    def _needs_enrichment(info):
        return (
            info.get("sector",   "Unknown") == "Unknown"
            and info.get("country", "Unknown") == "Unknown"
        )

    def _tavily_enrich(self, raw_input, existing):
        try:
            query    = raw_input + " company sector industry headquarters"
            result   = self._tavily.search(query, max_results=3, search_depth="basic")
            snippets = " ".join(r.get("content", "") for r in result.get("results", []))[:2000]
            for c in ["United States", "United Kingdom", "France", "Germany",
                      "Japan", "China", "Canada", "Australia", "India"]:
                if c.lower() in snippets.lower():
                    existing["country"] = c
                    break
        except Exception as exc:
            logger.warning("Tavily enrich failed for '%s': %s", raw_input, exc)
        return existing
