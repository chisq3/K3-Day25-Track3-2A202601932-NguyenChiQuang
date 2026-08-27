"""Property, edge-case, and concurrency tests for the in-memory cache."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reliability_lab.cache import ResponseCache


@given(text=st.text())
def test_similarity_identity_is_one(text: str) -> None:
    assert ResponseCache.similarity(text, text) == 1.0


@given(left=st.text(), right=st.text())
def test_similarity_is_symmetric_and_bounded(left: str, right: str) -> None:
    forward = ResponseCache.similarity(left, right)
    reverse = ResponseCache.similarity(right, left)

    assert forward == pytest.approx(reverse)
    assert 0.0 <= forward <= 1.0


def test_similarity_is_case_insensitive() -> None:
    assert ResponseCache.similarity("Circuit Breaker", "circuit breaker") == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("ttl", "threshold", "message"),
    [
        (0, 0.5, "ttl_seconds"),
        (-1, 0.5, "ttl_seconds"),
        (1, -0.1, "similarity_threshold"),
        (1, 1.1, "similarity_threshold"),
    ],
)
def test_invalid_configuration_is_rejected(ttl: int, threshold: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ResponseCache(ttl_seconds=ttl, similarity_threshold=threshold)


def test_metadata_is_copied_on_set() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    metadata = {"provider": "primary"}
    cache.set("hello world", "response", metadata)

    metadata["provider"] = "changed"

    assert cache._entries[0].metadata == {"provider": "primary"}


def test_expired_entries_are_physically_evicted() -> None:
    cache = ResponseCache(ttl_seconds=1, similarity_threshold=0.5)
    cache.set("expired query", "response")
    cache._entries[0].created_at = time.time() - 2

    cached, _ = cache.get("expired query")

    assert cached is None
    assert cache._entries == []


def test_false_hit_log_contains_auditable_evidence() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Refund policy for 2024", "old policy")

    cached, score = cache.get("Refund policy for 2026")

    assert cached is None
    assert cache.false_hit_log == [
        {
            "query": "Refund policy for 2026",
            "cached_key": "Refund policy for 2024",
            "score": score,
            "reason": "date_or_number_mismatch",
            "ts": cache.false_hit_log[0]["ts"],
        }
    ]
    assert isinstance(cache.false_hit_log[0]["ts"], float)


def test_different_numeric_constraints_are_rejected() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.9)
    cache.set("Summarize the admission FAQ in 5 bullets", "five bullets")

    cached, score = cache.get("Summarize the admission FAQ in 3 bullets")

    assert score >= cache.similarity_threshold
    assert cached is None
    assert cache.false_hit_log[0]["reason"] == "date_or_number_mismatch"


def test_concurrent_get_and_set_is_safe() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.99)

    def store_and_read(index: int) -> str | None:
        query = f"unique cache query token-{index}"
        value = f"response-{index}"
        cache.set(query, value)
        cached, _ = cache.get(query)
        return cached

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(store_and_read, range(64)))

    assert all(result is not None for result in results)
    assert len(cache._entries) == 64


def test_concurrent_privacy_queries_are_never_stored() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda index: cache.set(f"account balance for user {index}", "secret"),
                range(64),
            )
        )

    assert cache._entries == []
