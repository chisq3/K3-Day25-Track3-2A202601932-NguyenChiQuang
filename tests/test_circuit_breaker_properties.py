"""Additional edge-case and property tests for the circuit breaker."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState


@given(failures=st.integers(min_value=0, max_value=50))
def test_failures_below_threshold_keep_circuit_closed(failures: int) -> None:
    breaker = CircuitBreaker(
        "property",
        failure_threshold=failures + 1,
        reset_timeout_seconds=1,
    )

    for _ in range(failures):
        breaker.record_failure()

    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == failures


@given(threshold=st.integers(min_value=1, max_value=50))
def test_reaching_failure_threshold_opens_exactly_once(threshold: int) -> None:
    breaker = CircuitBreaker(
        "property",
        failure_threshold=threshold,
        reset_timeout_seconds=60,
    )

    for _ in range(threshold + 5):
        breaker.record_failure()

    open_transitions = [item for item in breaker.transition_log if item["to"] == "open"]
    assert breaker.state == CircuitState.OPEN
    assert len(open_transitions) == 1
    assert open_transitions[0]["reason"] == "failure_threshold_reached"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": ""}, "name"),
        ({"failure_threshold": 0}, "failure_threshold"),
        ({"reset_timeout_seconds": 0}, "reset_timeout_seconds"),
        ({"success_threshold": 0}, "success_threshold"),
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "name": "test",
        "failure_threshold": 3,
        "reset_timeout_seconds": 1,
        "success_threshold": 1,
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        CircuitBreaker(**values)  # type: ignore[arg-type]


def test_open_failures_do_not_extend_reset_timeout() -> None:
    breaker = CircuitBreaker("test", failure_threshold=1, reset_timeout_seconds=60)
    breaker.record_failure()
    first_opened_at = breaker.opened_at

    breaker.record_failure()

    assert first_opened_at is not None
    assert breaker.opened_at == first_opened_at
    assert len(breaker.transition_log) == 1


def test_open_without_timestamp_fails_safe() -> None:
    breaker = CircuitBreaker("test", failure_threshold=1, reset_timeout_seconds=1)
    breaker.state = CircuitState.OPEN
    breaker.opened_at = None

    assert not breaker.allow_request()


def test_probe_success_clears_open_timestamp() -> None:
    breaker = CircuitBreaker("test", failure_threshold=1, reset_timeout_seconds=1)
    breaker.record_failure()
    breaker.opened_at = time.monotonic() - 2

    assert breaker.allow_request()
    breaker.record_success()

    assert breaker.state == CircuitState.CLOSED
    assert breaker.opened_at is None


def test_concurrent_failures_create_one_open_transition() -> None:
    breaker = CircuitBreaker("concurrent", failure_threshold=3, reset_timeout_seconds=60)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: breaker.record_failure(), range(64)))

    open_transitions = [item for item in breaker.transition_log if item["to"] == "open"]
    assert breaker.state == CircuitState.OPEN
    assert len(open_transitions) == 1
