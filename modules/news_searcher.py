"""
News searcher — company news only (2 targeted searches per company).
Removed: Industry news, Country/macro, Regulatory/competitive.
"""
import json
import re
import logging
from datetime import datetime

logger       = logging.getLogger(__name__)
CURRENT_YEAR = datetime.now().year
RESULTS_PER_QUERY = 5


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

    def search_news(self, company_info, progress_cb=None, days_back=7, exclude_urls=None):
        """Returns (articles_list, queries_list)."""
        exclude_urls = exclude_urls or set()
        if progress_cb:
            progress_cb("Generating company news queries for " + company_info.get("name", "company") + "...")

        queries = self._generate_queries(company_info)

        if progress_cb:
            progress_cb("Running " + str(len(queries)) + " company news searches (last " + str(days_back) + " days)...")

        all_articles = []
        seen_urls    = set()

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
                    all_articles.append({
                        "title":          r.get("title", ""),
                        "url":            url,
                        "content":        (r.get("content", "") or "")[:800],
                        "published_date": r.get("published_date", ""),
                        "source":         url.split("/")[2] if url else "",
                        "query":          query,
                        "category":       tag,
                        "score":          r.get("score", 0),
                    })
            except Exception as exc:
                logger.warning("News search failed for '%s': %s", query, exc)

        return all_articles, queries

    def search_custom(self, queries, days_back=7, exclude_urls=None):
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
