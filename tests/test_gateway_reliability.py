"""Reliability, observability, concurrency, and budget tests for the gateway."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from reliability_lab.cache import ResponseCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState
from reliability_lab.gateway import CostBudget, ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider, ProviderResponse


class CountingProvider(FakeLLMProvider):
    def __init__(self, name: str, cost: float = 0.001):
        super().__init__(name, fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=cost)
        self.calls = 0
        self.response_cost = cost

    def complete(self, prompt: str) -> ProviderResponse:
        self.calls += 1
        return ProviderResponse(
            provider=self.name,
            text=f"[{self.name}] {prompt}",
            latency_ms=1.0,
            input_tokens=1,
            output_tokens=1,
            estimated_cost=self.response_cost,
        )


def breaker_for(provider: FakeLLMProvider) -> CircuitBreaker:
    return CircuitBreaker(provider.name, failure_threshold=2, reset_timeout_seconds=60)


def test_cache_hit_short_circuits_provider_call() -> None:
    provider = CountingProvider("primary")
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.9)
    cache.set("cached query", "cached response")
    gateway = ReliabilityGateway(
        [provider],
        {provider.name: breaker_for(provider)},
        cache,
    )

    response = gateway.complete("cached query")

    assert response.cache_hit
    assert response.provider is None
    assert response.estimated_cost == 0.0
    assert provider.calls == 0


def test_open_breaker_skips_provider() -> None:
    primary = CountingProvider("primary")
    backup = CountingProvider("backup")
    primary_breaker = breaker_for(primary)
    primary_breaker.state = CircuitState.OPEN
    primary_breaker.opened_at = None
    gateway = ReliabilityGateway(
        [primary, backup],
        {"primary": primary_breaker, "backup": breaker_for(backup)},
    )

    response = gateway.complete("test")

    assert primary.calls == 0
    assert backup.calls == 1
    assert response.provider == "backup"
    assert response.route == "fallback"
    assert response.error is not None and "primary" in response.error


def test_successful_fallback_is_cached_with_provider_metadata() -> None:
    primary = FakeLLMProvider("primary", 1.0, 1, 0.01)
    backup = CountingProvider("backup")
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.9)
    gateway = ReliabilityGateway(
        [primary, backup],
        {"primary": breaker_for(primary), "backup": breaker_for(backup)},
        cache,
    )

    response = gateway.complete("fallback query")

    assert response.route == "fallback"
    assert cache._entries[0].metadata == {"provider": "backup"}
    assert response.route_reason is not None and "provider=backup" in response.route_reason


def test_empty_provider_chain_returns_static_fallback() -> None:
    response = ReliabilityGateway([], {}).complete("test")

    assert response.route == "static_fallback"
    assert response.error == "gateway: no providers configured"


def test_missing_breaker_is_rejected_at_construction() -> None:
    provider = CountingProvider("primary")

    with pytest.raises(ValueError, match="primary"):
        ReliabilityGateway([provider], {})


def test_budget_warning_routes_to_cheapest_provider_first() -> None:
    expensive = CountingProvider("expensive", cost=0.02)
    cheap = CountingProvider("cheap", cost=0.001)
    budget = CostBudget(max_cost=1.0, warning_ratio=0.8)
    budget.record(0.8)
    gateway = ReliabilityGateway(
        [expensive, cheap],
        {"expensive": breaker_for(expensive), "cheap": breaker_for(cheap)},
        budget=budget,
    )

    response = gateway.complete("budget query")

    assert response.provider == "cheap"
    assert response.route == "fallback"
    assert response.route_reason is not None and "budget_warning" in response.route_reason
    assert expensive.calls == 0


def test_exhausted_budget_is_cache_only() -> None:
    provider = CountingProvider("primary")
    budget = CostBudget(max_cost=0.01)
    budget.record(0.01)
    gateway = ReliabilityGateway(
        [provider],
        {"primary": breaker_for(provider)},
        budget=budget,
    )

    response = gateway.complete("uncached query")

    assert response.route == "static_fallback"
    assert response.budget_status == "exhausted"
    assert response.route_reason == "budget_exhausted_cache_only"
    assert provider.calls == 0


def test_exhausted_budget_still_serves_cache_hits() -> None:
    provider = CountingProvider("primary")
    budget = CostBudget(max_cost=0.01)
    budget.record(0.01)
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.9)
    cache.set("cached query", "cached response")
    gateway = ReliabilityGateway(
        [provider],
        {"primary": breaker_for(provider)},
        cache=cache,
        budget=budget,
    )

    response = gateway.complete("cached query")

    assert response.cache_hit
    assert response.text == "cached response"
    assert provider.calls == 0


def test_unexpected_provider_exception_is_not_swallowed() -> None:
    class UnexpectedFailureProvider(CountingProvider):
        def complete(self, prompt: str) -> ProviderResponse:
            raise ValueError(f"unexpected: {prompt}")

    provider = UnexpectedFailureProvider("primary")
    gateway = ReliabilityGateway(
        [provider],
        {"primary": breaker_for(provider)},
    )

    with pytest.raises(ValueError, match="unexpected"):
        gateway.complete("test")


def test_budget_tracking_is_thread_safe() -> None:
    provider = CountingProvider("primary", cost=0.001)
    budget = CostBudget(max_cost=10.0)
    gateway = ReliabilityGateway(
        [provider],
        {"primary": breaker_for(provider)},
        budget=budget,
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(gateway.complete, (f"query-{index}" for index in range(64))))

    assert all(response.route == "primary" for response in responses)
    assert budget.spent == pytest.approx(0.064)


@pytest.mark.parametrize(
    ("max_cost", "warning_ratio", "message"),
    [
        (0.0, 0.8, "max_cost"),
        (1.0, 0.0, "warning_ratio"),
        (1.0, 1.0, "warning_ratio"),
    ],
)
def test_invalid_budget_configuration_is_rejected(
    max_cost: float,
    warning_ratio: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        CostBudget(max_cost=max_cost, warning_ratio=warning_ratio)


def test_budget_rejects_negative_cost_records() -> None:
    budget = CostBudget(max_cost=1.0)

    with pytest.raises(ValueError, match="cost"):
        budget.record(-0.01)
