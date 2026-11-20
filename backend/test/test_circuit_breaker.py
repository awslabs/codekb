"""Tests for the run-scoped circuit breaker."""

from src.core.circuit_breaker import CircuitBreaker


class TestCircuitBreaker:
    def test_opens_after_threshold_consecutive_failures(self):
        cb = CircuitBreaker(3)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open
        cb.record_failure()  # third consecutive → open
        assert cb.is_open

    def test_success_resets_consecutive_count(self):
        cb = CircuitBreaker(3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()  # reset
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open  # only 2 since the reset

    def test_zero_threshold_disables(self):
        cb = CircuitBreaker(0)
        for _ in range(100):
            cb.record_failure()
        assert not cb.is_open

    def test_stays_open_once_tripped(self):
        cb = CircuitBreaker(1)
        cb.record_failure()
        assert cb.is_open
        cb.record_success()  # does not close within the run
        assert cb.is_open
