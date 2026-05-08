"""
Lightweight RAG processor for long PDF documents.

Steps:
  1. Fetch PDF from URL (or accept raw text)
  2. Extract full text via PyPDF2
  3. Split into overlapping word-level chunks
  4. Vectorise with TF-IDF
  5. Return the top-k chunks most similar to the query

No heavyweight embedding models — all done with sklearn TF-IDF.
"""
import io
import logging
import requests
import numpy as np

import PyPDF2
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)

MIN_WORDS_FOR_RAG  = 300   # Shorter docs returned as-is
DEFAULT_CHUNK_SIZE = 400   # words per chunk
DEFAULT_OVERLAP    = 60    # word overlap between chunks
DEFAULT_TOP_K      = 4     # chunks to return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_pdf_bytes(url: str, timeout: int = 30) -> bytes | None:
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; PortfolioMonitor/1.0)'}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        if 'pdf' in resp.headers.get('Content-Type', '').lower() or url.lower().endswith('.pdf'):
            return resp.content
    except Exception as e:
        logger.warning(f"PDF fetch failed for {url}: {e}")
    return None


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        pages  = [p.extract_text() or '' for p in reader.pages]
        return '\n'.join(pages)
    except Exception as e:
        logger.warning(f"PDF text extraction error: {e}")
        return ''


def chunk_text(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE,
               overlap: int = DEFAULT_OVERLAP) -> list[str]:
    words  = text.split()
    chunks = []
    step   = max(1, chunk_size - overlap)
    for i in range(0, len(words), step):
        chunk = ' '.join(words[i: i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)
        if i + chunk_size >= len(words):
            break
    return chunks


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class RAGProcessor:
    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap:    int = DEFAULT_OVERLAP,
        top_k:      int = DEFAULT_TOP_K,
    ):
        self.chunk_size = chunk_size
        self.overlap    = overlap
        self.top_k      = top_k

    # ------------------------------------------------------------------
    def process_text(self, text: str, query: str) -> str:
        """Return the most relevant chunks of a long text for a given query."""
        words = text.split()
        if len(words) < MIN_WORDS_FOR_RAG:
            return text   # Short enough to use as-is

        chunks = chunk_text(text, self.chunk_size, self.overlap)
        if not chunks:
            return text

        try:
            vectorizer  = TfidfVectorizer(stop_words='english', max_features=8000)
            all_texts   = chunks + [query]
            tfidf       = vectorizer.fit_transform(all_texts)
            query_vec   = tfidf[-1]
            doc_vecs    = tfidf[:-1]
            sims        = cosine_similarity(query_vec, doc_vecs).flatten()
            top_indices = np.argsort(sims)[-self.top_k:][::-1]
            # Return chunks in document order for readability
            selected    = sorted(top_indices)
            return '\n\n---\n\n'.join(chunks[i] for i in selected)
        except Exception as e:
            logger.warning(f"TF-IDF RAG failed: {e} — falling back to first chunks")
            return '\n\n'.join(chunks[: self.top_k])

    # ------------------------------------------------------------------
    def process_pdf_url(self, url: str, query: str) -> str | None:
        """Fetch a PDF URL and return the most relevant excerpt for query."""
        pdf_bytes = fetch_pdf_bytes(url)
        if not pdf_bytes:
            return None
        text = extract_text_from_pdf(pdf_bytes)
        if not text.strip():
            return None
        return self.process_text(text, query)
