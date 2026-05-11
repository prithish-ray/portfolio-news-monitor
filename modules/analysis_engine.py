"""
Analysis engine — structured JSON analysis with token-budget guard.
Groq free-tier dev key: 6,000 TPM limit.
Target: keep total prompt under ~4,200 estimated tokens (~1 token per 3.5 chars).
"""
import json
import re
import logging

logger = logging.getLogger(__name__)

MAX_FILINGS          = 4      # fewer filings but with full content
MAX_NEWS             = 6
MAX_DEVS             = 6
FILING_CONTENT_CHARS = 2500   # enough to hold key numbers from a PDF excerpt
NEWS_CONTENT_CHARS   = 350    # short snippets are fine for news
HARD_CTX_CHARS       = 9_000
CHARS_PER_TOK        = 3.5

# Minimum chars before we consider content "too thin" and try to enrich
_THIN_THRESHOLD = 350


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
        from .rag_processor import RAGProcessor
        # Tuned for financial docs: smaller chunks, more of them
        self._rag = RAGProcessor(chunk_size=220, overlap=40, top_k=3)

    # ── Document content enrichment ───────────────────────────────────────────
    def _enrich_filings(self, filings, name, ticker, progress_cb=None):
        """
        For each filing where content is thin (<_THIN_THRESHOLD chars) or
        the URL is a PDF (e.g. NSE archives), fetch and extract the actual
        document text via RAGProcessor so the LLM has something substantive
        to read.

        Modifies filings in-place and returns them.
        """
        query = (
            name + " " + ticker.split(".")[0]
            + " revenue profit earnings results announcement date"
        )
        enriched_count = 0

        for i, filing in enumerate(filings):
            url     = filing.get("url", "")
            content = (filing.get("content") or "").strip()

            is_pdf  = (
                url.lower().endswith(".pdf")
                or "nsearchives.nseindia.com" in url
                or "bseindia.com/xml-data" in url
                or "bseindia.com/bseplus" in url
            )
            is_thin = len(content) < _THIN_THRESHOLD

            if (is_pdf or is_thin) and url:
                try:
                    if progress_cb:
                        progress_cb(
                            "📄 Reading source document for: "
                            + filing.get("title", url)[:60] + "..."
                        )
                    fetched = self._rag.process_pdf_url(url, query)
                    if fetched and len(fetched.strip()) > len(content):
                        filing["content"] = fetched
                        enriched_count += 1
                        logger.info(
                            "Enriched filing %d: fetched %d chars from %s",
                            i + 1, len(fetched), url[:70],
                        )
                    else:
                        logger.debug(
                            "Filing %d: fetch yielded no improvement (%s)", i + 1, url[:70]
                        )
                except Exception as exc:
                    logger.debug("Filing content fetch failed for %s: %s", url[:60], exc)

        if enriched_count and progress_cb:
            progress_cb(
                "   ✅ Read full content for " + str(enriched_count) + " filing(s)."
            )

        return filings

    # ── Relevance filter ──────────────────────────────────────────────────────
    def _filter_relevant(self, items, name, ticker, item_type, max_retries=3):
        """
        LLM gate: keep only items that clearly pertain to `name` / `ticker`.
        Uses an agentic retry loop — retries up to max_retries times on parse
        failure before falling back to the unfiltered list (fail-safe).

        item_type: 'filing' or 'news'
        Returns filtered list.
        """
        if not items:
            return items

        prefix = "F" if item_type == "filing" else "N"
        lines  = []
        for i, item in enumerate(items):
            ref     = prefix + str(i + 1)
            title   = (item.get("title")   or "").strip()[:120]
            snippet = (item.get("content") or "").strip()[:80]
            source  = (item.get("source")  or "").strip()
            lines.append(f"[{ref}] {title}  |  {snippet}  ({source})")

        base_ticker = ticker.split(".")[0].upper()
        prompt = (
            f"Company: {name}  |  Ticker: {ticker}  |  Base symbol: {base_ticker}\n\n"
            f"The {item_type} results below were retrieved for this company. "
            f"Some may belong to a DIFFERENT company with a similar name or ticker.\n\n"
            f"Rules:\n"
            f"- KEEP an item if it is about {name} ({ticker}) or is clearly relevant.\n"
            f"- KEEP an item if you are unsure — only remove if you are CERTAIN it is about another company.\n"
            f"- Return ONLY a JSON array of the reference labels to keep, e.g. [\"F1\",\"F3\"].\n"
            f"- If all items are relevant, return all labels.\n\n"
            + "\n".join(lines)
        )

        for attempt in range(1, max_retries + 1):
            try:
                raw = self.groq.query(
                    prompt,
                    system_prompt=(
                        "You are a financial data quality checker. "
                        "Return ONLY a JSON array of reference labels. No explanation."
                    ),
                    max_tokens=150,
                    use_cache=False,
                )
                raw = raw.strip()
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$",          "", raw.strip())

                relevant_refs = json.loads(raw)
                if not isinstance(relevant_refs, list):
                    raise ValueError("LLM did not return a list")

                ref_set  = set(str(r).strip() for r in relevant_refs)
                filtered = []
                for i, item in enumerate(items):
                    ref = prefix + str(i + 1)
                    if ref in ref_set:
                        filtered.append(item)
                    else:
                        logger.info(
                            "Relevance filter REMOVED %s [%s]: %s",
                            item_type, ref, (item.get("title") or "")[:80],
                        )

                kept    = len(filtered)
                dropped = len(items) - kept
                logger.info(
                    "Relevance filter for %s (%s): kept %d / %d (dropped %d)",
                    name, item_type, kept, len(items), dropped,
                )
                return filtered

            except Exception as exc:
                logger.warning(
                    "Relevance filter attempt %d/%d failed for %s (%s): %s",
                    attempt, max_retries, name, item_type, exc,
                )
                if attempt == max_retries:
                    logger.warning(
                        "Relevance filter giving up — returning all %d %s items for %s",
                        len(items), item_type, name,
                    )
                    return items

        return items   # unreachable but satisfies linter

    def _build_filings_ctx(self, filings):
        """Returns (context_text, url_map) where url_map is {ref: (url, meta_date)}.
        meta_date is the search-index date — the LLM should prefer dates found
        inside the document content itself."""
        if not filings:
            return "None found.", {}
        lines   = []
        url_map = {}
        for i, f in enumerate(filings[:MAX_FILINGS]):
            ref       = "F" + str(i + 1)
            url       = f.get("url", "")
            meta_date = f.get("published_date", "")   # search-index date, may be inaccurate
            url_map[ref] = (url, meta_date)
            content   = (f.get("content") or "").strip()[:FILING_CONTENT_CHARS]
            lines.append(
                "[" + ref + "] " + f.get("title", "(no title)")
                + " [" + f.get("source", "") + "]"
                + (" | indexed: " + meta_date if meta_date else "")
                + ("\n  URL: " + url if url else "")
                + ("\n\n" + content if content else "")
            )
        return "\n\n".join(lines), url_map

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
            content = (n.get("content") or "").strip()[:NEWS_CONTENT_CHARS]
            lines.append(
                "[" + ref + "] [" + n.get("category", "") + "] " + n.get("title", "(no title)")
                + " | " + (date or "?")
                + ("\n  " + content if content else "")
            )
        return "\n".join(lines), url_map

    def _build_prompt(self, name, ticker, info, filings_ctx, news_ctx):
        sector   = info.get("sector",   "?")
        industry = info.get("industry", "?")
        country  = info.get("country",  "?")

        p  = "You are analysing filings and news for portfolio holding " + name + " (" + ticker + ").\n"
        p += "Sector: " + sector + " | Industry: " + industry + " | Country: " + country + "\n\n"

        p += "═══ FILINGS (last 30 days) ═══\n" + filings_ctx + "\n\n"
        p += "═══ NEWS (last 30 days) ═══\n" + news_ctx + "\n\n"

        p += "ANALYSIS RULES — follow strictly:\n"
        p += "1. DATE: Extract the actual date FROM the document content (look for board meeting date,\n"
        p += "   earnings call date, filing date, record date stated inside the document).\n"
        p += "   Do NOT use the 'indexed:' metadata date — that is the search-index date, not the event date.\n"
        p += "   If no date is found in the content, leave the date field as an empty string.\n"
        p += "2. SUBSTANCE: Read the actual content of each filing. For earnings calls / transcripts,\n"
        p += "   identify specific figures: revenue, profit, margins, guidance, YoY change.\n"
        p += "   For board meetings, identify what was resolved. For results, quote key numbers.\n"
        p += "3. NO GENERIC LABELS: Do NOT write 'routine filing', 'standard disclosure', or\n"
        p += "   'transcript is routine'. Every development must cite what was actually reported.\n"
        p += "   If the content lacks detail, say what is known and note that detail is limited.\n"
        p += "4. IMPACT: Assess business impact specifically for " + name + " shareholders.\n"
        p += "   Positive = clear upside (beat estimates, new contract, raised guidance).\n"
        p += "   Negative = clear downside (miss, litigation, write-off, management departure).\n"
        p += "   Neutral = administrative or genuinely routine with no material impact.\n\n"

        p += 'Return ONLY valid JSON (no markdown fences, no extra text):\n'
        p += '{\n'
        p += '  "company": "' + name + '",\n'
        p += '  "ticker": "' + ticker + '",\n'
        p += '  "filings_summary": "one sentence summarising what the filings reveal",\n'
        p += '  "news_summary": "one sentence summarising the news picture",\n'
        p += '  "overall_sentiment": "Positive|Negative|Neutral|Mixed",\n'
        p += '  "one_line_overall": "the single most important development for ' + name + ' right now",\n'
        p += '  "overall_summary": "2-3 sentences: key facts, figures, and what they mean for shareholders",\n'
        p += '  "developments": [\n'
        p += '    {\n'
        p += '      "title": "specific headline — include figures if available",\n'
        p += '      "one_line_summary": "what actually happened, with numbers where present",\n'
        p += '      "impact": "Positive|Negative|Neutral",\n'
        p += '      "impact_explanation": "specific reason this matters for ' + name + ' shareholders",\n'
        p += '      "type": "filing|company_news|industry_news|regulatory",\n'
        p += '      "source": "publication or exchange name",\n'
        p += '      "source_ref": "F1|N2|etc — the [ref] label from the source list above",\n'
        p += '      "date": "YYYY-MM-DD extracted from document content, or empty string"\n'
        p += '    }\n'
        p += '  ]\n'
        p += '}\n'
        p += "List max " + str(MAX_DEVS) + " developments, most material first. "
        p += "Use only data from the sources above — do not invent facts."
        return p

    def analyze_company(self, company_info, filings, news, search_terms,
                        progress_cb=None):
        name   = company_info.get("name",   company_info.get("ticker", "Unknown"))
        ticker = company_info.get("ticker", "")

        filings      = filings      or []
        news         = news         or []
        search_terms = search_terms or []

        def _emit(msg):
            if progress_cb:
                progress_cb(msg)

        # ── Relevance gate: verify items actually pertain to this company ──────
        if filings:
            _emit(f"🔎 Verifying {len(filings)} filing(s) are about {name}...")
            filings = self._filter_relevant(filings, name, ticker, "filing")
            _emit(f"   ✅ {len(filings)} relevant filing(s) confirmed.")

        if news:
            _emit(f"🔎 Verifying {len(news)} news item(s) are about {name}...")
            news = self._filter_relevant(news, name, ticker, "news")
            _emit(f"   ✅ {len(news)} relevant news item(s) confirmed.")

        # ── Enrich filing content: fetch actual PDFs / documents ─────────────
        if filings:
            _emit(f"📄 Fetching source documents for {len(filings)} filing(s)...")
            filings = self._enrich_filings(filings, name, ticker, progress_cb=_emit)

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
                    "You are a senior financial analyst preparing a portfolio briefing. "
                    "Return only valid JSON — no markdown fences, no extra text. "
                    "You must cite specific figures, dates, and events from the source text. "
                    "Generic phrases like 'routine filing', 'standard disclosure', or "
                    "'transcript is routine' are FORBIDDEN — always state what was actually reported. "
                    "String values should be concise but factual (under 50 words each)."
                ),
                max_tokens=2048,
                use_cache=False,
            )

            result = _repair_json(raw)

            if result is None:
                raise ValueError("Could not parse or repair LLM JSON output")

            if "developments" not in result:
                result["developments"] = []

            # Resolve source_ref → url / date for each development.
            # Date priority: LLM-extracted date from document content  > metadata fallback.
            for dev in result["developments"]:
                ref = (dev.get("source_ref") or "").strip()
                if ref in url_map:
                    resolved_url, meta_date = url_map[ref]
                    # URL: use resolved URL if LLM didn't supply one
                    if resolved_url and not dev.get("source_url"):
                        dev["source_url"] = resolved_url
                    # Date: LLM date wins; only fall back to meta_date if LLM left it blank
                    llm_date = (dev.get("date") or "").strip()
                    if not llm_date and meta_date:
                        dev["date"] = meta_date
                        logger.debug(
                            "Dev '%s': no LLM date — using metadata date %s",
                            dev.get("title", "")[:40], meta_date,
                        )
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
