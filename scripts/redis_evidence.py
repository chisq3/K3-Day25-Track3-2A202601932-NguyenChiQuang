"""Generate reproducible evidence for Redis shared state and guardrails."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from reliability_lab.cache import SharedRedisCache
from reliability_lab.shared_circuit_breaker import SharedRedisCircuitBreaker


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    parser.add_argument("--out", default="reports/redis_shared_state.json")
    args = parser.parse_args()

    prefix = "rl:evidence:cache:"
    first = SharedRedisCache(args.redis_url, 300, 0.3, prefix)
    second = SharedRedisCache(args.redis_url, 300, 0.3, prefix)
    first_breaker = SharedRedisCircuitBreaker(
        "evidence-primary",
        failure_threshold=2,
        reset_timeout_seconds=2,
        success_threshold=1,
        redis_url=args.redis_url,
        prefix="rl:evidence:cb:",
        state_ttl_seconds=300,
        graceful_degradation=False,
    )
    second_breaker = SharedRedisCircuitBreaker(
        "evidence-primary",
        failure_threshold=2,
        reset_timeout_seconds=2,
        success_threshold=1,
        redis_url=args.redis_url,
        prefix="rl:evidence:cb:",
        state_ttl_seconds=300,
        graceful_degradation=False,
    )
    first.flush()
    first_breaker.reset_shared_state()
    try:
        shared_query = "Explain shared Redis cache state"
        shared_response = "Visible from both gateway instances"
        first.set(shared_query, shared_response, {"writer": "instance-a"})
        observed, score = second.get(shared_query)
        shared_key = f"{prefix}{first._query_hash(shared_query)}"

        privacy_query = "account balance for user 123"
        first.set(privacy_query, "must not be stored")
        privacy_key = f"{prefix}{first._query_hash(privacy_query)}"

        first.set("refund policy for 2024", "old policy")
        false_hit_response, false_hit_score = second.get("refund policy for 2026")

        first_breaker.record_failure()
        second_breaker.record_failure()
        shared_circuit_open = not first_breaker.allow_request()

        evidence = {
            "redis_ping": first.ping(),
            "prefix": prefix,
            "shared_key": shared_key,
            "shared_state_visible": observed == shared_response,
            "shared_state_score": score,
            "shared_hash": first._redis.hgetall(shared_key),
            "shared_key_ttl_seconds": first._redis.ttl(shared_key),
            "privacy_key_exists": bool(first._redis.exists(privacy_key)),
            "numeric_false_hit_blocked": false_hit_response is None,
            "numeric_false_hit_score": false_hit_score,
            "false_hit_log": second.false_hit_log,
            "redis_keys": sorted(
                str(key)
                for key in first._redis.scan_iter(match="rl:evidence:*", count=100)
            ),
            "shared_circuit_breaker": {
                "redis_key": first_breaker.redis_key,
                "open_visible_across_instances": shared_circuit_open,
                "snapshot": first_breaker.shared_snapshot(),
                "ttl_seconds": first_breaker.shared_ttl(),
            },
        }
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"wrote {args.out}")
    finally:
        first.close()
        second.close()
        first_breaker.close()
        second_breaker.close()


if __name__ == "__main__":
    main()
