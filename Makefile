.PHONY: test test-redis lint typecheck run-chaos run-chaos-redis compare-cache-backends evaluate-cache-threshold redis-evidence report clean docker-up docker-down

test:
	pytest -q

test-redis:
	pytest tests/test_redis_cache.py tests/test_redis_resilience.py tests/test_shared_circuit_breaker.py -v

lint:
	ruff check src tests scripts

typecheck:
	mypy src

run-chaos:
	python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json

run-chaos-redis:
	python scripts/run_chaos.py --config configs/redis.yaml \
		--out reports/redis_metrics.json \
		--csv-out reports/redis_metrics.csv \
		--cache-comparison-out reports/redis_cache_comparison.json \
		--cache-comparison-csv reports/redis_cache_comparison.csv \
		--concurrency-comparison-out reports/redis_concurrency_comparison.json \
		--concurrency-comparison-csv reports/redis_concurrency_comparison.csv

compare-cache-backends:
	python scripts/compare_cache_backends.py

evaluate-cache-threshold:
	python scripts/evaluate_cache_threshold.py

redis-evidence:
	python scripts/redis_evidence.py

report:
	python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache reports/metrics.json reports/final_report.md
