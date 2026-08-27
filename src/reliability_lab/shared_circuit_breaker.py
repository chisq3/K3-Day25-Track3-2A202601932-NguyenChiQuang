"""Optional Redis-backed circuit-breaker state for multi-instance gateways."""

from __future__ import annotations

import time
from typing import Any

from redis.exceptions import LockError, RedisError

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState


class SharedRedisCircuitBreaker(CircuitBreaker):
    """Synchronize breaker counters and state through a Redis hash.

    A short Redis distributed lock makes each load/mutate/persist operation atomic
    across gateway processes. Provider calls remain outside the lock, just like the
    in-memory breaker, so slow upstream calls never serialize all traffic.
    """

    __slots__ = (
        "_graceful_degradation",
        "_redis",
        "_redis_key",
        "_state_ttl_seconds",
    )

    def __init__(
        self,
        name: str,
        failure_threshold: int,
        reset_timeout_seconds: float,
        success_threshold: int,
        *,
        redis_url: str,
        prefix: str = "rl:cb:",
        state_ttl_seconds: int = 300,
        graceful_degradation: bool = True,
    ) -> None:
        import redis as redis_lib

        super().__init__(
            name=name,
            failure_threshold=failure_threshold,
            reset_timeout_seconds=reset_timeout_seconds,
            success_threshold=success_threshold,
        )
        if not prefix:
            raise ValueError("prefix must not be empty")
        if state_ttl_seconds <= 0:
            raise ValueError("state_ttl_seconds must be greater than zero")
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._redis_key = f"{prefix}{name}"
        self._state_ttl_seconds = state_ttl_seconds
        self._graceful_degradation = graceful_degradation

    @property
    def redis_key(self) -> str:
        return self._redis_key

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except RedisError:
            return False

    def allow_request(self) -> bool:
        try:
            with self._redis.lock(
                f"{self._redis_key}:lock",
                timeout=5,
                blocking_timeout=2,
            ):
                self._load_shared_state()
                allowed = super().allow_request()
                self._persist_shared_state()
                return allowed
        except (RedisError, LockError):
            if not self._graceful_degradation:
                raise
            return super().allow_request()

    def record_success(self) -> None:
        try:
            with self._redis.lock(
                f"{self._redis_key}:lock",
                timeout=5,
                blocking_timeout=2,
            ):
                self._load_shared_state()
                super().record_success()
                self._persist_shared_state()
        except (RedisError, LockError):
            if not self._graceful_degradation:
                raise
            super().record_success()

    def record_failure(self) -> None:
        try:
            with self._redis.lock(
                f"{self._redis_key}:lock",
                timeout=5,
                blocking_timeout=2,
            ):
                self._load_shared_state()
                super().record_failure()
                self._persist_shared_state()
        except (RedisError, LockError):
            if not self._graceful_degradation:
                raise
            super().record_failure()

    def shared_snapshot(self) -> dict[str, str]:
        """Return the persisted state for tests and operational evidence."""
        result = self._redis.hgetall(self._redis_key)
        return {str(key): str(value) for key, value in result.items()}

    def shared_ttl(self) -> int:
        return int(self._redis.ttl(self._redis_key))

    def reset_shared_state(self) -> None:
        """Reset only this provider's namespaced state."""
        self._redis.delete(self._redis_key)
        with self._lock:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0
            self.opened_at = None
            self.transition_log.clear()

    def close(self) -> None:
        self._redis.close()

    def _load_shared_state(self) -> None:
        raw = self._redis.hgetall(self._redis_key)
        if not raw:
            self._persist_shared_state()
            return

        with self._lock:
            self.state = CircuitState(raw.get("state", CircuitState.CLOSED.value))
            self.failure_count = int(raw.get("failure_count", 0))
            self.success_count = int(raw.get("success_count", 0))
            opened_at_epoch = raw.get("opened_at_epoch", "")
            if opened_at_epoch:
                elapsed = max(0.0, time.time() - float(opened_at_epoch))
                self.opened_at = time.monotonic() - elapsed
            else:
                self.opened_at = None

    def _persist_shared_state(self) -> None:
        opened_at_epoch = ""
        if self.opened_at is not None:
            elapsed = max(0.0, time.monotonic() - self.opened_at)
            opened_at_epoch = str(time.time() - elapsed)
        with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.hset(
                self._redis_key,
                mapping={
                    "state": self.state.value,
                    "failure_count": self.failure_count,
                    "success_count": self.success_count,
                    "opened_at_epoch": opened_at_epoch,
                },
            )
            pipeline.expire(self._redis_key, self._state_ttl_seconds)
            pipeline.execute()
