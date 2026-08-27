from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from threading import RLock
from typing import Any

from redis.exceptions import RedisError

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True when both queries contain different numeric constraints.

    This covers the required four-digit years/IDs and shorter constraints such as
    "3 bullets" versus "5 bullets".
    """
    nums_q = set(re.findall(r"\b\d+\b", query))
    nums_c = set(re.findall(r"\b\d+\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Thread-safe in-memory response cache with semantic guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be between zero and one")

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []
        self._lock = RLock()

    def get(self, query: str) -> tuple[str | None, float]:
        """Return the best non-expired match if it passes all guardrails."""
        if _is_uncacheable(query):
            return None, 0.0

        now = time.time()
        with self._lock:
            self._entries = [
                entry
                for entry in self._entries
                if now - entry.created_at <= self.ttl_seconds
            ]
            entries = tuple(self._entries)

        best_entry: CacheEntry | None = None
        best_score = 0.0
        for entry in entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_entry = entry
                best_score = score

        if best_entry is None or best_score < self.similarity_threshold:
            return None, best_score

        if _looks_like_false_hit(query, best_entry.key):
            false_hit = {
                "query": query,
                "cached_key": best_entry.key,
                "score": best_score,
                "reason": "date_or_number_mismatch",
                "ts": now,
            }
            with self._lock:
                self.false_hit_log.append(false_hit)
            return None, best_score

        return best_entry.value, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response unless the query is privacy-sensitive."""
        if _is_uncacheable(query):
            return

        entry = CacheEntry(
            key=query,
            value=value,
            created_at=time.time(),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._entries.append(entry)

    def clear(self) -> None:
        """Remove all in-memory entries while preserving cache configuration."""
        with self._lock:
            self._entries.clear()

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute cosine similarity over lowercase word tokens and character trigrams."""
        if a == b:
            return 1.0

        def vectorize(text: str) -> Counter[str]:
            words = re.findall(r"\w+", text.lower())
            tokens = list(words)
            for word in words:
                tokens.extend(word[index : index + 3] for index in range(len(word) - 2))
            return Counter(tokens)

        vector_a = vectorize(a)
        vector_b = vectorize(b)
        if not vector_a or not vector_b:
            return 0.0

        dot_product = sum(
            count * vector_b.get(token, 0) for token, count in vector_a.items()
        )
        norm_a = math.sqrt(sum(count * count for count in vector_a.values()))
        norm_b = math.sqrt(sum(count * count for count in vector_b.values()))
        score = dot_product / (norm_a * norm_b)
        return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache with privacy and numeric-intent guardrails.

    Data model:
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    Exact lookups are O(1). Semantic lookup uses SCAN plus pipelined HMGET batches,
    never the blocking Redis KEYS command. Optional local memory fallback keeps the
    gateway available during a Redis outage and records degradation counters.
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
        *,
        graceful_degradation: bool = False,
        redis_retry_interval_seconds: float = 1.0,
    ):
        import redis as redis_lib

        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be between zero and one")
        if not prefix:
            raise ValueError("prefix must not be empty")
        if redis_retry_interval_seconds < 0:
            raise ValueError("redis_retry_interval_seconds must not be negative")

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._fallback = (
            ResponseCache(ttl_seconds, similarity_threshold) if graceful_degradation else None
        )
        self._redis_retry_interval_seconds = redis_retry_interval_seconds
        self._redis_unavailable_until = 0.0
        self._degraded_since: float | None = None
        self._lock = RLock()
        self.redis_error_count = 0
        self.memory_fallback_hits = 0
        self.memory_fallback_writes = 0
        self.redis_recovery_count = 0

    @property
    def degraded(self) -> bool:
        """Whether a Redis error has activated the local fallback path."""
        with self._lock:
            return self._degraded_since is not None

    def health_metrics(self) -> dict[str, int | bool]:
        """Return counters suitable for reports without exposing Redis internals."""
        with self._lock:
            return {
                "degraded": self._degraded_since is not None,
                "redis_error_count": self.redis_error_count,
                "memory_fallback_hits": self.memory_fallback_hits,
                "memory_fallback_writes": self.memory_fallback_writes,
                "redis_recovery_count": self.redis_recovery_count,
            }

    def _should_try_redis(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._redis_unavailable_until

    def _mark_redis_error(self) -> None:
        with self._lock:
            self.redis_error_count += 1
            if self._degraded_since is None:
                self._degraded_since = time.monotonic()
            self._redis_unavailable_until = (
                time.monotonic() + self._redis_retry_interval_seconds
            )

    def _mark_redis_success(self) -> None:
        with self._lock:
            if self._degraded_since is not None:
                self.redis_recovery_count += 1
            self._degraded_since = None
            self._redis_unavailable_until = 0.0

    def _fallback_get(self, query: str) -> tuple[str | None, float]:
        if self._fallback is None:
            return None, 0.0
        cached, score = self._fallback.get(query)
        if cached is not None:
            with self._lock:
                self.memory_fallback_hits += 1
        return cached, score

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            available = bool(self._redis.ping())
        except RedisError:
            self._mark_redis_error()
            return False
        if available:
            self._mark_redis_success()
        return available

    def get(self, query: str) -> tuple[str | None, float]:
        """Return an exact or semantic Redis hit, falling back locally on Redis errors."""
        if _is_uncacheable(query):
            return None, 0.0
        if not self._should_try_redis():
            return self._fallback_get(query)

        try:
            result = self._get_from_redis(query)
        except RedisError:
            self._mark_redis_error()
            return self._fallback_get(query)

        self._mark_redis_success()
        if result[0] is not None:
            return result
        fallback_result = self._fallback_get(query)
        return fallback_result if fallback_result[0] is not None else result

    def _get_from_redis(self, query: str) -> tuple[str | None, float]:
        exact_key = f"{self.prefix}{self._query_hash(query)}"
        exact_query, exact_response = self._redis.hmget(
            exact_key,
            ["query", "response"],
        )
        if (
            isinstance(exact_query, str)
            and isinstance(exact_response, str)
            and exact_query.lower().strip() == query.lower().strip()
        ):
            return exact_response, 1.0

        best_query: str | None = None
        best_response: str | None = None
        best_score = 0.0
        keys = list(self._redis.scan_iter(match=f"{self.prefix}*", count=100))
        for start in range(0, len(keys), 100):
            batch = keys[start : start + 100]
            pipeline = self._redis.pipeline(transaction=False)
            for key in batch:
                pipeline.hmget(key, ["query", "response"])
            for values in pipeline.execute():
                if not isinstance(values, (list, tuple)) or len(values) != 2:
                    continue
                cached_query, cached_response = values
                if not isinstance(cached_query, str) or not isinstance(cached_response, str):
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_query = cached_query
                    best_response = cached_response
                    best_score = score

        if best_query is None or best_response is None or best_score < self.similarity_threshold:
            return None, best_score
        if _looks_like_false_hit(query, best_query):
            with self._lock:
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_query,
                        "score": best_score,
                        "reason": "date_or_number_mismatch",
                        "ts": time.time(),
                    }
                )
            return None, best_score
        return best_response, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Atomically store a privacy-safe response hash and its TTL in Redis."""
        if _is_uncacheable(query):
            return

        if self._fallback is not None:
            self._fallback.set(query, value, metadata)
        if not self._should_try_redis():
            with self._lock:
                self.memory_fallback_writes += 1
            return

        key = f"{self.prefix}{self._query_hash(query)}"
        mapping = {
            "query": query,
            "response": value,
            "metadata": json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        }
        try:
            with self._redis.pipeline(transaction=True) as pipeline:
                pipeline.hset(key, mapping=mapping)
                pipeline.expire(key, self.ttl_seconds)
                pipeline.execute()
        except RedisError:
            self._mark_redis_error()
            if self._fallback is not None:
                with self._lock:
                    self.memory_fallback_writes += 1
            return
        self._mark_redis_success()

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        try:
            keys = list(self._redis.scan_iter(match=f"{self.prefix}*", count=100))
            for start in range(0, len(keys), 100):
                self._redis.delete(*keys[start : start + 100])
        except RedisError:
            self._mark_redis_error()
            if self._fallback is None:
                raise
        if self._fallback is not None:
            self._fallback.clear()

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
