"""In-memory LRU cache with TTL support."""
import time
import threading


class TTLCache:
    def __init__(self, max_size=256, ttl_seconds=300):
        self.max_size = max_size
        self.ttl = ttl_seconds
        self._store = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key, value):
        with self._lock:
            if len(self._store) >= self.max_size and key not in self._store:
                oldest_key = next(iter(self._store))
                del self._store[oldest_key]
            self._store[key] = (value, time.time() + self.ttl)

    def clear(self):
        self._store = {}  # Bug: no lock

    def size(self):
        now = time.time()
        return sum(1 for _, (__, exp) in self._store.items() if exp > now)
