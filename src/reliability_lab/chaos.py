from __future__ import annotations

import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import CostBudget, GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider
from reliability_lab.shared_circuit_breaker import SharedRedisCircuitBreaker

Observation = tuple[str, GatewayResponse, float]


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(
    config: LabConfig,
    provider_overrides: dict[str, float] | None = None,
    *,
    seed: int | None = None,
    cache_enabled: bool | None = None,
    cache_prefix: str | None = None,
    circuit_prefix: str | None = None,
) -> ReliabilityGateway:
    """Build an isolated gateway for one reproducible scenario."""
    overrides = provider_overrides or {}
    scenario_seed = config.load_test.random_seed if seed is None else seed
    providers = [
        FakeLLMProvider(
            provider.name,
            overrides.get(provider.name, provider.fail_rate),
            provider.base_latency_ms,
            provider.cost_per_1k_tokens,
            rng=random.Random(scenario_seed + index * 1009),
        )
        for index, provider in enumerate(config.providers)
    ]
    breakers: dict[str, CircuitBreaker] = {}
    for provider in config.providers:
        if config.circuit_breaker.shared_state_backend == "redis":
            breakers[provider.name] = SharedRedisCircuitBreaker(
                name=provider.name,
                failure_threshold=config.circuit_breaker.failure_threshold,
                reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
                success_threshold=config.circuit_breaker.success_threshold,
                redis_url=config.circuit_breaker.redis_url,
                prefix=circuit_prefix or config.circuit_breaker.redis_prefix,
                state_ttl_seconds=config.circuit_breaker.state_ttl_seconds,
                graceful_degradation=config.circuit_breaker.graceful_degradation,
            )
        else:
            breakers[provider.name] = CircuitBreaker(
                name=provider.name,
                failure_threshold=config.circuit_breaker.failure_threshold,
                reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
                success_threshold=config.circuit_breaker.success_threshold,
            )

    cache: ResponseCache | SharedRedisCache | None = None
    use_cache = config.cache.enabled if cache_enabled is None else cache_enabled
    if use_cache:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
                prefix=cache_prefix or config.cache.redis_prefix,
                graceful_degradation=config.cache.graceful_degradation,
                redis_retry_interval_seconds=config.cache.redis_retry_interval_seconds,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)

    budget = None
    if config.budget.enabled:
        budget = CostBudget(config.budget.max_cost, config.budget.warning_ratio)
    return ReliabilityGateway(providers, breakers, cache, budget)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Return the mean duration from each OPEN transition to its next CLOSED transition."""
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        opened_at: float | None = None
        for transition in breaker.transition_log:
            destination = transition["to"]
            timestamp = float(transition["ts"])
            if destination == "open":
                opened_at = timestamp
            elif destination == "closed" and opened_at is not None:
                recovery_times.append((timestamp - opened_at) * 1000.0)
                opened_at = None

    return sum(recovery_times) / len(recovery_times) if recovery_times else None


def _scenario_seed(config: LabConfig, scenario: ScenarioConfig) -> int:
    name_offset = sum((index + 1) * ord(char) for index, char in enumerate(scenario.name))
    return config.load_test.random_seed + name_offset


def _run_batch(
    gateway: ReliabilityGateway,
    prompts: list[str],
    concurrency: int,
) -> list[Observation]:
    def execute(prompt: str) -> Observation:
        started_at = time.perf_counter()
        response = gateway.complete(prompt)
        latency_ms = (time.perf_counter() - started_at) * 1000.0
        return prompt, response, latency_ms

    if concurrency == 1:
        return [execute(prompt) for prompt in prompts]
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        return list(executor.map(execute, prompts))


def _expected_cache_cost(config: LabConfig, prompt: str) -> float:
    """Estimate avoided primary-provider cost using a documented 50-output-token baseline."""
    if not config.providers:
        return 0.0
    expected_tokens = max(1, len(prompt.split())) + 50
    return expected_tokens / 1000.0 * config.providers[0].cost_per_1k_tokens


def _record_observation(metrics: RunMetrics, observation: Observation, config: LabConfig) -> None:
    prompt, response, latency_ms = observation
    route_key = "cache_hit" if response.route.startswith("cache_hit:") else response.route
    metrics.total_requests += 1
    metrics.latencies_ms.append(latency_ms)
    metrics.estimated_cost += response.estimated_cost
    metrics.route_counts[route_key] = metrics.route_counts.get(route_key, 0) + 1

    if response.cache_hit:
        metrics.cache_hits += 1
        metrics.estimated_cost_saved += _expected_cache_cost(config, prompt)
    if response.provider is not None:
        metrics.provider_successes[response.provider] = (
            metrics.provider_successes.get(response.provider, 0) + 1
        )

    if response.route == "fallback":
        metrics.fallback_successes += 1
        metrics.successful_requests += 1
    elif response.route == "static_fallback":
        metrics.static_fallbacks += 1
        metrics.failed_requests += 1
    else:
        metrics.successful_requests += 1


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run one deterministic scenario, optionally with a fail-then-recover phase."""
    if not queries:
        raise ValueError("queries must not be empty")

    seed = _scenario_seed(config, scenario)
    scenario_namespace = re.sub(r"[^a-zA-Z0-9_-]", "_", scenario.name)
    gateway = build_gateway(
        config,
        scenario.provider_overrides,
        seed=seed,
        cache_enabled=scenario.cache_enabled,
        cache_prefix=f"{config.cache.redis_prefix}{scenario_namespace}:",
        circuit_prefix=f"{config.circuit_breaker.redis_prefix}{scenario_namespace}:",
    )
    for breaker in gateway.breakers.values():
        if isinstance(breaker, SharedRedisCircuitBreaker):
            breaker.reset_shared_state()
    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.flush()
    query_rng = random.Random(seed)
    prompts = [query_rng.choice(queries) for _ in range(config.load_test.requests)]
    started_at = time.perf_counter()

    if scenario.recovery_provider is None:
        observations = _run_batch(gateway, prompts, config.load_test.concurrency)
    else:
        split_at = min(scenario.recovery_after_requests or 1, len(prompts))
        observations = _run_batch(
            gateway,
            prompts[:split_at],
            config.load_test.concurrency,
        )
        recovering_provider = next(
            (provider for provider in gateway.providers if provider.name == scenario.recovery_provider),
            None,
        )
        if recovering_provider is None:
            raise ValueError(f"Unknown recovery provider: {scenario.recovery_provider}")
        recovering_provider.fail_rate = scenario.recovery_fail_rate
        time.sleep(config.circuit_breaker.reset_timeout_seconds + 0.05)
        observations.extend(
            _run_batch(gateway, prompts[split_at:], config.load_test.concurrency)
        )

    metrics = RunMetrics(duration_ms=(time.perf_counter() - started_at) * 1000.0)
    for observation in observations:
        _record_observation(metrics, observation, config)

    metrics.circuit_open_count = sum(
        transition["to"] == "open"
        for breaker in gateway.breakers.values()
        for transition in breaker.transition_log
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)

    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.close()
    for breaker in gateway.breakers.values():
        if isinstance(breaker, SharedRedisCircuitBreaker):
            breaker.close()
    return metrics


def _scenario_passed(name: str, metrics: RunMetrics) -> bool:
    if name == "primary_timeout_100":
        return (
            metrics.availability >= 0.95
            and metrics.fallback_successes > 0
            and metrics.circuit_open_count >= 1
        )
    if name == "primary_flaky_50":
        return (
            metrics.availability >= 0.95
            and metrics.provider_successes.get("primary", 0) > 0
            and metrics.fallback_successes > 0
        )
    if name == "all_healthy":
        return (
            metrics.availability == 1.0
            and metrics.static_fallbacks == 0
            and metrics.circuit_open_count == 0
        )
    if name == "all_providers_down":
        return (
            metrics.failed_requests == metrics.total_requests
            and metrics.static_fallbacks == metrics.total_requests
            and metrics.circuit_open_count >= 2
        )
    if name == "primary_recovers":
        return (
            metrics.recovery_time_ms is not None
            and metrics.fallback_successes > 0
            and metrics.provider_successes.get("primary", 0) > 0
        )
    return metrics.successful_requests > 0


def _merge_metrics(combined: RunMetrics, result: RunMetrics) -> None:
    combined.total_requests += result.total_requests
    combined.successful_requests += result.successful_requests
    combined.failed_requests += result.failed_requests
    combined.fallback_successes += result.fallback_successes
    combined.static_fallbacks += result.static_fallbacks
    combined.cache_hits += result.cache_hits
    combined.circuit_open_count += result.circuit_open_count
    combined.estimated_cost += result.estimated_cost
    combined.estimated_cost_saved += result.estimated_cost_saved
    combined.duration_ms += result.duration_ms
    combined.latencies_ms.extend(result.latencies_ms)
    for route, count in result.route_counts.items():
        combined.route_counts[route] = combined.route_counts.get(route, 0) + count
    for provider, count in result.provider_successes.items():
        combined.provider_successes[provider] = (
            combined.provider_successes.get(provider, 0) + count
        )


def _scenario_report(result: RunMetrics) -> dict[str, object]:
    report = result.to_report_dict()
    report.pop("scenarios", None)
    report.pop("scenario_metrics", None)
    return report


def _numeric_metric(report: dict[str, object], key: str) -> float:
    value = report[key]
    if not isinstance(value, (int, float)):
        raise TypeError(f"Metric {key} must be numeric")
    return float(value)


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all configured scenarios and retain aggregate and per-scenario metrics."""
    scenarios = config.scenarios or [ScenarioConfig(name="default", description="baseline run")]
    combined = RunMetrics()
    recovery_times: list[float] = []

    for scenario in scenarios:
        result = run_scenario(config, queries, scenario)
        status = "pass" if _scenario_passed(scenario.name, result) else "fail"
        combined.scenarios[scenario.name] = status
        combined.scenario_metrics[scenario.name] = _scenario_report(result)
        _merge_metrics(combined, result)
        if result.recovery_time_ms is not None:
            recovery_times.append(result.recovery_time_ms)

    if recovery_times:
        combined.recovery_time_ms = sum(recovery_times) / len(recovery_times)
    return combined


def run_cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, object]:
    """Compare identical healthy workloads with and without the in-memory cache."""
    provider_overrides = {provider.name: 0.0 for provider in config.providers}
    scenario = ScenarioConfig(name="cache_comparison", provider_overrides=provider_overrides)
    disabled_budget = config.budget.model_copy(update={"enabled": False})
    comparison_config = config.model_copy(deep=True, update={"budget": disabled_budget})

    without_cache = run_scenario(
        comparison_config,
        queries,
        scenario.model_copy(update={"cache_enabled": False}),
    )
    with_cache = run_scenario(
        comparison_config,
        queries,
        scenario.model_copy(update={"cache_enabled": True}),
    )
    without_report = _scenario_report(without_cache)
    with_report = _scenario_report(with_cache)
    return {
        "without_cache": without_report,
        "with_cache": with_report,
        "delta": {
            "latency_p50_ms": round(
                _numeric_metric(with_report, "latency_p50_ms")
                - _numeric_metric(without_report, "latency_p50_ms"),
                2,
            ),
            "latency_p95_ms": round(
                _numeric_metric(with_report, "latency_p95_ms")
                - _numeric_metric(without_report, "latency_p95_ms"),
                2,
            ),
            "estimated_cost": round(
                _numeric_metric(with_report, "estimated_cost")
                - _numeric_metric(without_report, "estimated_cost"),
                6,
            ),
            "cache_hit_rate": round(
                _numeric_metric(with_report, "cache_hit_rate")
                - _numeric_metric(without_report, "cache_hit_rate"),
                4,
            ),
        },
    }


def run_concurrency_comparison(config: LabConfig, queries: list[str]) -> dict[str, object]:
    """Compare sequential and concurrent throughput on a healthy no-cache workload."""
    provider_overrides = {provider.name: 0.0 for provider in config.providers}
    scenario = ScenarioConfig(
        name="concurrency_comparison",
        provider_overrides=provider_overrides,
        cache_enabled=False,
    )
    disabled_budget = config.budget.model_copy(update={"enabled": False})
    sequential_load = config.load_test.model_copy(update={"concurrency": 1})
    sequential_config = config.model_copy(
        deep=True,
        update={"budget": disabled_budget, "load_test": sequential_load},
    )
    concurrent_config = config.model_copy(
        deep=True,
        update={"budget": disabled_budget},
    )

    sequential = run_scenario(sequential_config, queries, scenario)
    concurrent = run_scenario(concurrent_config, queries, scenario)
    sequential_report = _scenario_report(sequential)
    concurrent_report = _scenario_report(concurrent)
    speedup = (
        sequential.duration_ms / concurrent.duration_ms if concurrent.duration_ms > 0 else 0.0
    )
    return {
        "sequential_workers": 1,
        "concurrent_workers": config.load_test.concurrency,
        "sequential": sequential_report,
        "concurrent": concurrent_report,
        "speedup": round(speedup, 2),
    }
