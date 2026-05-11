"""
News searcher — company news only (2 targeted searches per company).
Removed: Industry news, Country/macro, Regulatory/competitive.

Results are filtered: articles that don't mention the company name
or ticker anywhere in title + content are discarded before returning.
"""
import json
import re
import logging
from datetime import datetime

logger       = logging.getLogger(__name__)
CURRENT_YEAR = datetime.now().year
RESULTS_PER_QUERY = 5


# Legal suffixes stripped before building mention terms — mirrors company_info.py
# so the filter works correctly even if cached info still carries a suffix.
_SUFFIX_STRIP_RE = re.compile(
    r'[,\s]+'
    r'('
    r'Incorporated|Corporation|Limited Liability Company|Limited Partnership'
    r'|Public Limited Company'
    r'|Inc\.?|Corp\.|Ltd\.?|Limited|PLC|plc|LLC|LP|LLP'
    r'|GmbH|AG|SA|NV|SE|AB|ASA|Oyj|SPA|S\.A\.|N\.V\.'
    r'|Berhad|Bhd\.?'
    r')'
    r'\.?\s*$',
    re.IGNORECASE,
)
_TRAILING_PUNCT_RE = re.compile(r'[,.\s]+$')

_CORP_STOP = {
    "the", "and", "of", "for",
    "inc", "ltd", "plc", "corp", "llc", "llp", "lp",
    "group", "holdings", "holding", "limited", "corporation",
    "nv", "ag", "sa", "ab", "se", "gmbh", "oyj", "asa",
    "berhad", "bhd",
}


def _clean_name(raw):
    """Strip trailing legal suffix and punctuation from a company name."""
    if not raw:
        return raw
    cleaned = _SUFFIX_STRIP_RE.sub("", raw.strip())
    return _TRAILING_PUNCT_RE.sub("", cleaned).strip() or raw.strip()


def _build_mention_terms(info):
    """
    Return a set of lowercase strings, any of which must appear in
    title+content for the article to be kept.

    Uses the CLEANED name (legal suffix stripped) so that articles which
    write "SG Finserve" are not dropped just because company_info stored
    "SG Finserve Ltd." (e.g. from a stale cache entry).

    Produces:
      - cleaned full name  (e.g. "sg finserve")
      - each meaningful word of the name ≥ 4 chars  (e.g. "finserve")
      - base ticker symbol ≥ 3 chars  (e.g. "sgfin")
    """
    raw_name = (info.get("name") or "").strip()
    name     = _clean_name(raw_name)          # strip suffix before matching
    ticker   = (info.get("ticker") or "").strip()

    terms = set()
    if name:
        terms.add(name.lower())
        for word in re.split(r"[\s\-&./,]+", name):
            w = word.strip().lower()
            if len(w) >= 4 and w not in _CORP_STOP:
                terms.add(w)
    if ticker:
        base = ticker.split(".")[0].lower()
        if len(base) >= 3:
            terms.add(base)

    return terms


def _mentions_company(article, mention_terms):
    """
    Return True if the article title or content contains at least one
    of the mention_terms (case-insensitive).
    """
    haystack = (
        (article.get("title")   or "") + " " +
        (article.get("content") or "")
    ).lower()
    return any(term in haystack for term in mention_terms)


class NewsSearcher:
    def __init__(self, tavily_client, groq_client):
        self.tavily = tavily_client
        self.groq   = groq_client

    def _generate_queries(self, info):
        name   = info.get("name",   "Unknown")
        yr     = str(CURRENT_YEAR)

        prompt = (
            "Generate 2 news search queries for investment research on this company.\n"
            "Company: " + name + "\n\n"
            "Query 1: Recent news, earnings, results, management, strategy for " + name + "\n"
            "Query 2: Recent contracts, partnerships, acquisitions, or product news for " + name + "\n\n"
            "Return ONLY a JSON array of 2 short search strings. "
            "Each query MUST include the year " + yr + ". "
            'Example: ["q1","q2"]'
        )
        try:
            raw = self.groq.query(
                prompt,
                system_prompt="Return only a valid JSON array of 2 strings. No other text.",
                max_tokens=120,
            )
            match = re.search(r"\[[\s\S]*?\]", raw)
            if match:
                queries = json.loads(match.group())
                if isinstance(queries, list) and len(queries) >= 1:
                    fixed = []
                    for q in queries[:2]:
                        q = str(q)
                        if str(CURRENT_YEAR) not in q:
                            q = q.replace("2025", str(CURRENT_YEAR))
                        if str(CURRENT_YEAR) not in q:
                            q = q + " " + str(CURRENT_YEAR)
                        fixed.append(q)
                    return fixed
        except Exception as exc:
            logger.warning("Query generation failed: %s", exc)

        # Fallback
        yr = str(CURRENT_YEAR)
        return [
            name + " news earnings results " + yr,
            name + " contracts partnerships acquisitions " + yr,
        ]

    def search_news(self, company_info, progress_cb=None, days_back=30, exclude_urls=None):
        """Returns (articles_list, queries_list)."""
        exclude_urls  = exclude_urls or set()
        mention_terms = _build_mention_terms(company_info)
        name          = _clean_name(company_info.get("name", "company"))

        if progress_cb:
            progress_cb("Generating company news queries for " + name + "...")

        queries = self._generate_queries(company_info)

        if progress_cb:
            progress_cb("Running " + str(len(queries)) + " company news searches (last " + str(days_back) + " days)...")

        all_articles = []
        seen_urls    = set()
        dropped      = 0

        for idx, query in enumerate(queries):
            tag = "Company news"
            if progress_cb:
                progress_cb("[" + tag + "] " + query)
            try:
                data = self.tavily.search_news(query, max_results=RESULTS_PER_QUERY, days=days_back)
                for r in data.get("results", []):
                    url = r.get("url", "")
                    if url in seen_urls or url in exclude_urls:
                        continue
                    seen_urls.add(url)

                    article = {
                        "title":          r.get("title", ""),
                        "url":            url,
                        "content":        (r.get("content", "") or "")[:800],
                        "published_date": r.get("published_date", ""),
                        "source":         url.split("/")[2] if url else "",
                        "query":          query,
                        "category":       tag,
                        "score":          r.get("score", 0),
                    }

                    # ── Mention filter: drop articles with no company reference ──
                    if mention_terms and not _mentions_company(article, mention_terms):
                        dropped += 1
                        logger.info(
                            "News mention-filter DROPPED (no mention of %s): %s",
                            name, article["title"][:80],
                        )
                        continue

                    all_articles.append(article)
            except Exception as exc:
                logger.warning("News search failed for '%s': %s", query, exc)

        if dropped:
            logger.info(
                "News mention-filter: dropped %d article(s) with no mention of %s", dropped, name
            )
            if progress_cb:
                progress_cb(
                    "   ⚠️ " + str(dropped) + " unrelated article(s) removed (no mention of " + name + ")."
                )

        return all_articles, queries

    def search_custom(self, queries, days_back=30, exclude_urls=None):
        """
        Run user-supplied custom search strings.
        Returns list of article dicts with category='Custom'.
        """
        exclude_urls = exclude_urls or set()
        all_articles = []
        seen_urls    = set()

        for query in queries:
            try:
                data = self.tavily.search_news(query, max_results=RESULTS_PER_QUERY, days=days_back)
                for r in data.get("results", []):
                    url = r.get("url", "")
                    if url in seen_urls or url in exclude_urls:
                        continue
                    seen_urls.add(url)
                    all_articles.append({
                        "title":          r.get("title", ""),
                        "url":            url,
                        "content":        (r.get("content", "") or "")[:800],
                        "published_date": r.get("published_date", ""),
                        "source":         url.split("/")[2] if url else "",
                        "query":          query,
                        "category":       "Custom",
                        "score":          r.get("score", 0),
                    })
            except Exception as exc:
                logger.warning("Custom search failed for '%s': %s", query, exc)

        return all_articles
