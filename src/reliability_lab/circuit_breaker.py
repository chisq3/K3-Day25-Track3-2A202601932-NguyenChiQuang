from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Thread-safe three-state circuit breaker.

    - CLOSED: calls pass through; count failures.
    - OPEN: fail fast until reset timeout elapses.
    - HALF_OPEN: allow a probe; close on success or re-open on failure.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Reject invalid breaker settings before they reach the state machine."""
        if not self.name.strip():
            raise ValueError("Circuit breaker name must not be empty")
        if self.failure_threshold <= 0:
            raise ValueError("failure_threshold must be greater than zero")
        if self.reset_timeout_seconds <= 0:
            raise ValueError("reset_timeout_seconds must be greater than zero")
        if self.success_threshold <= 0:
            raise ValueError("success_threshold must be greater than zero")

    def allow_request(self) -> bool:
        """Return whether a request should be attempted for the current state."""
        with self._lock:
            if self.state in {CircuitState.CLOSED, CircuitState.HALF_OPEN}:
                return True

            # An OPEN breaker without a timestamp cannot prove that its timeout elapsed.
            if self.opened_at is None:
                return False

            elapsed = time.monotonic() - self.opened_at
            if elapsed < self.reset_timeout_seconds:
                return False

            self.success_count = 0
            self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
            return True

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call a function and update breaker state from its outcome."""
        if not self.allow_request():
            raise CircuitOpenError(f"Circuit '{self.name}' is open")

        # Never hold the state lock while running slow provider code.
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise

        self.record_success()
        return result

    def record_success(self) -> None:
        """Record a successful provider call and close a recovered breaker."""
        with self._lock:
            self.failure_count = 0
            self.success_count += 1

            if (
                self.state == CircuitState.HALF_OPEN
                and self.success_count >= self.success_threshold
            ):
                self._transition(CircuitState.CLOSED, "probe_success")
                self.success_count = 0
                self.opened_at = None

    def record_failure(self) -> None:
        """Record a failure, opening or re-opening the breaker when required."""
        with self._lock:
            self.failure_count += 1
            self.success_count = 0

            if self.state == CircuitState.HALF_OPEN:
                self.opened_at = time.monotonic()
                self._transition(CircuitState.OPEN, "probe_failure")
            elif (
                self.state == CircuitState.CLOSED
                and self.failure_count >= self.failure_threshold
            ):
                self.opened_at = time.monotonic()
                self._transition(CircuitState.OPEN, "failure_threshold_reached")

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        with self._lock:
            if self.state == new_state:
                return
            self.transition_log.append(
                {
                    "from": self.state.value,
                    "to": new_state.value,
                    "reason": reason,
                    "ts": time.time(),
                }
            )
            self.state = new_state
