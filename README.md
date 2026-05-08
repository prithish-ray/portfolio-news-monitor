# AI Portfolio News Monitor

Tracks recent news and filings for your portfolio companies and delivers an AI-generated impact assessment, downloadable report, and spoken audio briefing.

---

## What it does

For each company you add, the app:

1. **Fetches filings** from the relevant stock exchange — SEC/EDGAR (US), TDnet (Japan), NSE/BSE (India), or Investegate (UK/AIM)
2. **Searches company news** — earnings, contracts, partnerships, management changes
3. **Searches industry news** — sector and country-level developments (optional)
4. **Runs custom searches** — any queries you define (optional)
5. **Analyses everything with an LLM** (Groq) — assigns sentiment, impact, and a one-line summary per development
6. **Streams results live** to a web dashboard with source links and dates per development
7. **Generates a spoken briefing** via Groq Orpheus TTS — one sentence per company, played in the browser

---

## Setup

### Prerequisites

- Python 3.10+
- API keys for [Groq](https://console.groq.com), [Tavily](https://tavily.com), and [Financial Modeling Prep](https://financialmodelingprep.com)

### Install

```bash
git clone <repo-url>
cd "Portfolio News Monitor"
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
```

### Configure

Create a `.env` file in the project root:

```env
GROQ_API_KEY=gsk_...
TAVILY_API_KEY=tvly-...
FMP_API_KEY=...
```

### Run

```bash
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser.

---

## Usage

1. Enter up to **3 tickers** (e.g. `AAPL`, `7203.T`, `SGFIN.NS`, `FOUR.L`)
2. Choose your **LLM model** and **search areas**
3. Click **Run Analysis** — results stream in live
4. Click **Listen to Summary** to hear an AI-spoken briefing
5. Click **Download JSON Report** to save the full output
6. Upload a previous report on the next run — the app will only fetch **new** developments since then

---

## Search areas

| Area | Source |
|---|---|
| Stock Exchange Filings | SEC / TDnet / NSE / BSE / Investegate |
| Company News | Tavily web search, LLM-generated queries |
| Industry News | `[Industry] [Country] News 2026` |
| Custom Searches | Your own search strings |

---

## Project structure

```
app.py                      Flask app, routes, SSE streaming, TTS endpoint
modules/
  fmp_client.py             Financial Modeling Prep — ticker lookup
  company_info.py           Company metadata resolver (FMP + Tavily fallback)
  filings_searcher.py       Exchange-aware filings search + TDnet scraper
  tdnet_searcher.py         TDnet (JPX) HTML scraper — no API key required
  news_searcher.py          Company and industry news search
  analysis_engine.py        Groq LLM — structured JSON impact analysis
  groq_client.py            Groq LLM + TTS client
  tavily_client.py          Tavily search wrapper
  rag_processor.py          PDF content extraction
  cache_manager.py          In-memory TTL cache
templates/index.html        Single-page UI
static/css/style.css        Styles
static/js/app.js            Frontend logic (SSE, cards, audio player)
```

---

## API keys

| Service | Used for | Free tier |
|---|---|---|
| [Groq](https://console.groq.com) | LLM analysis + TTS | Yes |
| [Tavily](https://tavily.com) | News and filing search | Yes (1,000 searches/month) |
| [FMP](https://financialmodelingprep.com) | Ticker and company lookup | Yes (`/search-symbol` endpoint) |

---

## Exchange routing

The app automatically routes each ticker to the correct filing source:

| Suffix | Exchange | Source |
|---|---|---|
| *(none)* | US (NYSE / NASDAQ) | SEC EDGAR |
| `.T` | Japan (TSE) | TDnet |
| `.NS` | India (NSE) | NSE India |
| `.BO` | India (BSE) | BSE India |
| `.L` | UK (LSE / AIM) | Investegate |
| Other | Local exchange | Tavily web search |

---

## Notes

- **Incremental mode** — uploading a previous JSON report limits searches to new content since the last run, reducing API usage
- **Company disambiguation** — if a ticker matches multiple listings, a modal lets you pick the correct one
- **Token budget** — prompts are trimmed to stay within Groq's free-tier TPM limits
- **TTS voice** — uses Groq Orpheus `tara` voice; alternatives: `leah`, `jess`, `leo`, `dan`, `mia`, `zac`, `zoe`
