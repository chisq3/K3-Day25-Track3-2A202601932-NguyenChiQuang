from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None
    route_reason: str | None = None
    budget_status: str = "disabled"


@dataclass(slots=True)
class CostBudget:
    """Thread-safe soft budget used to influence provider routing."""

    max_cost: float
    warning_ratio: float = 0.8
    _spent: float = field(default=0.0, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.max_cost <= 0:
            raise ValueError("max_cost must be greater than zero")
        if not 0.0 < self.warning_ratio < 1.0:
            raise ValueError("warning_ratio must be between zero and one")

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    @property
    def status(self) -> str:
        with self._lock:
            if self._spent >= self.max_cost:
                return "exhausted"
            if self._spent >= self.max_cost * self.warning_ratio:
                return "warning"
            return "normal"

    def record(self, cost: float) -> None:
        if cost < 0:
            raise ValueError("cost must not be negative")
        with self._lock:
            self._spent += cost


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        budget: CostBudget | None = None,
    ):
        missing_breakers = [provider.name for provider in providers if provider.name not in breakers]
        if missing_breakers:
            missing = ", ".join(missing_breakers)
            raise ValueError(f"Missing circuit breakers for providers: {missing}")

        self.providers = list(providers)
        self.breakers = dict(breakers)
        self.cache = cache
        self.budget = budget

    def complete(self, prompt: str) -> GatewayResponse:
        """Route a prompt through cache, protected providers, and static fallback."""
        budget_status = self.budget.status if self.budget is not None else "disabled"

        if self.cache is not None:
            cached_text, score = self.cache.get(prompt)
            if cached_text is not None:
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=0.0,
                    estimated_cost=0.0,
                    route_reason=f"semantic_cache_score={score:.4f}",
                    budget_status=budget_status,
                )

        if budget_status == "exhausted":
            return self._static_fallback(
                error="cost_budget: exhausted after cache miss",
                route_reason="budget_exhausted_cache_only",
                budget_status=budget_status,
            )

        indexed_providers = list(enumerate(self.providers))
        if budget_status == "warning":
            indexed_providers.sort(key=lambda item: item[1].cost_per_1k_tokens)

        last_error: str | None = None
        for original_index, provider in indexed_providers:
            breaker = self.breakers[provider.name]
            try:
                response = breaker.call(provider.complete, prompt)
            except (ProviderError, CircuitOpenError) as exc:
                last_error = f"{provider.name}: {type(exc).__name__}: {exc}"
                continue

            if self.cache is not None:
                self.cache.set(prompt, response.text, {"provider": provider.name})
            if self.budget is not None:
                self.budget.record(response.estimated_cost)

            route = "primary" if original_index == 0 else "fallback"
            reason = f"provider={provider.name}"
            if budget_status == "warning":
                reason = f"budget_warning;{reason};cheapest_provider_first"
            elif last_error is not None:
                reason = f"provider_fallback_after={last_error};{reason}"

            return GatewayResponse(
                text=response.text,
                route=route,
                provider=response.provider,
                cache_hit=False,
                latency_ms=response.latency_ms,
                estimated_cost=response.estimated_cost,
                error=last_error,
                route_reason=reason,
                budget_status=self.budget.status if self.budget is not None else "disabled",
            )

        return self._static_fallback(
            error=last_error or "gateway: no providers configured",
            route_reason="all_providers_failed_or_open",
            budget_status=budget_status,
        )

    @staticmethod
    def _static_fallback(error: str, route_reason: str, budget_status: str) -> GatewayResponse:
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=0.0,
            estimated_cost=0.0,
            error=error,
            route_reason=route_reason,
            budget_status=budget_status,
        )
