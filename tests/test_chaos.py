"""Deterministic chaos, recovery, comparison, and observability tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from reliability_lab.chaos import (
    build_gateway,
    calculate_recovery_time_ms,
    run_cache_comparison,
    run_concurrency_comparison,
    run_scenario,
    run_simulation,
)
from reliability_lab.config import LabConfig, ScenarioConfig


def fast_config(requests: int = 20) -> LabConfig:
    return LabConfig.model_validate(
        {
            "providers": [
                {
                    "name": "primary",
                    "fail_rate": 0.0,
                    "base_latency_ms": 5,
                    "cost_per_1k_tokens": 0.01,
                },
                {
                    "name": "backup",
                    "fail_rate": 0.0,
                    "base_latency_ms": 5,
                    "cost_per_1k_tokens": 0.006,
                },
            ],
            "circuit_breaker": {
                "failure_threshold": 2,
                "reset_timeout_seconds": 0.01,
                "success_threshold": 1,
            },
            "cache": {
                "enabled": True,
                "backend": "memory",
                "ttl_seconds": 60,
                "similarity_threshold": 0.9,
            },
            "load_test": {
                "requests": requests,
                "concurrency": 4,
                "random_seed": 202601932,
            },
            "budget": {"enabled": False},
            "scenarios": [],
        }
    )


QUERIES = ["query alpha", "query beta", "query gamma"]


def test_calculate_recovery_time_averages_complete_cycles() -> None:
    gateway = build_gateway(fast_config())
    gateway.breakers["primary"].transition_log = [
        {"from": "closed", "to": "open", "reason": "failure", "ts": 1.0},
        {"from": "open", "to": "half_open", "reason": "timeout", "ts": 1.05},
        {"from": "half_open", "to": "closed", "reason": "success", "ts": 1.1},
    ]
    gateway.breakers["backup"].transition_log = [
        {"from": "closed", "to": "open", "reason": "failure", "ts": 2.0},
        {"from": "open", "to": "half_open", "reason": "timeout", "ts": 2.1},
        {"from": "half_open", "to": "closed", "reason": "success", "ts": 2.3},
    ]

    assert calculate_recovery_time_ms(gateway) == pytest.approx(200.0)


def test_run_healthy_scenario_collects_end_to_end_metrics() -> None:
    scenario = ScenarioConfig(
        name="all_healthy",
        provider_overrides={"primary": 0.0, "backup": 0.0},
    )

    metrics = run_scenario(fast_config(), QUERIES, scenario)

    assert metrics.total_requests == 20
    assert metrics.availability == 1.0
    assert metrics.static_fallbacks == 0
    assert len(metrics.latencies_ms) == 20
    assert metrics.duration_ms > 0
    assert metrics.throughput_rps > 0


def test_primary_outage_opens_circuit_and_uses_fallback() -> None:
    scenario = ScenarioConfig(
        name="primary_timeout_100",
        provider_overrides={"primary": 1.0, "backup": 0.0},
    )

    metrics = run_scenario(fast_config(), QUERIES, scenario)

    assert metrics.availability == 1.0
    assert metrics.fallback_successes > 0
    assert metrics.circuit_open_count >= 1


def test_all_providers_down_is_contained_by_static_fallback() -> None:
    scenario = ScenarioConfig(
        name="all_providers_down",
        provider_overrides={"primary": 1.0, "backup": 1.0},
        cache_enabled=False,
    )

    metrics = run_scenario(fast_config(requests=12), QUERIES, scenario)

    assert metrics.failed_requests == 12
    assert metrics.static_fallbacks == 12
    assert metrics.circuit_open_count >= 2


def test_recovery_scenario_records_open_to_closed_time() -> None:
    scenario = ScenarioConfig(
        name="primary_recovers",
        provider_overrides={"primary": 1.0, "backup": 0.0},
        cache_enabled=False,
        recovery_provider="primary",
        recovery_after_requests=4,
        recovery_fail_rate=0.0,
    )

    metrics = run_scenario(fast_config(requests=12), QUERIES, scenario)

    assert metrics.availability == 1.0
    assert metrics.fallback_successes > 0
    assert metrics.provider_successes.get("primary", 0) > 0
    assert metrics.recovery_time_ms is not None


def test_simulation_retains_per_scenario_metrics_and_statuses() -> None:
    config = fast_config(requests=12)
    config.scenarios = [
        ScenarioConfig(
            name="all_healthy",
            provider_overrides={"primary": 0.0, "backup": 0.0},
        ),
        ScenarioConfig(
            name="all_providers_down",
            provider_overrides={"primary": 1.0, "backup": 1.0},
            cache_enabled=False,
        ),
    ]

    metrics = run_simulation(config, QUERIES)

    assert metrics.total_requests == 24
    assert metrics.scenarios == {"all_healthy": "pass", "all_providers_down": "pass"}
    assert set(metrics.scenario_metrics) == {"all_healthy", "all_providers_down"}


def test_cache_comparison_uses_identical_request_counts() -> None:
    comparison = run_cache_comparison(fast_config(), QUERIES)
    without_cache = comparison["without_cache"]
    with_cache = comparison["with_cache"]

    assert isinstance(without_cache, dict)
    assert isinstance(with_cache, dict)
    assert without_cache["total_requests"] == with_cache["total_requests"] == 20
    assert without_cache["cache_hit_rate"] == 0.0
    assert isinstance(with_cache["cache_hit_rate"], float)
    assert isinstance(with_cache["estimated_cost"], float)
    assert isinstance(without_cache["estimated_cost"], float)
    assert with_cache["cache_hit_rate"] > 0.0
    assert with_cache["estimated_cost"] < without_cache["estimated_cost"]


def test_concurrency_comparison_preserves_workload() -> None:
    comparison = run_concurrency_comparison(fast_config(requests=16), QUERIES)
    sequential = comparison["sequential"]
    concurrent = comparison["concurrent"]

    assert isinstance(sequential, dict)
    assert isinstance(concurrent, dict)
    assert sequential["total_requests"] == concurrent["total_requests"] == 16
    assert comparison["concurrent_workers"] == 4
    assert isinstance(comparison["speedup"], float)
    assert comparison["speedup"] > 0.0


def test_empty_query_set_is_rejected() -> None:
    with pytest.raises(ValueError, match="queries"):
        run_scenario(fast_config(), [], ScenarioConfig(name="empty"))


def test_metrics_outputs_can_be_written(tmp_path: Path) -> None:
    metrics = run_scenario(
        fast_config(requests=4),
        QUERIES,
        ScenarioConfig(name="all_healthy", provider_overrides={"primary": 0.0}),
    )
    metrics.scenarios["all_healthy"] = "pass"
    json_path = tmp_path / "nested" / "metrics.json"
    csv_path = tmp_path / "nested" / "metrics.csv"

    metrics.write_json(json_path)
    metrics.write_csv(csv_path)

    assert json_path.exists()
    assert csv_path.exists()
    assert "scenario_all_healthy" in csv_path.read_text(encoding="utf-8")
