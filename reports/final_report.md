# Day 25 Reliability Agent Final Report

This report is generated from committed configuration and machine-readable evidence. The
workload uses fake local providers, `100` requests per scenario,
`8` workers, and random seed `202601932`.

## 1. Architecture summary

```text
User request
    |
    v
ReliabilityGateway
    |
    +--> privacy guard --> cache lookup ----------------------> cache hit: return
    |                     | memory or shared Redis
    |                     v miss / Redis local degradation
    |
    +--> CostBudget (normal / warning / exhausted)
    |        | warning: cheapest healthy provider first
    |        | exhausted: cache-only, then static fallback
    |
    +--> CircuitBreaker[primary] --> primary provider
    |        | OPEN/error: skip without a retry loop
    |        v
    +--> CircuitBreaker[backup]  --> backup provider
    |        | OPEN/error
    |        v
    +--> static fallback (reported as a contained failure)

Optional Redis deployment:
    SharedRedisCache <--> Redis hashes + TTL
    SharedRedisCircuitBreaker <--> Redis state hash + distributed lock + TTL
```

The breaker implements `CLOSED -> OPEN -> HALF_OPEN -> CLOSED`; provider exceptions are
recorded once and re-raised, so the gateway performs at most one attempt per provider and
cannot create an unbounded retry storm. Route reasons identify the selected provider,
prior failure, cache score, and cost-budget state.

## 2. Configuration and rationale

| Setting | Value | Rationale |
|---|---:|---|
| provider.primary.fail_rate | 0.25 | Default non-chaos failure probability for the preferred route. |
| provider.primary.base_latency_ms | 180 ms | Deterministic local latency model; no external API key is required. |
| provider.primary.cost_per_1k_tokens | $0.010000 | Drives cost accounting and cost-aware routing. |
| provider.backup.fail_rate | 0.05 | Default non-chaos failure probability for the fallback route. |
| provider.backup.base_latency_ms | 260 ms | Deterministic local latency model; no external API key is required. |
| provider.backup.cost_per_1k_tokens | $0.006000 | Drives cost accounting and cost-aware routing. |
| circuit.failure_threshold | 3 | Stops retry storms while tolerating isolated failures. |
| circuit.reset_timeout_seconds | 2 s | Bounds outage probing and supports the <5 s recovery SLO. |
| circuit.success_threshold | 1 | One successful HALF_OPEN probe closes the simulated circuit. |
| circuit.shared_state_backend | memory | Keeps the portable default independent from Redis. |
| cache.enabled | true | Enables cache-first routing in the normal workload. |
| cache.backend | memory | Portable default; Redis has a separate reproducible configuration. |
| cache.ttl_seconds | 300 s | Limits stale reuse and automatically expires Redis keys. |
| cache.similarity_threshold | 0.90 | Retained 100% precision and recall on the labelled lab set. |
| cache.redis_url | redis://localhost:6379/0 | Local Docker Redis endpoint; no external service is required. |
| load_test.requests | 100 | Stable sample size per scenario without making the lab excessively slow. |
| load_test.concurrency | 8 | Exercises thread safety and supports the concurrency comparison. |
| load_test.random_seed | 202601932 | Reproducible query selection and provider outcomes. |
| budget.enabled | true | Exercises warning and exhausted cost-aware routes. |
| budget.max_cost | $0.050000 per scenario | Enables cost-aware routing without coupling independent scenarios. |
| budget.warning_ratio | 0.80 | At 80%, the gateway tries the cheaper provider first. |
| Redis cache backend | redis | Selects shared cache state for the Redis evidence run. |
| Redis cache prefix | `rl:lab25:` | Isolates applications and each controlled chaos scenario. |
| Redis graceful degradation | true | Keeps serving through a local cache when Redis is unavailable. |
| Redis retry interval | 0.25 s | Avoids hammering failed Redis while allowing fast recovery. |
| shared breaker state | redis | Shares counters and state across gateway instances. |
| shared breaker Redis URL | redis://localhost:6379/0 | Uses the same local, health-checked Redis service. |
| shared breaker prefix | `rl:lab25:cb:` | Separates breaker state from cached responses. |
| shared breaker TTL | 300 s | Cleans up abandoned provider state automatically. |
| shared breaker graceful degradation | true | Falls back to local breaker state if Redis becomes unavailable. |

Five named chaos scenarios override only provider failure rates. Cache and breaker Redis
namespaces are scenario-specific and flushed before controlled runs, preventing stale data
from making a later benchmark appear artificially successful.

### Similarity threshold evidence

Threshold `0.90` achieved precision `1.00` and
recall `1.00` on the labelled lab pairs. At `0.92`,
the refund paraphrase scored `0.904534` and became a false negative, reducing recall to
`0.6667`; therefore `0.90` is the highest tested threshold that retained perfect precision
and recall.

Numeric guardrails rejected high-scoring unsafe candidates:

- `refund_year_mismatch`: similarity=0.944444, rejected by `date_or_number_mismatch`.
- `tuition_year_mismatch`: similarity=0.957447, rejected by `date_or_number_mismatch`.
- `bullet_count_mismatch`: similarity=0.968750, rejected by `date_or_number_mismatch`.

## 3. SLO definitions and assessment

| SLI | Target | Actual | Result |
|---|---:|---:|---|
| Availability, recoverable scenarios | >= 99% | 100.00% | **PASS** |
| Availability, aggregate including total outage | >= 99% | 80.00% | **FAIL (expected chaos case)** |
| Latency P95 | < 2500 ms | memory 482.73 ms; Redis 652.62 ms | **PASS** |
| Fallback success, recoverable fallback attempts | >= 95% | 100.00% | **PASS** |
| Fallback success, aggregate including both providers down | >= 95% | 48.72% | **FAIL (expected chaos case)** |
| Cache hit rate | >= 10% | memory 34.80%; Redis 34.00% | **PASS** |
| Recovery time | < 5000 ms | memory 2848.98 ms; Redis 2973.49 ms | **PASS** |

The aggregate includes 100 deliberately failed requests from `all_providers_down`; treating
those static fallbacks as successes would hide an outage. Operational SLOs are therefore
reported both with and without that explicitly unrecoverable scenario.

## 4. Metrics

| Metric | Memory run | Redis + shared-breaker run |
|---|---:|---:|
| total_requests | 500 | 500 |
| successful / failed | 400 / 100 | 400 / 100 |
| availability | 80.00% | 80.00% |
| error_rate | 20.00% | 20.00% |
| latency_p50_ms | 3.47 | 115.06 |
| latency_p95_ms | 482.73 | 652.62 |
| latency_p99_ms | 524.57 | 1035.66 |
| fallback_success_rate | 48.72% | 48.72% |
| cache_hit_rate | 34.80% | 34.00% |
| estimated_cost | $0.104068 | $0.106216 |
| estimated_cost_saved | $0.102250 | $0.099990 |
| circuit_open_count | 5 | 5 |
| recovery_time_ms | 2848.98 | 2973.49 |
| throughput_rps | 45.10 | 33.28 |

Memory route counts: `{"fallback": 95, "cache_hit": 174, "primary": 131, "static_fallback": 100}`. Provider successes: `{"backup": 95, "primary": 131}`.
Raw evidence is in `reports/metrics.json`, `reports/metrics.csv`,
`reports/redis_metrics.json`, and `reports/redis_metrics.csv`.

## 5. Cache comparison

### In-memory cache enabled versus disabled

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 206.82 | 183.36 | -23.46 |
| latency_p95_ms | 238.37 | 237.35 | -1.02 |
| estimated_cost | $0.058900 | $0.030990 | -$0.027910 |
| cache_hit_rate | 0.00% | 47.00% | 47.00% |
| throughput_rps | 36.72 | 66.73 | +30.01 |

The memory cache reduced observed cost by
`$0.027910`
and increased throughput by
`81.73%`.

### Redis cache enabled versus disabled

| Metric | Without cache | With Redis cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 234.16 | 199.19 | -34.97 |
| latency_p95_ms | 519.80 | 449.75 | -70.05 |
| estimated_cost | $0.058900 | $0.031850 | -$0.027050 |
| cache_hit_rate | 0.00% | 46.00% | 46.00% |
| throughput_rps | 27.90 | 49.49 | +21.59 |

Redis caching reduced cost by
`45.93%`
and increased throughput by
`77.38%`.

## 6. Redis shared cache

An in-memory cache is process-local: two gateway replicas duplicate work, lose cache state on
restart, and can return inconsistent hit rates. `SharedRedisCache` uses namespaced Redis
hashes, atomic `HSET + EXPIRE`, O(1) exact lookup, and `SCAN` plus pipelined `HMGET` for
semantic candidates. The application never uses blocking `KEYS`; it is shown below only as
the rubric-requested diagnostic command.

Evidence from two independently constructed clients:

- Redis ping: `true`.
- Instance B observed instance A's value: `true` at score `1.00`.
- Shared key TTL: `300` seconds.
- Privacy-sensitive key exists: `false`.
- Numeric false hit blocked: `true` at similarity `0.882353`.

```console
$ docker compose exec -T redis redis-cli KEYS 'rl:evidence:*'
rl:evidence:cache:3169695a66ac
rl:evidence:cache:c54f309198be
rl:evidence:cb:evidence-primary
```

### Memory versus Redis backend isolation benchmark

Both cold caches used 100 sequential requests and the same seed, producing the same 68% hit
rate and provider cost.

| Metric | Memory | Redis | Redis - memory |
|---|---:|---:|---:|
| latency_p50_ms | 2.12 | 1.48 | -0.64 |
| latency_p95_ms | 235.36 | 239.52 | +4.16 |
| throughput_rps | 14.21 | 14.01 | -0.20 |
| cache_hit_rate | 68.00% | 68.00% | 0.00% |

Redis added `4.16 ms` at P95 in this small local
workload while providing cross-process state and restart durability.

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Result |
|---|---|---|---|
| `primary_timeout_100` | Primary fails; backup serves traffic; primary circuit opens. | availability 100.00%; fallback=43; cache hits=57; opens=1 | **PASS** |
| `primary_flaky_50` | Mix of primary and backup responses; failure is contained. | primary=11; fallback=27; cache hits=62; opens=1 | **PASS** |
| `all_healthy` | Primary/cache serve all requests; no circuit opens. | availability 100.00%; primary=45; cache hits=55; opens=0 | **PASS** |
| `all_providers_down` | Static fallback contains a deliberately unrecoverable outage. | expected failures=100/100; static fallbacks=100; opens=2 | **PASS** |
| `primary_recovers` | Fallback serves the outage, then primary returns after a probe. | fallback=25; primary after recovery=75; recovery=2848.98 ms | **PASS** |

All five scenarios passed their explicit criteria. Static fallback is intentionally counted
as a failed request: it contains the blast radius but does not satisfy the original request.

## 8. Stretch goals completed

| Stretch goal | Evidence | Result |
|---|---|---|
| Concurrent load | Memory 1->8 workers: 7.68x; Redis: 6.56x. | **PASS** |
| Redis circuit state | Two instances see state `open`, failure count `2`, TTL `300` s. | **PASS** |
| Redis graceful degradation | Gateway integration test proves Redis error -> local cache -> automatic recovery counters. | **PASS** |
| Cost-aware routing | At 80% budget, cheapest provider is tried first; at 100%, cache-only/static route is used. | **PASS** |
| Property-based tests | Hypothesis fuzzes breaker transitions and cache similarity/guardrail invariants. | **PASS** |
| SLO table | Recoverable and aggregate scopes are reported separately above. | **PASS** |

## 9. Failure analysis

The largest remaining production weakness is semantic Redis lookup complexity. Exact hits are
O(1), but a semantic miss scans every namespaced key and computes lexical n-gram similarity
inside the gateway. The lab corpus is small, so the isolated Redis overhead is only
`4.16 ms` at P95; this result does not establish
scalability to millions of entries. High cardinality could increase Redis traffic and gateway
CPU, while lexical similarity can still miss non-numeric intent changes.

Before production, replace full scans with tenant-partitioned vector/ANN retrieval, store
precomputed embeddings, fetch only top-k candidates, and apply an intent classifier plus the
existing privacy/numeric checks before reuse. Add cache-quality SLOs for false-hit rate and
evaluate against a larger labelled dataset.

A secondary trade-off is distributed breaker coordination. The Redis chaos P95 is
`652.62 ms` versus
`482.73 ms` in memory. The backend-only comparison attributes
only `4.16 ms` to cache access, so the extra Redis
round trips and lock coordination are a likely contributor, although this is an inference
from separate runs. A production design should replace lock/load/store operations with one
atomic Lua transition and enforce a short Redis timeout.

## 10. Verification and reproducibility

```bash
export UV_PROJECT_ENVIRONMENT="${XDG_CACHE_HOME:-$HOME/.cache}/uv-envs/day25-lab25"
export UV_LINK_MODE=copy

docker compose up -d
uv sync --python 3.11 --extra dev
uv run --no-sync make lint
uv run --no-sync make typecheck
uv run --no-sync make test
uv run --no-sync make run-chaos
uv run --no-sync make run-chaos-redis
uv run --no-sync make compare-cache-backends
uv run --no-sync make evaluate-cache-threshold
uv run --no-sync make redis-evidence
uv run --no-sync make report
```

Final verification: `89 passed, 7 xpassed`; Redis tests are not skipped. Phase-specific
evidence is retained in `reports/phase1_circuit_breaker_tests.txt` through
`reports/phase5_redis_tests.txt`.

## 11. Next steps

1. Replace O(N) semantic scans with a measured top-k vector index and tenant isolation.
2. Convert Redis breaker transitions to an atomic Lua script and add Redis latency/failure SLOs.
3. Add per-user rate limits, tracing, dashboards, and a larger labelled cache-quality eval set.
