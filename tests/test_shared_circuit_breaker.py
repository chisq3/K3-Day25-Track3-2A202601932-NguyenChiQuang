"""Integration tests for the optional Redis-shared circuit state."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from reliability_lab.circuit_breaker import CircuitState
from reliability_lab.shared_circuit_breaker import SharedRedisCircuitBreaker


@pytest.fixture
def shared_breakers() -> Iterator[tuple[SharedRedisCircuitBreaker, SharedRedisCircuitBreaker]]:
    def build() -> SharedRedisCircuitBreaker:
        return SharedRedisCircuitBreaker(
            name="shared-primary",
            failure_threshold=2,
            reset_timeout_seconds=0.02,
            success_threshold=1,
            redis_url="redis://localhost:6379/0",
            prefix="rl:test:shared-cb:",
            state_ttl_seconds=60,
            graceful_degradation=False,
        )

    first = build()
    second = build()
    if not first.ping():
        pytest.skip("Redis not running — start with: docker compose up -d")
    first.reset_shared_state()
    yield first, second
    first.reset_shared_state()
    first.close()
    second.close()


def test_failure_count_and_open_state_are_shared(
    shared_breakers: tuple[SharedRedisCircuitBreaker, SharedRedisCircuitBreaker],
) -> None:
    first, second = shared_breakers

    first.record_failure()
    second.record_failure()

    assert second.state == CircuitState.OPEN
    assert not first.allow_request()
    assert first.state == CircuitState.OPEN
    assert first.failure_count == 2
    assert first.shared_snapshot()["state"] == "open"
    assert first.shared_ttl() > 0


def test_recovery_transition_is_visible_across_instances(
    shared_breakers: tuple[SharedRedisCircuitBreaker, SharedRedisCircuitBreaker],
) -> None:
    first, second = shared_breakers
    first.record_failure()
    second.record_failure()
    time.sleep(0.03)

    assert second.allow_request()
    assert second.state == CircuitState.HALF_OPEN
    second.record_success()

    assert first.allow_request()
    assert first.state == CircuitState.CLOSED
    assert first.failure_count == 0
