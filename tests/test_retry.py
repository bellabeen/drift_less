"""Unit tests for retry and backoff of transient CloudFormation errors (task 5.10).

Covers the shared helper (``drift_manager.retry``) and its application in
``StackDetection``: throttled-then-success completes detection, exhausted
retries yield ``Unknown(reason)``, non-transient errors are not retried, and
backoff never extends past the 300s per-stack timeout (fake clock).

Requirements: 2.4, 2.6.
"""

from __future__ import annotations

import pytest

from drift_manager.detector import (
    PER_STACK_TIMEOUT_SECONDS,
    DetectionState,
    StackDetection,
    detect_stack,
)
from drift_manager.models import Detected, DriftStatus, Unknown
from drift_manager.retry import (
    RetryExhaustedError,
    RetryPolicy,
    call_with_retry,
    is_transient_error,
)
from drift_manager.selector import Stack

from tests.fakes import FakeCfnClient, FakeClientError, FakeClock, throttling_error

IN_PROGRESS = "DETECTION_IN_PROGRESS"
COMPLETE = "DETECTION_COMPLETE"
DETECT = "detect_stack_drift"
STATUS = "describe_stack_drift_detection_status"
RESOURCES = "describe_stack_resource_drifts"


def _stack(cfn: FakeCfnClient, name: str = "s1") -> Stack:
    scripted = cfn.add_stack(name)
    return Stack(
        name=name,
        region=scripted.region,
        status=scripted.status,
        stackId=scripted.stack_id,
    )


class _Flaky:
    """Callable that raises the scripted errors in order, then returns ``value``."""

    def __init__(self, errors: list[Exception], value: object = "ok") -> None:
        self.errors = list(errors)
        self.value = value
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.value


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def test_throttling_and_transport_errors_are_transient() -> None:
    assert is_transient_error(throttling_error())
    assert is_transient_error(FakeClientError("ServiceUnavailable"))
    assert is_transient_error(ConnectionError("reset"))
    assert is_transient_error(TimeoutError("slow"))


def test_validation_and_plain_errors_are_not_transient() -> None:
    assert not is_transient_error(FakeClientError("ValidationError"))
    assert not is_transient_error(FakeClientError("AccessDenied"))
    assert not is_transient_error(ValueError("boom"))


# ---------------------------------------------------------------------------
# call_with_retry helper
# ---------------------------------------------------------------------------


def test_helper_retries_throttling_with_exponential_backoff(clock: FakeClock) -> None:
    fn = _Flaky([throttling_error(), throttling_error(), throttling_error()])

    assert call_with_retry(fn, clock=clock) == "ok"
    assert fn.calls == 4
    # Default backoff 1s + 2s + 4s.
    assert clock.monotonic() == 7.0


def test_helper_exhaustion_raises_with_last_error(clock: FakeClock) -> None:
    last = throttling_error("final")
    fn = _Flaky([throttling_error()] * 4 + [last])

    with pytest.raises(RetryExhaustedError) as info:
        call_with_retry(fn, clock=clock)

    assert fn.calls == 5
    assert info.value.attempts == 5
    assert info.value.last_error is last
    assert "final" in str(info.value)
    # Four backoff sleeps between five attempts: 1 + 2 + 4 + 8.
    assert clock.monotonic() == 15.0


def test_helper_does_not_retry_non_transient_errors(clock: FakeClock) -> None:
    error = FakeClientError("ValidationError", "bad stack")
    fn = _Flaky([error])

    with pytest.raises(FakeClientError) as info:
        call_with_retry(fn, clock=clock)

    assert info.value is error
    assert fn.calls == 1
    assert clock.monotonic() == 0.0


def test_helper_backoff_is_capped_by_max_delay() -> None:
    policy = RetryPolicy(max_attempts=10, base_delay=1.0, multiplier=2.0, max_delay=20.0)
    assert [policy.delay_for(n) for n in range(1, 7)] == [1, 2, 4, 8, 16, 20]


def test_helper_never_sleeps_past_deadline(clock: FakeClock) -> None:
    clock.advance(10.0)
    fn = _Flaky([throttling_error()] * 10)

    with pytest.raises(RetryExhaustedError) as info:
        call_with_retry(fn, clock=clock, deadline=15.0)

    # Sleeps 1s (t=11), 2s (t=13); a 4s sleep would end at 17 > 15.
    assert clock.monotonic() == 13.0
    assert fn.calls == 3
    assert "timeout window" in str(info.value)


# ---------------------------------------------------------------------------
# StackDetection integration
# ---------------------------------------------------------------------------


def test_throttled_initiation_then_success_completes_detection(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_detection("s1", status_progression=[COMPLETE], stack_drift_status="IN_SYNC")
    cfn.script_errors("s1", DETECT, [throttling_error(), throttling_error()])

    result = detect_stack(cfn, clock, stack)

    assert result.outcome == Detected(status=DriftStatus.IN_SYNC, driftedResources=())
    assert cfn.call_count(DETECT) == 3
    assert cfn.times_for(DETECT) == [0.0, 1.0, 3.0]


def test_throttled_poll_and_resource_fetch_then_success_completes_detection(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_detection("s1", status_progression=[COMPLETE], stack_drift_status="DRIFTED")
    cfn.script_errors("s1", STATUS, [throttling_error()])
    cfn.script_errors("s1", RESOURCES, [throttling_error(), throttling_error()])

    result = detect_stack(cfn, clock, stack)

    assert isinstance(result.outcome, Detected)
    assert result.outcome.status is DriftStatus.DRIFTED
    assert cfn.call_count(STATUS) == 2
    assert cfn.call_count(RESOURCES) == 3


def test_exhausted_initiation_retries_yield_unknown_with_reason(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_errors("s1", DETECT, [throttling_error("Rate exceeded")] * 5)

    detection = StackDetection(cfn, clock, stack)
    result = detection.run()

    assert detection.state is DetectionState.UNKNOWN
    assert isinstance(result.outcome, Unknown)
    assert "failed to initiate" in result.outcome.reason
    assert "Rate exceeded" in result.outcome.reason
    assert cfn.call_count(DETECT) == 5
    assert cfn.call_count(STATUS) == 0
    # Terminal: further steps make no calls.
    calls_before = cfn.call_count()
    detection.step()
    assert cfn.call_count() == calls_before


def test_exhausted_poll_retries_yield_unknown_with_reason(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_detection("s1", status_progression=[COMPLETE])
    cfn.script_errors("s1", STATUS, [throttling_error("Rate exceeded")] * 5)

    result = detect_stack(cfn, clock, stack)

    assert isinstance(result.outcome, Unknown)
    assert "failed to poll" in result.outcome.reason
    assert "Rate exceeded" in result.outcome.reason
    assert cfn.call_count(STATUS) == 5


def test_non_transient_initiation_error_is_not_retried(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_errors("s1", DETECT, [FakeClientError("ValidationError", "nope")])

    result = detect_stack(cfn, clock, stack)

    assert isinstance(result.outcome, Unknown)
    assert "nope" in result.outcome.reason
    assert cfn.call_count(DETECT) == 1
    assert clock.monotonic() == 0.0


def test_non_transient_resource_fetch_error_is_not_retried(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_detection("s1", status_progression=[COMPLETE], stack_drift_status="DRIFTED")
    cfn.script_errors("s1", RESOURCES, [FakeClientError("AccessDenied", "denied")])

    result = detect_stack(cfn, clock, stack)

    assert isinstance(result.outcome, Unknown)
    assert "failed to retrieve resource drifts" in result.outcome.reason
    assert cfn.call_count(RESOURCES) == 1


def test_poll_backoff_never_extends_past_timeout(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_detection("s1", status_progression=[IN_PROGRESS])
    detection = StackDetection(cfn, clock, stack)

    assert detection.step() is DetectionState.DETECTING  # initiated at t=0
    clock.advance(296.0)
    cfn.script_errors("s1", STATUS, [throttling_error()] * 5)

    assert detection.step() is DetectionState.UNKNOWN
    # Sleeps 1s (t=297) and 2s (t=299); a 4s sleep would end at 303 > 300.
    assert clock.monotonic() == 299.0
    assert clock.monotonic() <= PER_STACK_TIMEOUT_SECONDS
    assert cfn.times_for(STATUS) == [296.0, 297.0, 299.0]
    outcome = detection.outcome
    assert outcome is not None and isinstance(outcome.outcome, Unknown)
    assert "timeout window" in outcome.outcome.reason


def test_initiation_backoff_never_extends_past_timeout(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    # Large backoff so the timeout window, not the attempt limit, binds.
    policy = RetryPolicy(max_attempts=50, base_delay=10.0, multiplier=2.0, max_delay=1000.0)
    cfn.script_errors("s1", DETECT, [throttling_error()] * 50)

    result = detect_stack(cfn, clock, stack, retry_policy=policy)

    assert isinstance(result.outcome, Unknown)
    # Sleeps 10, 20, 40, 80 (t=150); next 160s would end at 310 > 300.
    assert clock.monotonic() == 150.0
    assert all(t <= PER_STACK_TIMEOUT_SECONDS for t in cfn.times_for(DETECT))
    assert cfn.call_count(DETECT) == 5
