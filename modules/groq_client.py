"""
Groq LLM client with:
  - Multi-model support
  - Request throttling (max 1 query / 2 seconds)
  - Response caching
  - Retry on transient errors
"""
import time
import logging
from groq import Groq
from .cache_manager import CacheManager

logger = logging.getLogger(__name__)

# Display names shown in UI
AVAILABLE_MODELS = [
    {"id": "llama-3.1-8b-instant",     "label": "LLaMA 3.1 8B — Fast (default)", "default": True},
    {"id": "llama-3.3-70b-versatile",  "label": "LLaMA 3.3 70B — Versatile"},
    {"id": "openai/gpt-oss-20b",       "label": "GPT-OSS 20B"},
    {"id": "openai/gpt-oss-120b",      "label": "GPT-OSS 120B — Most Powerful"},
]

THROTTLE_DELAY = 2.0   # seconds between calls
MAX_RETRIES    = 3
RETRY_DELAY    = 3.0


class GroqClient:
    def __init__(self, api_key: str, model: str = "llama-3.1-8b-instant"):
        self.api_key = api_key
        self.client  = Groq(api_key=api_key)
        self.model   = model
        self.cache   = CacheManager(ttl=1800)   # 30-min cache for LLM outputs
        self._last_call: float = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.time() - self._last_call
        if elapsed < THROTTLE_DELAY:
            wait = THROTTLE_DELAY - elapsed
            logger.debug(f"Throttling LLM call — sleeping {wait:.2f}s")
            time.sleep(wait)
        self._last_call = time.time()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def query(
        self,
        prompt: str,
        system_prompt: str = "You are a helpful financial analyst assistant.",
        max_tokens: int = 2048,
        use_cache: bool = True,
    ) -> str:
        cache_key = f"llm:{self.model}:{system_prompt[:80]}:{prompt[:200]}"

        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                logger.debug("LLM cache hit")
                return cached

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._throttle()
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": prompt},
                    ],
                    max_tokens=max_tokens,
                    temperature=0.2,
                )
                content = resp.choices[0].message.content
                result = (content or "").strip()
                if use_cache:
                    self.cache.set(cache_key, result)
                return result

            except Exception as e:
                logger.warning(f"Groq call attempt {attempt}/{MAX_RETRIES} failed: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                else:
                    raise RuntimeError(f"Groq API failed after {MAX_RETRIES} retries: {e}") from e

        return ""  # unreachable but keeps linter happy

    # ------------------------------------------------------------------
    # Text-to-Speech
    # ------------------------------------------------------------------
    def text_to_speech(self, text: str, voice: str = "tara") -> bytes:
        """
        Convert text to speech using Groq's Orpheus TTS model.
        Calls the REST endpoint directly (SDK audio.speech not available in all versions).
        Returns raw WAV bytes.
        Voices: tara, leah, jess, leo, dan, mia, zac, zoe
        """
        import requests
        logger.info("TTS: generating audio for %d chars", len(text))
        resp = requests.post(
            "https://api.groq.com/openai/v1/audio/speech",
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type":  "application/json",
            },
            json={
                "model":           "canopylabs/orpheus-v1-english",
                "input":           text,
                "voice":           voice,
                "response_format": "wav",
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.content
