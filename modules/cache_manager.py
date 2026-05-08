"""
Simple file-based cache with TTL support.
Avoids redundant Tavily searches and LLM calls across runs.
"""
import json
import os
import hashlib
import time
import logging

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.cache')
DEFAULT_TTL = 3600  # 1 hour


class CacheManager:
    def __init__(self, cache_dir: str = CACHE_DIR, ttl: int = DEFAULT_TTL):
        self.cache_dir = cache_dir
        self.ttl = ttl
        os.makedirs(cache_dir, exist_ok=True)

    def _key_to_path(self, key: str) -> str:
        hashed = hashlib.md5(key.encode('utf-8')).hexdigest()
        return os.path.join(self.cache_dir, f"{hashed}.json")

    def get(self, key: str):
        path = self._key_to_path(key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                entry = json.load(f)
            if time.time() - entry.get('ts', 0) < self.ttl:
                logger.debug(f"Cache HIT for key hash {path[-12:-5]}")
                return entry['value']
            # Expired — remove
            os.remove(path)
        except Exception as e:
            logger.debug(f"Cache read error: {e}")
        return None

    def set(self, key: str, value) -> None:
        path = self._key_to_path(key)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'ts': time.time(), 'value': value}, f)
        except Exception as e:
            logger.debug(f"Cache write error: {e}")

    def invalidate(self, key: str) -> None:
        path = self._key_to_path(key)
        if os.path.exists(path):
            os.remove(path)

    def clear_all(self) -> int:
        count = 0
        for fname in os.listdir(self.cache_dir):
            if fname.endswith('.json'):
                os.remove(os.path.join(self.cache_dir, fname))
                count += 1
        return count
