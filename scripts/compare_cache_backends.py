"""Compare memory and Redis cache performance on an identical healthy workload."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from reliability_lab.chaos import load_queries, run_scenario
from reliability_lab.config import ScenarioConfig, load_config


def numeric_metric(report: dict[str, object], key: str) -> float:
    value = report[key]
    if not isinstance(value, (int, float)):
        raise TypeError(f"Metric {key} must be numeric")
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/redis.yaml")
    parser.add_argument("--out", default="reports/cache_backend_comparison.json")
    parser.add_argument("--csv-out", default="reports/cache_backend_comparison.csv")
    args = parser.parse_args()

    config = load_config(args.config)
    queries = load_queries()
    healthy_overrides = {provider.name: 0.0 for provider in config.providers}
    scenario = ScenarioConfig(
        name="backend_comparison",
        provider_overrides=healthy_overrides,
        cache_enabled=True,
    )
    disabled_budget = config.budget.model_copy(update={"enabled": False})
    memory_breakers = config.circuit_breaker.model_copy(
        update={"shared_state_backend": "memory"}
    )
    sequential_load = config.load_test.model_copy(update={"concurrency": 1})
    base_config = config.model_copy(
        deep=True,
        update={
            "budget": disabled_budget,
            "circuit_breaker": memory_breakers,
            "load_test": sequential_load,
        },
    )
    memory_config = base_config.model_copy(
        deep=True,
        update={"cache": base_config.cache.model_copy(update={"backend": "memory"})},
    )
    redis_config = base_config.model_copy(
        deep=True,
        update={
            "cache": base_config.cache.model_copy(
                update={"backend": "redis", "graceful_degradation": False}
            )
        },
    )

    memory = run_scenario(memory_config, queries, scenario).to_report_dict()
    redis = run_scenario(redis_config, queries, scenario).to_report_dict()
    for report in (memory, redis):
        report.pop("scenarios", None)
        report.pop("scenario_metrics", None)

    result: dict[str, object] = {
        "workload": {
            "requests": config.load_test.requests,
            "concurrency": sequential_load.concurrency,
            "random_seed": config.load_test.random_seed,
            "mode": "cold-cache sequential comparison",
        },
        "memory": memory,
        "redis": redis,
        "delta_redis_minus_memory": {
            "latency_p50_ms": round(
                numeric_metric(redis, "latency_p50_ms")
                - numeric_metric(memory, "latency_p50_ms"),
                2,
            ),
            "latency_p95_ms": round(
                numeric_metric(redis, "latency_p95_ms")
                - numeric_metric(memory, "latency_p95_ms"),
                2,
            ),
            "throughput_rps": round(
                numeric_metric(redis, "throughput_rps")
                - numeric_metric(memory, "throughput_rps"),
                2,
            ),
            "cache_hit_rate": round(
                numeric_metric(redis, "cache_hit_rate")
                - numeric_metric(memory, "cache_hit_rate"),
                4,
            ),
        },
    }

    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = Path(args.csv_out)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "backend",
        "total_requests",
        "availability",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "cache_hit_rate",
        "throughput_rps",
        "estimated_cost",
        "estimated_cost_saved",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for backend, report in (("memory", memory), ("redis", redis)):
            writer.writerow({"backend": backend, **{field: report[field] for field in fields[1:]}})

    print(f"wrote {args.out}")
    print(f"wrote {args.csv_out}")


if __name__ == "__main__":
    main()
