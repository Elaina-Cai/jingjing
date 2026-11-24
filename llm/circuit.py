from __future__ import annotations

import time
import structlog
from dataclasses import dataclass, field
from typing import Any

log = structlog.get_logger()


@dataclass
class CircuitState:
    failures: int = 0
    last_failure_time: float = 0.0
    state: str = "closed"
    half_open_allowed: bool = True

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def is_half_open(self) -> bool:
        return self.state == "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1,
    ):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls
        self._circuits: dict[str, CircuitState] = {}

    def _get_circuit(self, name: str) -> CircuitState:
        if name not in self._circuits:
            self._circuits[name] = CircuitState()
        return self._circuits[name]

    def allow(self, name: str) -> bool:
        c = self._get_circuit(name)
        if c.state == "closed":
            return True
        if c.state == "open":
            elapsed = time.time() - c.last_failure_time
            if elapsed >= self._recovery_timeout:
                c.state = "half_open"
                c.half_open_allowed = True
                log.info("circuit_half_open", provider=name)
                return True
            return False
        if c.state == "half_open":
            if c.half_open_allowed:
                c.half_open_allowed = False
                return True
            return False
        return False

    def is_open(self, name: str) -> bool:
        """Non-consuming query: an open circuit blocks new calls (half-open is not open)."""
        state = self._circuits.get(name)
        return bool(state is not None and state.state == "open")

    def record_success(self, name: str):
        c = self._get_circuit(name)
        # 连续失败计数：任何成功都要清零。旧实现只在 half_open 关闭时清零，
        # 历史累计的失败让健康 provider 在下一次孤立失败时被误熔断 60 秒。
        c.failures = 0
        if c.state == "half_open":
            c.state = "closed"
            c.half_open_allowed = True
            log.info("circuit_closed", provider=name)

    def record_failure(self, name: str):
        c = self._get_circuit(name)
        c.failures += 1
        c.last_failure_time = time.time()
        if c.state == "half_open":
            c.state = "open"
            log.warning("circuit_reopened", provider=name)
        elif c.failures >= self._failure_threshold:
            c.state = "open"
            log.warning("circuit_opened", provider=name, failures=c.failures)

    def stats(self) -> dict[str, dict[str, Any]]:
        result = {}
        for name, c in self._circuits.items():
            result[name] = {
                "state": c.state,
                "failures": c.failures,
                "last_failure_ago": round(time.time() - c.last_failure_time, 1) if c.last_failure_time else None,
            }
        return result

    def reset(self, name: str | None = None):
        if name:
            self._circuits.pop(name, None)
        else:
            self._circuits.clear()


class RetryPolicy:
    def __init__(
        self,
        max_retries: int = 2,
        backoff_base: float = 1.0,
        retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    ):
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.retryable_exceptions = retryable_exceptions

    def should_retry(self, attempt: int, error: Exception) -> bool:
        if attempt >= self.max_retries:
            return False
        return isinstance(error, self.retryable_exceptions)

    def delay(self, attempt: int) -> float:
        return self.backoff_base * (2 ** attempt)


_DEFAULT_RETRY = RetryPolicy(
    max_retries=2,
    backoff_base=1.0,
    retryable_exceptions=(ConnectionError, TimeoutError, OSError),
)


def get_retry_policy() -> RetryPolicy:
    return _DEFAULT_RETRY
