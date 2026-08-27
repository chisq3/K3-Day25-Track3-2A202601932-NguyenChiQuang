"""Additional Redis isolation, concurrency, and graceful-degradation tests."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from redis.exceptions import RedisError

from reliability_lab.cache import SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


class FailingRedis:
    """Minimal Redis substitute that consistently raises connection errors."""

    def ping(self) -> bool:
        raise RedisError("simulated Redis outage")

    def pipeline(self, transaction: bool = True) -> Any:
        del transaction
        raise RedisError("simulated Redis outage")

    def hmget(self, name: str, keys: list[str]) -> list[str | None]:
        del name, keys
        raise RedisError("simulated Redis outage")


@pytest.fixture
def resilient_cache() -> Iterator[SharedRedisCache]:
    cache = SharedRedisCache(
        redis_url="redis://localhost:6379/0",
        ttl_seconds=60,
        similarity_threshold=0.9,
        prefix="rl:test:resilience:",
        graceful_degradation=True,
        redis_retry_interval_seconds=0.0,
    )
    if not cache.ping():
        pytest.skip("Redis not running — start with: docker compose up -d")
    cache.flush()
    yield cache
    cache.flush()
    cache.close()


def test_set_degrades_to_memory_during_redis_outage(
    resilient_cache: SharedRedisCache,
) -> None:
    original_redis = resilient_cache._redis
    resilient_cache._redis = FailingRedis()
    try:
        resilient_cache.set("safe outage query", "memory response")
        cached, score = resilient_cache.get("safe outage query")

        assert cached == "memory response"
        assert score == 1.0
        assert resilient_cache.degraded
        assert resilient_cache.redis_error_count >= 1
        assert resilient_cache.memory_fallback_writes == 1
        assert resilient_cache.memory_fallback_hits == 1
    finally:
        resilient_cache._redis = original_redis


def test_cache_detects_redis_recovery(resilient_cache: SharedRedisCache) -> None:
    original_redis = resilient_cache._redis
    resilient_cache._redis = FailingRedis()
    try:
        resilient_cache.set("recovery query", "fallback response")
        assert resilient_cache.degraded
    finally:
        resilient_cache._redis = original_redis
    resilient_cache.set("recovery query", "redis response")

    assert not resilient_cache.degraded
    assert resilient_cache.redis_recovery_count == 1
    cached, _ = resilient_cache.get("recovery query")
    assert cached == "redis response"


def test_gateway_remains_available_and_reuses_memory_cache_during_outage() -> None:
    cache = SharedRedisCache(
        redis_url="redis://localhost:6379/0",
        ttl_seconds=60,
        similarity_threshold=0.9,
        prefix="rl:test:gateway-outage:",
        graceful_degradation=True,
        redis_retry_interval_seconds=60.0,
    )
    original_redis = cache._redis
    cache._redis = FailingRedis()
    provider = FakeLLMProvider("primary", 0.0, 1, 0.001)
    gateway = ReliabilityGateway(
        [provider],
        {"primary": CircuitBreaker("primary", 2, 1)},
        cache,
    )
    try:
        first = gateway.complete("gateway outage query")
        second = gateway.complete("gateway outage query")

        assert first.route == "primary"
        assert second.cache_hit
        assert second.text == first.text
        assert cache.degraded
    finally:
        cache._redis = original_redis
        cache.close()


def test_prefixes_isolate_independent_applications() -> None:
    first = SharedRedisCache("redis://localhost:6379/0", 60, 0.9, "rl:test:app-a:")
    second = SharedRedisCache("redis://localhost:6379/0", 60, 0.9, "rl:test:app-b:")
    if not first.ping():
        pytest.skip("Redis not running — start with: docker compose up -d")
    first.flush()
    second.flush()
    try:
        first.set("isolated query", "first response")
        cached, _ = second.get("isolated query")
        assert cached is None
    finally:
        first.flush()
        second.flush()
        first.close()
        second.close()


def test_concurrent_redis_access_is_consistent() -> None:
    cache = SharedRedisCache(
        "redis://localhost:6379/0",
        60,
        0.99,
        "rl:test:concurrent:",
    )
    if not cache.ping():
        pytest.skip("Redis not running — start with: docker compose up -d")
    cache.flush()

    def round_trip(index: int) -> bool:
        query = f"concurrent cache query {index}"
        expected = f"response {index}"
        cache.set(query, expected)
        cached, score = cache.get(query)
        return cached == expected and score == 1.0

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            assert all(executor.map(round_trip, range(32)))
    finally:
        cache.flush()
        cache.close()
