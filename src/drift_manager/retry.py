"""Bounded retry with exponential backoff for transient CloudFormation errors.

Shared by the Drift_Detector (task 5.9) and the Reconciler (task 8.2). Design
error-handling row: "Transient CFN API errors (throttling) -> bounded retry
with backoff within the per-stack timeout window; exhaustion maps to
UNKNOWN/Failed".

The helper is deliberately generic:

- It wraps any zero-argument callable (typically a ``lambda`` around a
  ``CfnClient`` method).
- Backoff sleeps go through an injected clock exposing ``monotonic()`` and
  ``sleep()``, so tests drive it with the ``FakeClock``.
- An optional absolute ``deadline`` (a ``clock.monotonic()`` reading) bounds
  the retries: a backoff sleep that would end past the deadline is not taken,
  so retrying never extends past the caller's timeout window.
- Only transient errors are retried. Anything else is re-raised immediately,
  unchanged. Exhaustion raises :class:`RetryExhaustedError` carrying the last
  transient error, which callers record as ``Unknown(reason)`` / ``Failed``.

Classification does not import botocore (it is not a dependency). It
duck-types botocore's ``ClientError`` via ``exc.response["Error"]["Code"]`` and
recognises a handful of transport-level error class names, plus the built-in
``ConnectionError`` / ``TimeoutError`` and the explicit :class:`TransientError`
marker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, TypeVar

T = TypeVar("T")

#: CloudFormation / AWS error codes treated as transient (throttling and
#: server-side availability errors).
TRANSIENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "ThrottledException",
        "RequestThrottled",
        "RequestThrottledException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "SlowDown",
        "ProvisionedThroughputExceededException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "InternalFailure",
        "InternalError",
        "InternalServiceError",
        "RequestTimeout",
        "RequestTimeoutException",
    }
)

#: Exception class names (botocore transport errors) treated as transient.
TRANSIENT_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "EndpointConnectionError",
        "ConnectionClosedError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
    }
)


class RetryClock(Protocol):
    """Clock surface the retry helper needs."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class TransientError(Exception):
    """Explicit marker for an error that is safe to retry."""


class RetryExhaustedError(Exception):
    """Raised when a transient error persists past the retry budget.

    ``last_error`` is the final transient error observed; ``attempts`` is the
    number of calls made. ``str()`` includes the last error so it can be used
    directly as a recorded failure reason.
    """

    def __init__(self, last_error: BaseException, attempts: int, why: str) -> None:
        self.last_error = last_error
        self.attempts = attempts
        self.why = why
        super().__init__(
            f"transient error persisted after {attempts} attempt(s) ({why}): "
            f"{last_error}"
        )


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff parameters.

    The delay before retry ``n`` (1-based) is
    ``min(max_delay, base_delay * multiplier ** (n - 1))``. At most
    ``max_attempts`` calls are made in total.
    """

    max_attempts: int = 5
    base_delay: float = 1.0
    multiplier: float = 2.0
    max_delay: float = 20.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays must be non-negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")

    def delay_for(self, retry_number: int) -> float:
        """Backoff delay, in seconds, before the ``retry_number``-th retry."""
        return min(self.max_delay, self.base_delay * self.multiplier ** (retry_number - 1))


#: Default policy: up to 5 attempts, sleeping 1s, 2s, 4s, 8s between them.
DEFAULT_RETRY_POLICY = RetryPolicy()


def error_code(exc: BaseException) -> Optional[str]:
    """Return the AWS error code of a botocore-style ``ClientError``, if any."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return None


def is_transient_error(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` is a throttling / transient error."""
    if isinstance(exc, (TransientError, ConnectionError, TimeoutError)):
        return True
    code = error_code(exc)
    if code is not None and code in TRANSIENT_ERROR_CODES:
        return True
    return any(cls.__name__ in TRANSIENT_EXCEPTION_NAMES for cls in type(exc).__mro__)


def call_with_retry(
    fn: Callable[[], T],
    *,
    clock: RetryClock,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    deadline: Optional[float] = None,
    is_transient: Callable[[BaseException], bool] = is_transient_error,
) -> T:
    """Call ``fn``, retrying transient errors with bounded exponential backoff.

    - Non-transient errors propagate immediately (no retry).
    - A transient error is retried after ``policy.delay_for(n)`` seconds,
      slept via ``clock``, up to ``policy.max_attempts`` total calls.
    - If ``deadline`` is given, no backoff sleep is taken that would end after
      it, so retrying stays within the caller's timeout window.
    - When the budget (attempts or deadline) is exhausted,
      :class:`RetryExhaustedError` is raised from the last transient error.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified below
            if not is_transient(exc):
                raise
            if attempt >= policy.max_attempts:
                raise RetryExhaustedError(
                    exc, attempt, f"retry limit of {policy.max_attempts} attempts reached"
                ) from exc
            delay = policy.delay_for(attempt)
            if deadline is not None and clock.monotonic() + delay > deadline:
                raise RetryExhaustedError(
                    exc, attempt, "retry backoff would exceed the timeout window"
                ) from exc
            clock.sleep(delay)
