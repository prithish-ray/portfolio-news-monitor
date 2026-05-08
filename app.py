"""
AI Portfolio News Monitor — Flask Application
Routes:
  GET  /                      main UI
  POST /analyze               kick off analysis, returns {session_id}
  GET  /stream/<sid>          SSE progress + results
  POST /resolve/<sid>         resolve a company-choice prompt (returns {ok})
  GET  /download/<sid>        download cumulative JSON report
  GET  /tts/<sid>             text-to-speech portfolio summary (WAV)
  GET  /quote/<ticker>        real-time % day change (cached 5 min)
  GET  /health                Render health-check
"""
import json
import logging
import os
import queue
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone, timedelta

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY   = os.environ.get("GROQ_API_KEY",   "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")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY",  "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")
FMP_API_KEY    = os.environ.get("FMP_API_KEY",     "YdT04xUEz5iC9XmYAiaroAGviY01xbxB")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

_sse_queues    = {}   # session_id -> queue.Queue (SSE events)
_reports       = {}   # session_id -> report data
_choice_queues = {}   # session_id -> queue.Queue (user disambiguation choices)
_lock = threading.Lock()

MODELS = [
    {"id": "llama-3.1-8b-instant",    "label": "LLaMA 3.1 8B — Fast (default)", "default": True},
    {"id": "llama-3.3-70b-versatile", "label": "LLaMA 3.3 70B — Versatile"},
    {"id": "openai/gpt-oss-20b",      "label": "GPT-OSS 20B"},
    {"id": "openai/gpt-oss-120b",     "label": "GPT-OSS 120B — Most Powerful"},
]
MAX_COMPANIES = 3


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", models=MODELS)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})


@app.route("/quote/<ticker>")
def get_quote(ticker):
    """Return % day change for a ticker via FMP. Cached 5 min."""
    from modules.fmp_client import FMPClient
    fmp  = FMPClient(FMP_API_KEY)
    data = fmp.get_quote(ticker.upper())
    if data:
        return jsonify(data)
    return jsonify({"ticker": ticker.upper(), "error": "unavailable", "change_pct": None})


@app.route("/analyze", methods=["POST"])
def start_analysis():
    body = request.get_json(force=True, silent=True) or {}
    companies = [c.strip() for c in body.get("companies", []) if c.strip()][:MAX_COMPANIES]
    model     = body.get("model", "llama-3.1-8b-instant")

    search_exchange_filings = bool(body.get("search_exchange_filings", True))
    search_company_news     = bool(body.get("search_company_news",     True))
    search_industry_news    = bool(body.get("search_industry_news",    False))
    search_custom           = bool(body.get("search_custom",           False))
    custom_searches         = [s.strip() for s in body.get("custom_searches", []) if s.strip()]

    existing_report = body.get("existing_report")  # may be None

    if not companies:
        return jsonify({"error": "Please provide at least one company."}), 400

    any_source = (search_exchange_filings or search_company_news or
                  search_industry_news or (search_custom and custom_searches))
    if not any_source:
        return jsonify({"error": "Please select at least one search area."}), 400

    session_id = str(uuid.uuid4())
    q = queue.Queue()
    with _lock:
        _sse_queues[session_id] = q
        _reports[session_id]    = {"status": "running"}

    t = threading.Thread(
        target=_run_analysis,
        args=(session_id, companies, model,
              search_exchange_filings,
              search_company_news, search_industry_news, search_custom, custom_searches,
              existing_report, q),
        daemon=True,
    )
    t.start()
    return jsonify({"session_id": session_id})


@app.route("/resolve/<session_id>", methods=["POST"])
def resolve_choice(session_id):
    """Frontend posts the user's chosen symbol here to unblock the analysis thread."""
    body   = request.get_json(force=True, silent=True) or {}
    symbol = body.get("symbol", "").strip()
    with _lock:
        cq = _choice_queues.get(session_id)
    if cq and symbol:
        cq.put(symbol)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "No pending choice for this session"}), 404


@app.route("/stream/<session_id>")
def sse_stream(session_id):
    def generate():
        q = _sse_queues.get(session_id)
        if q is None:
            yield _sse({"type": "error", "data": {"message": "Session not found."}})
            return
        while True:
            try:
                event = q.get(timeout=45)
            except queue.Empty:
                yield _sse({"type": "heartbeat"})
                continue
            if event is None:
                yield _sse({"type": "stream_end"})
                break
            yield _sse(event)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/download/<session_id>")
def download_report(session_id):
    report = _reports.get(session_id)
    if not report or report.get("status") != "complete":
        return jsonify({"error": "Report not ready or not found."}), 404
    filename = "portfolio-report-" + session_id[:8] + ".json"
    return Response(
        json.dumps(report["data"], indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="' + filename + '"'},
    )


@app.route("/tts/<session_id>")
def tts_summary(session_id):
    """
    Generate and stream a spoken portfolio summary using Groq TTS.
    Combines each company's one_line_overall into a short briefing.
    """
    report = _reports.get(session_id)
    if not report or report.get("status") != "complete":
        return jsonify({"error": "Report not ready or not found."}), 404

    data    = report["data"]
    results = data.get("results", [])

    # Build one-sentence-per-company briefing
    lines = []
    for r in results:
        analysis = r.get("analysis", {})
        sentence = (
            analysis.get("one_line_overall") or
            analysis.get("overall_summary",  "") or ""
        ).strip()
        # Trim to first sentence if overall_summary was used as fallback
        if sentence:
            first = re.split(r'(?<=[.!?])\s+', sentence)
            lines.append(first[0])

    if not lines:
        return jsonify({"error": "No summaries available yet."}), 404

    briefing = (
        "Here is your portfolio briefing. "
        + "  ".join(lines)
        + "  That's all for now."
    )

    try:
        from modules.groq_client import GroqClient
        groq = GroqClient(GROQ_API_KEY)
        audio_bytes = groq.text_to_speech(briefing, voice="tara")
        return Response(
            audio_bytes,
            mimetype="audio/wav",
            headers={"Cache-Control": "no-cache"},
        )
    except Exception as exc:
        logger.error("TTS error: %s", exc)
        return jsonify({"error": "TTS failed: " + str(exc)}), 500


# ── Helpers ──────────────────────────────────────────────────────────────────

def _emit(q, event_type, data):
    q.put({"type": event_type, "data": data})

def _sse(payload):
    return "data: " + json.dumps(payload) + "\n\n"

def _parse_cutoff(existing_report):
    """Return (cutoff_datetime, days_back) from an uploaded report, or defaults."""
    if existing_report:
        gen_at = existing_report.get("generated_at", "")
        try:
            cutoff = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
            days_back = max(1, (datetime.now(timezone.utc) - cutoff).days + 1)
            return cutoff, days_back
        except Exception:
            pass
    return datetime.now(timezone.utc) - timedelta(days=7), 7

def _existing_for_ticker(existing_report, ticker, raw_input):
    """Find a company's existing result block by ticker or input string."""
    if not existing_report:
        return None
    for r in existing_report.get("results", []):
        r_ticker = r.get("company_info", {}).get("ticker", "").upper()
        r_input  = r.get("company_input", "").upper()
        if r_ticker == ticker.upper() or r_input == raw_input.upper():
            return r
    return None

def _existing_urls(existing_co):
    """Collect all source URLs already in a previous result block."""
    urls = set()
    if not existing_co:
        return urls
    for src_list in existing_co.get("sources", {}).values():
        for s in src_list:
            if s.get("url"):
                urls.add(s["url"])
    return urls

def _merge_developments(existing_devs, new_devs):
    """Append new developments that don't duplicate existing titles."""
    existing_titles = {d.get("title", "").lower()[:70] for d in existing_devs}
    truly_new = [d for d in new_devs if d.get("title", "").lower()[:70] not in existing_titles]
    return existing_devs + truly_new

def _source_summary(source):
    """
    Extract the first meaningful sentence from content as a display summary.
    Falls back to title if content is empty.
    """
    content = (source.get("content", "") or "").strip()
    if not content:
        title = (source.get("title", "") or "").strip()
        # Clean up common title noise like "[PDF]" prefix
        title = re.sub(r'^\[PDF\]\s*', '', title, flags=re.IGNORECASE)
        return title[:150]
    # Find first sentence
    for sep in [". ", ".\n", "! ", "? "]:
        idx = content.find(sep)
        if 20 < idx < 220:
            return content[:idx + 1].strip()
    # No clean sentence boundary — use first 150 chars
    snippet = content[:150].strip()
    return snippet + ("…" if len(content) > 150 else "")

def _wait_for_choice(session_id, timeout=120):
    """Block until user resolves a company disambiguation, or timeout."""
    cq = queue.Queue()
    with _lock:
        _choice_queues[session_id] = cq
    try:
        return cq.get(timeout=timeout)
    except queue.Empty:
        return None
    finally:
        with _lock:
            _choice_queues.pop(session_id, None)


# ── Background worker ─────────────────────────────────────────────────────────

def _run_analysis(session_id, companies, model,
                  search_exchange_filings,
                  search_company_news, search_industry_news, search_custom, custom_searches,
                  existing_report, q):
    try:
        from modules.groq_client      import GroqClient
        from modules.tavily_client    import TavilyClient
        from modules.fmp_client       import FMPClient
        from modules.company_info     import CompanyInfoFetcher
        from modules.filings_searcher import FilingsSearcher
        from modules.news_searcher    import NewsSearcher
        from modules.analysis_engine  import AnalysisEngine

        groq     = GroqClient(GROQ_API_KEY, model)
        tavily   = TavilyClient(TAVILY_API_KEY)
        fmp      = FMPClient(FMP_API_KEY)
        fetcher  = CompanyInfoFetcher(tavily_client=tavily, fmp_client=fmp, groq_client=groq)
        filer    = FilingsSearcher(tavily, groq)
        newser   = NewsSearcher(tavily, groq)
        analyser = AnalysisEngine(groq)

        cutoff_dt, days_back = _parse_cutoff(existing_report)
        total       = len(companies)
        all_results = []

        _emit(q, "progress", {
            "message":    "🚀 Starting analysis for " + str(total) + " company/ies with " + model
                          + (" (incremental since " + cutoff_dt.strftime("%Y-%m-%d %H:%M UTC") + ")" if existing_report else "") + "...",
            "percentage": 0,
        })

        for idx, raw_input in enumerate(companies):
            base_pct  = int(idx / total * 100)
            step_size = int(1   / total * 100)

            # ── Step 0: Resolve company (search-symbol disambiguation) ──
            _emit(q, "progress", {
                "message":    "🔍 [" + str(idx+1) + "/" + str(total) + "] Looking up: " + raw_input,
                "percentage": base_pct + 2,
            })

            candidates = fetcher.get_candidates(raw_input)
            resolved_input = raw_input

            if len(candidates) > 1:
                # Check if there's an exact match first
                exact = [c for c in candidates if c["symbol"].upper() == raw_input.upper()]
                if exact:
                    resolved_input = exact[0]["symbol"]
                    logger.info("Exact match for %s → %s", raw_input, resolved_input)
                else:
                    # Need user to disambiguate
                    _emit(q, "need_choice", {
                        "input":      raw_input,
                        "candidates": candidates[:8],
                        "session_id": session_id,
                    })
                    _emit(q, "progress", {
                        "message":    "⏳ Multiple matches found for \"" + raw_input + "\" — waiting for your selection...",
                        "percentage": base_pct + 3,
                    })
                    chosen = _wait_for_choice(session_id, timeout=120)
                    if chosen:
                        resolved_input = chosen
                        logger.info("User chose %s for %s", chosen, raw_input)
                    else:
                        resolved_input = candidates[0]["symbol"]
                        logger.warning("Choice timeout for %s — defaulting to %s", raw_input, resolved_input)

            elif len(candidates) == 1:
                resolved_input = candidates[0]["symbol"]

            # ── Company info ──
            try:
                info = fetcher.get_info(resolved_input)
            except Exception as e:
                logger.warning("Company lookup failed for %s: %s", resolved_input, e)
                info = {"ticker": raw_input.upper(), "name": raw_input, "sector": "Unknown",
                        "industry": "Unknown", "country": "Unknown", "website": "",
                        "exchange": "", "currency": "USD", "market_cap": 0, "description": "", "employees": 0}

            _emit(q, "company_info", {"input": raw_input, "info": info})

            # Existing data for this company
            ticker    = info.get("ticker", raw_input).upper()
            existing_co   = _existing_for_ticker(existing_report, ticker, raw_input)
            excl_urls     = _existing_urls(existing_co)
            existing_devs = existing_co.get("analysis", {}).get("developments", []) if existing_co else []

            if existing_co and existing_devs:
                _emit(q, "progress", {
                    "message":    "📂 Loaded " + str(len(existing_devs)) + " prior development(s) — searching for new ones since " + cutoff_dt.strftime("%Y-%m-%d"),
                    "percentage": base_pct + 5,
                })

            filings, news, search_terms = [], [], []

            # ── Filings ──
            if search_exchange_filings:
                _emit(q, "progress", {"message": "📄 Searching filings for " + info["name"] + "...", "percentage": base_pct + int(step_size*0.20)})
                try:
                    def _fp(msg): _emit(q, "progress", {"message": "   📄 " + msg, "percentage": base_pct + int(step_size*0.25)})
                    filings = filer.search_all(
                        info, _fp, days_back=days_back, exclude_urls=excl_urls,
                        do_exchange=True,
                        do_ir=False,
                    ) or []
                    _emit(q, "progress", {"message": "   ✅ " + str(len(filings)) + " new filing(s)", "percentage": base_pct + int(step_size*0.35)})
                except Exception as e:
                    logger.error("Filings error: %s", e)

            # ── Company news ──
            if search_company_news:
                _emit(q, "progress", {"message": "📰 Searching company news for " + info["name"] + "...", "percentage": base_pct + int(step_size*0.40)})
                try:
                    def _np(msg): _emit(q, "progress", {"message": "   🔎 " + msg, "percentage": base_pct + int(step_size*0.55)})
                    news, search_terms = newser.search_news(info, _np, days_back=days_back, exclude_urls=excl_urls)
                    _emit(q, "search_terms", {"input": raw_input, "terms": search_terms})
                    _emit(q, "progress", {"message": "   ✅ " + str(len(news)) + " news article(s)", "percentage": base_pct + int(step_size*0.60)})
                except Exception as e:
                    logger.error("News error: %s", e)

            # ── Custom searches ──
            if search_custom and custom_searches:
                _emit(q, "progress", {"message": "🔍 Running " + str(len(custom_searches)) + " custom search(es)...", "percentage": base_pct + int(step_size*0.62)})
                try:
                    custom_articles = newser.search_custom(custom_searches, days_back=days_back, exclude_urls=excl_urls)
                    news.extend(custom_articles)
                    _emit(q, "progress", {"message": "   ✅ " + str(len(custom_articles)) + " custom result(s)", "percentage": base_pct + int(step_size*0.65)})
                except Exception as e:
                    logger.error("Custom search error: %s", e)

            # ── Industry news ──
            if search_industry_news:
                industry = (info.get("industry") or "").strip()
                country  = (info.get("country")  or "").strip()
                if industry and industry != "Unknown":
                    ind_query = industry + " " + country + " News 2026"
                    _emit(q, "progress", {"message": "🌐 Searching industry news: " + ind_query + "...", "percentage": base_pct + int(step_size*0.67)})
                    try:
                        ind_articles = newser.search_custom([ind_query], days_back=days_back, exclude_urls=excl_urls)
                        # Tag as industry_news so LLM can assign the right type
                        for a in ind_articles:
                            a["category"] = "industry_news"
                        news.extend(ind_articles)
                        search_terms.append(ind_query)
                        _emit(q, "progress", {"message": "   ✅ " + str(len(ind_articles)) + " industry article(s)", "percentage": base_pct + int(step_size*0.70)})
                    except Exception as e:
                        logger.error("Industry news error: %s", e)
                else:
                    logger.info("Skipping industry news for %s — industry not resolved", ticker)

            # ── LLM analysis ──
            _emit(q, "progress", {"message": "🧠 Running LLM analysis for " + info["name"] + "...", "percentage": base_pct + int(step_size*0.75)})
            try:
                analysis = analyser.analyze_company(info, filings, news, search_terms)
            except Exception as e:
                logger.error("Analysis error: %s", e)
                analysis = {"company": info["name"], "ticker": ticker, "overall_sentiment": "Neutral",
                            "overall_summary": "Analysis failed: " + str(e), "developments": [],
                            "filings_summary": "Error", "news_summary": "Error", "error": True}

            # Merge developments with existing
            new_devs = analysis.get("developments", [])
            if existing_devs:
                analysis["developments"] = _merge_developments(existing_devs, new_devs)
                analysis["new_developments_count"] = len(analysis["developments"]) - len(existing_devs)
            else:
                analysis["new_developments_count"] = len(new_devs)

            # Build sources with summary field
            new_sources = {
                "filings": [
                    {
                        "title":   f["title"],
                        "url":     f["url"],
                        "source":  f["source"],
                        "date":    f.get("published_date", ""),
                        "summary": _source_summary(f),
                    }
                    for f in filings[:8]
                ],
                "news": [
                    {
                        "title":    n["title"],
                        "url":      n["url"],
                        "category": n.get("category", ""),
                        "date":     n.get("published_date", ""),
                        "summary":  _source_summary(n),
                    }
                    for n in news[:12]
                ],
            }
            if existing_co:
                existing_sources = existing_co.get("sources", {})
                merged_sources = {
                    "filings": existing_sources.get("filings", []) + new_sources["filings"],
                    "news":    existing_sources.get("news",    []) + new_sources["news"],
                }
            else:
                merged_sources = new_sources

            result = {
                "company_input": raw_input,
                "company_info":  info,
                "filings_count": len(filings) + (existing_co.get("filings_count", 0) if existing_co else 0),
                "news_count":    len(news)    + (existing_co.get("news_count",    0) if existing_co else 0),
                "analysis":      analysis,
                "sources":       merged_sources,
            }
            all_results.append(result)

            _emit(q, "company_result", {"input": raw_input, "result": result})
            _emit(q, "progress", {"message": "✅ Completed " + info["name"], "percentage": base_pct + step_size})

        # Finalise report
        report_data = {
            "report_id":          session_id,
            "generated_at":       datetime.now(timezone.utc).isoformat(),
            "previous_report_at": existing_report.get("generated_at") if existing_report else None,
            "model_used":         model,
            "search_exchange_filings": search_exchange_filings,
            "search_company_news":     search_company_news,
            "search_industry_news":    search_industry_news,
            "search_custom":           search_custom,
            "custom_searches":         custom_searches,
            "days_searched":           days_back,
            "companies_analysed":      len(all_results),
            "results":                 all_results,
        }
        with _lock:
            _reports[session_id] = {"status": "complete", "data": report_data}

        _emit(q, "complete", {"session_id": session_id, "company_count": len(all_results)})
        _emit(q, "progress", {"message": "🎉 Done! Report ready for " + str(len(all_results)) + " company/ies.", "percentage": 100})

    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Fatal analysis error: %s\n%s", e, tb)
        _emit(q, "error", {"message": str(e), "detail": tb[-800:]})
        with _lock:
            _reports[session_id] = {"status": "error", "error": str(e)}
    finally:
        q.put(None)


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
