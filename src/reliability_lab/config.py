from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class ProviderConfig(BaseModel):
    name: str
    fail_rate: float = Field(ge=0.0, le=1.0)
    base_latency_ms: int = Field(gt=0)
    cost_per_1k_tokens: float = Field(ge=0.0)


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = Field(gt=0)
    reset_timeout_seconds: float = Field(gt=0)
    success_threshold: int = Field(gt=0)
    shared_state_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str = Field(default="rl:cb:", min_length=1)
    state_ttl_seconds: int = Field(default=300, gt=0)
    graceful_degradation: bool = True


class CacheConfig(BaseModel):
    enabled: bool = True
    backend: Literal["memory", "redis"] = "memory"
    ttl_seconds: int = Field(gt=0)
    similarity_threshold: float = Field(ge=0.0, le=1.0)
    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str = Field(default="rl:cache:", min_length=1)
    graceful_degradation: bool = False
    redis_retry_interval_seconds: float = Field(default=1.0, ge=0.0)


class LoadTestConfig(BaseModel):
    requests: int = Field(gt=0)
    concurrency: int = Field(default=1, gt=0)
    random_seed: int = 202601932


class BudgetConfig(BaseModel):
    enabled: bool = False
    max_cost: float = Field(default=0.05, gt=0.0)
    warning_ratio: float = Field(default=0.8, gt=0.0, lt=1.0)


class ScenarioConfig(BaseModel):
    name: str
    description: str = ""
    provider_overrides: dict[str, float] = Field(default_factory=dict)
    cache_enabled: bool | None = None
    recovery_provider: str | None = None
    recovery_after_requests: int | None = Field(default=None, gt=0)
    recovery_fail_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class LabConfig(BaseModel):
    providers: list[ProviderConfig]
    circuit_breaker: CircuitBreakerConfig
    cache: CacheConfig
    load_test: LoadTestConfig
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    scenarios: list[ScenarioConfig] = Field(default_factory=list)


def load_config(path: str | Path) -> LabConfig:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text())
    return LabConfig.model_validate(raw)
