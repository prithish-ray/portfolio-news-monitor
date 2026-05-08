"""
Analysis engine — structured JSON analysis with token-budget guard.
Groq free-tier dev key: 6,000 TPM limit.
Target: keep total prompt under ~4,200 estimated tokens (~1 token per 3.5 chars).
"""
import json
import re
import logging

logger = logging.getLogger(__name__)

MAX_FILINGS    = 5
MAX_NEWS       = 8
MAX_DEVS       = 6          # cap LLM output; keeps response well under 2048 tokens
CONTENT_CHARS  = 150
HARD_CTX_CHARS = 8_000
CHARS_PER_TOK  = 3.5


def _estimate_tokens(text):
    return int(len(text) / CHARS_PER_TOK)


def _trim(text, budget):
    if len(text) <= budget:
        return text
    return text[:budget] + "\n[...trimmed]"


def _repair_json(raw):
    """
    Best-effort repair of a truncated JSON string from the LLM.
    Strategy:
      1. Strip markdown fences.
      2. Try a straight parse — return immediately if it works.
      3. Find the last complete development object and close the JSON cleanly.
      4. Return None if nothing can be salvaged.
    """
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$",          "", raw.strip())

    # Try as-is first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try closing truncated arrays/objects by counting braces
    # Find the last fully closed development object
    # Locate the "developments" array opening
    dev_start = raw.find('"developments"')
    if dev_start == -1:
        # No developments key yet — try to at least get the header fields
        # Close with empty developments
        salvage = raw.rstrip().rstrip(',')
        try:
            return json.loads(salvage + ', "developments": []}')
        except Exception:
            pass
        return None

    # Find the '[' that opens the developments array
    arr_open = raw.find('[', dev_start)
    if arr_open == -1:
        return None

    # Walk forward collecting complete development objects
    depth       = 0
    in_string   = False
    escape_next = False
    last_good   = arr_open  # position after the last complete '}' inside the array

    i = arr_open
    obj_depth   = 0
    obj_start   = None

    while i < len(raw):
        ch = raw[i]
        if escape_next:
            escape_next = False
            i += 1
            continue
        if ch == '\\' and in_string:
            escape_next = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        if ch == '{':
            obj_depth += 1
            if obj_depth == 1:
                obj_start = i
        elif ch == '}':
            obj_depth -= 1
            if obj_depth == 0 and obj_start is not None:
                last_good = i + 1   # include the closing '}'
                obj_start = None
        i += 1

    # Rebuild: header up to developments array + valid objects + closing
    header    = raw[:arr_open + 1]               # up to and including '['
    good_devs = raw[arr_open + 1: last_good].rstrip().rstrip(',')
    candidate = header + good_devs + "]}"

    # Also need to close any missing header fields before the developments key
    # Ensure the root object is valid
    try:
        return json.loads(candidate)
    except Exception:
        pass

    return None


class AnalysisEngine:
    def __init__(self, groq_client):
        self.groq = groq_client

    def _build_filings_ctx(self, filings):
        """Returns (context_text, url_map) where url_map is {ref: (url, date)}."""
        if not filings:
            return "None found.", {}
        lines   = []
        url_map = {}
        for i, f in enumerate(filings[:MAX_FILINGS]):
            ref  = "F" + str(i + 1)
            url  = f.get("url", "")
            date = f.get("published_date", "")
            url_map[ref] = (url, date)
            lines.append(
                "[" + ref + "] " + f.get("title", "(no title)") + " [" + f.get("source", "") + "]"
                + " | " + (date or "?")
                + ("\n  URL: " + url if url else "")
                + "\n  " + (f.get("content") or "")[:CONTENT_CHARS]
            )
        return "\n".join(lines), url_map

    def _build_news_ctx(self, news):
        """Returns (context_text, url_map) where url_map is {ref: (url, date)}."""
        if not news:
            return "None found.", {}
        lines   = []
        url_map = {}
        for i, n in enumerate(news[:MAX_NEWS]):
            ref  = "N" + str(i + 1)
            url  = n.get("url", "")
            date = n.get("published_date", "")
            url_map[ref] = (url, date)
            lines.append(
                "[" + ref + "] [" + n.get("category", "") + "] " + n.get("title", "(no title)")
                + " | " + (date or "?")
                + ("\n  URL: " + url if url else "")
                + "\n  " + (n.get("content") or "")[:CONTENT_CHARS]
            )
        return "\n".join(lines), url_map

    def _build_prompt(self, name, ticker, info, filings_ctx, news_ctx):
        sector   = info.get("sector",   "?")
        industry = info.get("industry", "?")
        country  = info.get("country",  "?")
        p  = "Analyse filings and news for portfolio holding " + name + " (" + ticker + ").\n"
        p += "Sector: " + sector + " | Industry: " + industry + " | Country: " + country + "\n\n"
        p += "FILINGS (7 days):\n" + filings_ctx + "\n\n"
        p += "NEWS (7 days):\n" + news_ctx + "\n\n"
        p += 'Return ONLY valid JSON (no markdown fences):\n'
        p += '{\n'
        p += '  "company": "' + name + '",\n'
        p += '  "ticker": "' + ticker + '",\n'
        p += '  "filings_summary": "one sentence",\n'
        p += '  "news_summary": "one sentence",\n'
        p += '  "overall_sentiment": "Positive|Negative|Neutral|Mixed",\n'
        p += '  "one_line_overall": "one tight sentence — the single most important thing happening with ' + name + ' right now",\n'
        p += '  "overall_summary": "2-3 sentence executive overview",\n'
        p += '  "developments": [\n'
        p += '    {\n'
        p += '      "title": "headline",\n'
        p += '      "one_line_summary": "what happened",\n'
        p += '      "impact": "Positive|Negative|Neutral",\n'
        p += '      "impact_explanation": "why it matters for ' + name + '",\n'
        p += '      "type": "filing|company_news|industry_news|regulatory",\n'
        p += '      "source": "publication",\n'
        p += '      "source_ref": "F1|N1|etc — the [ref] label of the source item",\n'
        p += '      "date": "YYYY-MM-DD or empty string"\n'
        p += '    }\n'
        p += '  ]\n'
        p += '}\n'
        p += "List max " + str(MAX_DEVS) + " developments. Use only data from above. Keep each field concise. "
        p += "For source_ref, use the bracketed label (e.g. F1, N2) of the item this development comes from."
        return p

    def analyze_company(self, company_info, filings, news, search_terms):
        name   = company_info.get("name",   company_info.get("ticker", "Unknown"))
        ticker = company_info.get("ticker", "")

        filings      = filings      or []
        news         = news         or []
        search_terms = search_terms or []

        filings_ctx, f_url_map = self._build_filings_ctx(filings)
        news_ctx,    n_url_map = self._build_news_ctx(news)
        url_map = {**f_url_map, **n_url_map}   # "F1" / "N2" → (url, date)

        filings_ctx = _trim(filings_ctx, HARD_CTX_CHARS // 2)
        news_ctx    = _trim(news_ctx,    HARD_CTX_CHARS // 2)

        prompt = self._build_prompt(name, ticker, company_info, filings_ctx, news_ctx)
        est    = _estimate_tokens(prompt)
        logger.info("Analysis prompt for %s: ~%d tokens", name, est)

        if est > 4200:
            logger.warning("Prompt too large (%d tokens) — trimming further", est)
            filings_ctx = _trim(filings_ctx, 1200)
            news_ctx    = _trim(news_ctx,    1500)
            prompt      = self._build_prompt(name, ticker, company_info, filings_ctx, news_ctx)
            logger.info("Re-trimmed prompt: ~%d tokens", _estimate_tokens(prompt))

        try:
            raw = self.groq.query(
                prompt,
                system_prompt=(
                    "You are a financial analyst. "
                    "Return only valid JSON, no markdown fences, no extra text. "
                    "Keep all string values concise (under 40 words each)."
                ),
                max_tokens=2048,
                use_cache=False,
            )

            result = _repair_json(raw)

            if result is None:
                raise ValueError("Could not parse or repair LLM JSON output")

            if "developments" not in result:
                result["developments"] = []

            # Resolve source_ref → url / date for each development
            for dev in result["developments"]:
                ref = (dev.get("source_ref") or "").strip()
                if ref in url_map:
                    resolved_url, resolved_date = url_map[ref]
                    if resolved_url and not dev.get("source_url"):
                        dev["source_url"] = resolved_url
                    if resolved_date and not dev.get("date"):
                        dev["date"] = resolved_date
                # Clean up the ref key — frontend doesn't need it
                dev.pop("source_ref", None)

            result["search_terms"]      = search_terms
            result["raw_filings_count"] = len(filings)
            result["raw_news_count"]    = len(news)
            result["company_info"]      = company_info
            return result

        except Exception as e:
            logger.error("Analysis error for %s: %s", name, e)

        return {
            "company": name, "ticker": ticker,
            "filings_summary": "Analysis could not be completed.",
            "news_summary":    "Analysis could not be completed.",
            "overall_sentiment": "Neutral",
            "overall_summary": "An error occurred during analysis. Please retry.",
            "developments": [],
            "search_terms": search_terms,
            "raw_filings_count": len(filings),
            "raw_news_count":    len(news),
            "company_info": company_info,
            "error": True,
        }
