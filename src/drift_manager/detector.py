"""Drift_Detector: per-stack detection state machine and bounded-concurrency pool.

Task 5.1 implements the per-stack state machine (:class:`StackDetection`).
Task 5.2 adds the bounded-concurrency worker pool and the bulk completion
signal on top of it (:class:`BulkDetection` / :func:`detect_all`).

Per-stack state machine (design "Per-stack detection state machine")::

    Pending --stack state disallows detection--> Skipped
    Pending --DetectStackDrift ok--------------> Detecting
    Pending --initiation error-----------------> Unknown
    Detecting --DETECTION_IN_PROGRESS, elapsed < 300s--> Detecting (poll again in 5s)
    Detecting --DETECTION_COMPLETE-------------> Completed
    Detecting --DETECTION_FAILED---------------> Unknown
    Detecting --elapsed >= 300s----------------> Unknown (timeout)

``Completed``, ``Unknown`` and ``Skipped`` are terminal: once reached, the
machine never calls CloudFormation again and its recorded outcome never changes
(design Properties 8 and 9; requirements 2.4-2.6).

Timing is driven by an injected clock exposing ``monotonic()`` and ``sleep()``
(the ``FakeClock`` in tests, :class:`SystemClock` in production), so the 5s poll
cadence and the 300s timeout are deterministic under test.

Transient CloudFormation errors (throttling, service unavailability) raised by
``detect_stack_drift``, ``describe_stack_drift_detection_status`` and
``describe_stack_resource_drifts`` are retried with bounded exponential backoff
via :func:`drift_manager.retry.call_with_retry` (task 5.9). Backoff sleeps go
through the injected clock and are capped by the per-stack timeout window,
which opens at the first initiation attempt. Retry exhaustion and any
non-transient exception are recorded as ``Unknown(reason)`` rather than
aborting the bulk operation. Without errors, no extra sleeping occurs, so the
5s poll cadence is unchanged.
"""

from __future__ import annotations

import time
from collections import deque
from enum import Enum
from typing import Any, Callable, Optional, Protocol

from .cfn_client import CfnClient
from .models import (
    ABSENT,
    Detected,
    DetectionOutcome,
    DetectionResults,
    OperationKind,
    DifferenceType,
    DriftStatus,
    PropertyDifference,
    ResourceDrift,
    ResourceDriftStatus,
    Skipped,
    StackDetectionOutcome,
    Unknown,
)
from .progress import Operation, ProgressTracker
from .retry import DEFAULT_RETRY_POLICY, RetryPolicy, call_with_retry
from .selector import ConfirmedSet, Stack, require_confirmed

#: Interval, in seconds, between detection-status polls (requirement 2.2).
POLL_INTERVAL_SECONDS: float = 5.0

#: Per-stack timeout, in seconds, measured from initiation (requirement 2.6).
PER_STACK_TIMEOUT_SECONDS: float = 300.0

#: Stack statuses on which CloudFormation permits drift detection. Any other
#: status (``*_IN_PROGRESS``, ``*_FAILED`` creates, ``DELETE_*``, ``REVIEW_IN_PROGRESS``
#: ...) is skipped without initiating detection (requirement 2.5).
DETECTABLE_STACK_STATUSES: frozenset[str] = frozenset(
    {
        "CREATE_COMPLETE",
        "UPDATE_COMPLETE",
        "UPDATE_ROLLBACK_COMPLETE",
        "UPDATE_ROLLBACK_FAILED",
        "IMPORT_COMPLETE",
        "IMPORT_ROLLBACK_COMPLETE",
        "IMPORT_ROLLBACK_FAILED",
    }
)

#: Resource drift statuses that count as a "drifted resource" in a result.
_DRIFTED_RESOURCE_STATUSES = frozenset(
    {ResourceDriftStatus.MODIFIED, ResourceDriftStatus.DELETED}
)


class DetectionClock(Protocol):
    """Clock surface the detector needs: a monotonic reading and a sleep."""

    def monotonic(self) -> float:
        """Return a monotonically increasing time in seconds."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block (or, for a fake clock, advance time) for ``seconds``."""
        ...


class SystemClock:
    """Real clock for production use."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class DetectionState(str, Enum):
    """States of the per-stack detection machine."""

    PENDING = "Pending"
    DETECTING = "Detecting"
    COMPLETED = "Completed"
    UNKNOWN = "Unknown"
    SKIPPED = "Skipped"


#: States from which no further transition or CloudFormation call occurs.
TERMINAL_STATES: frozenset[DetectionState] = frozenset(
    {DetectionState.COMPLETED, DetectionState.UNKNOWN, DetectionState.SKIPPED}
)


class StackDetection:
    """Drives drift detection for a single stack through the state machine.

    Use :meth:`step` to perform exactly one transition (handy for a scheduler
    such as the 5.2 worker pool, which can consult :attr:`next_poll_at`), or
    :meth:`run` to drive the stack to a terminal state, sleeping
    ``poll_interval`` between polls. ``step`` only sleeps for retry backoff
    after a transient CloudFormation error.
    """

    def __init__(
        self,
        cfn: CfnClient,
        clock: DetectionClock,
        stack: Stack,
        *,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        timeout: float = PER_STACK_TIMEOUT_SECONDS,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        self._cfn = cfn
        self._clock = clock
        self._stack = stack
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._retry_policy = retry_policy

        self._state = DetectionState.PENDING
        self._outcome: Optional[DetectionOutcome] = None
        self._detection_id: Optional[str] = None
        self._started_at: Optional[float] = None
        self._next_poll_at: Optional[float] = None

    # -- Introspection -------------------------------------------------------

    @property
    def stack(self) -> Stack:
        return self._stack

    @property
    def state(self) -> DetectionState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    @property
    def next_poll_at(self) -> Optional[float]:
        """Clock time at which the next poll is due (``None`` unless Detecting)."""
        return self._next_poll_at if self._state is DetectionState.DETECTING else None

    @property
    def outcome(self) -> Optional[StackDetectionOutcome]:
        """The recorded terminal outcome, or ``None`` while not yet terminal."""
        if self._outcome is None:
            return None
        return StackDetectionOutcome(
            stackName=self._stack.name,
            outcome=self._outcome,
            stackId=self._stack.stackId,
        )

    # -- Driving the machine -------------------------------------------------

    def step(self) -> DetectionState:
        """Perform one transition and return the resulting state.

        Does not sleep for the poll interval; it only sleeps (via the injected
        clock) for retry backoff after a transient CloudFormation error. A
        terminal machine is final: this is a no-op that makes no CloudFormation
        call.
        """
        if self.is_terminal:
            return self._state
        if self._state is DetectionState.PENDING:
            self._start()
        else:
            self._poll()
        return self._state

    def run(self) -> StackDetectionOutcome:
        """Drive the machine to a terminal state and return its outcome."""
        while not self.is_terminal:
            if self._state is DetectionState.DETECTING:
                self._clock.sleep(self._poll_interval)
            self.step()
        outcome = self.outcome
        assert outcome is not None
        return outcome

    # -- Transitions ---------------------------------------------------------

    def _start(self) -> None:
        """Pending -> Skipped | Detecting | Unknown."""
        if self._stack.status not in DETECTABLE_STACK_STATUSES:
            self._finish(
                DetectionState.SKIPPED,
                Skipped(
                    reason=(
                        f"stack status {self._stack.status!r} does not permit "
                        "drift detection"
                    )
                ),
            )
            return

        # The per-stack timeout window opens at the first initiation attempt so
        # that retrying a throttled initiation also counts against it.
        self._started_at = self._clock.monotonic()
        try:
            response = self._call(
                lambda: self._cfn.detect_stack_drift(StackName=self._stack.name)
            )
            detection_id = response["StackDriftDetectionId"]
        except Exception as exc:  # noqa: BLE001 - recorded as the failure reason
            self._finish(
                DetectionState.UNKNOWN,
                Unknown(reason=f"failed to initiate drift detection: {exc}"),
            )
            return

        self._detection_id = detection_id
        # Equal to ``_started_at + poll_interval`` when no retry occurred.
        self._next_poll_at = self._clock.monotonic() + self._poll_interval
        self._state = DetectionState.DETECTING

    def _call(self, fn: Callable[[], Any]) -> Any:
        """Invoke a CloudFormation call with retry bounded by the timeout window."""
        assert self._started_at is not None
        return call_with_retry(
            fn,
            clock=self._clock,
            policy=self._retry_policy,
            deadline=self._started_at + self._timeout,
        )

    def _poll(self) -> None:
        """Detecting -> Detecting | Completed | Unknown."""
        assert self._detection_id is not None and self._started_at is not None

        try:
            detection_id = self._detection_id
            status = self._call(
                lambda: self._cfn.describe_stack_drift_detection_status(
                    StackDriftDetectionId=detection_id
                )
            )
        except Exception as exc:  # noqa: BLE001 - recorded as the failure reason
            self._finish(
                DetectionState.UNKNOWN,
                Unknown(reason=f"failed to poll drift detection status: {exc}"),
            )
            return

        detection_status = status.get("DetectionStatus")

        if detection_status == "DETECTION_COMPLETE":
            self._complete(status)
        elif detection_status == "DETECTION_FAILED":
            reason = status.get("DetectionStatusReason") or "no reason reported"
            self._finish(
                DetectionState.UNKNOWN,
                Unknown(reason=f"drift detection failed: {reason}"),
            )
        else:
            # Still in progress (or an unrecognised status): keep waiting until
            # the per-stack timeout elapses.
            now = self._clock.monotonic()
            if now - self._started_at >= self._timeout:
                self._finish(
                    DetectionState.UNKNOWN,
                    Unknown(
                        reason=(
                            "timeout: drift detection did not complete within "
                            f"{self._timeout:g} seconds"
                        )
                    ),
                )
            else:
                self._next_poll_at = now + self._poll_interval

    def _complete(self, status: dict[str, Any]) -> None:
        """Record the reported stack status and drifted resources."""
        try:
            drift_status = DriftStatus(status.get("StackDriftStatus"))
        except ValueError:
            self._finish(
                DetectionState.UNKNOWN,
                Unknown(
                    reason=(
                        "drift detection completed with unrecognised stack drift "
                        f"status {status.get('StackDriftStatus')!r}"
                    )
                ),
            )
            return

        try:
            resources = self._fetch_drifted_resources()
        except Exception as exc:  # noqa: BLE001 - recorded as the failure reason
            self._finish(
                DetectionState.UNKNOWN,
                Unknown(reason=f"failed to retrieve resource drifts: {exc}"),
            )
            return

        self._finish(
            DetectionState.COMPLETED,
            Detected(status=drift_status, driftedResources=resources),
        )

    def _fetch_drifted_resources(self) -> tuple[ResourceDrift, ...]:
        """Collect every page of ``describe_stack_resource_drifts``."""
        resources: list[ResourceDrift] = []
        next_token: Optional[str] = None
        while True:
            kwargs: dict[str, Any] = {"StackName": self._stack.name}
            if next_token is not None:
                kwargs["NextToken"] = next_token
            response = self._call(
                lambda kw=kwargs: self._cfn.describe_stack_resource_drifts(**kw)
            )
            for record in response.get("StackResourceDrifts", []):
                resource = _to_resource_drift(record)
                if resource.resourceDriftStatus in _DRIFTED_RESOURCE_STATUSES:
                    resources.append(resource)
            next_token = response.get("NextToken")
            if not next_token:
                break
        return tuple(resources)

    def _finish(self, state: DetectionState, outcome: DetectionOutcome) -> None:
        """Enter a terminal state, recording its outcome exactly once."""
        self._state = state
        self._outcome = outcome
        self._next_poll_at = None


def detect_stack(
    cfn: CfnClient,
    clock: DetectionClock,
    stack: Stack,
    *,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    timeout: float = PER_STACK_TIMEOUT_SECONDS,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> StackDetectionOutcome:
    """Run drift detection for one stack to its terminal outcome."""
    return StackDetection(
        cfn,
        clock,
        stack,
        poll_interval=poll_interval,
        timeout=timeout,
        retry_policy=retry_policy,
    ).run()


# ---------------------------------------------------------------------------
# Bounded-concurrency worker pool (task 5.2)
# ---------------------------------------------------------------------------

#: Maximum number of stacks with in-flight detection at any instant (req 2.1).
MAX_CONCURRENCY: int = 10


class BulkDetection:
    """Drives detection for a :class:`ConfirmedSet` with bounded concurrency.

    A deterministic, single-threaded scheduler over :class:`StackDetection`
    machines, driven by the injected clock:

    1. *Admit*: while fewer than ``max_concurrency`` stacks are in flight,
       take the next pending stack and start it. A stack that terminates on
       start (skipped, initiation error) releases its slot immediately, so the
       next pending stack is admitted in its place.
    2. *Wait*: sleep until the earliest ``next_poll_at`` among in-flight stacks.
    3. *Poll*: step every in-flight stack whose poll is due; stacks reaching a
       terminal outcome release their slot.

    A slot is held from ``DetectStackDrift`` initiation until a terminal
    outcome, so in-flight count never exceeds ``max_concurrency`` (Property 6).
    Each terminal outcome is reported once to the Progress_Tracker operation
    (Detected -> success, Unknown -> failure, Skipped -> skip). Completion is
    signalled (:attr:`is_complete`, and the optional ``on_complete`` callback
    fires once) when every stack has exactly one terminal outcome (req 2.7).

    Use :meth:`tick` to perform one scheduling round (lets callers observe
    progress between rounds) or :meth:`run` to drive to completion.
    """

    def __init__(
        self,
        cfn: CfnClient,
        clock: DetectionClock,
        confirmed_set: ConfirmedSet,
        *,
        progress: Optional[ProgressTracker] = None,
        max_concurrency: int = MAX_CONCURRENCY,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        timeout: float = PER_STACK_TIMEOUT_SECONDS,
        on_complete: Optional[Callable[[DetectionResults], None]] = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        # Property 5: detection only proceeds on a confirmed set.
        confirmed = require_confirmed(confirmed_set)
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")

        self._clock = clock
        self._max_concurrency = max_concurrency
        self._on_complete = on_complete

        self._machines: tuple[StackDetection, ...] = tuple(
            StackDetection(
                cfn,
                clock,
                stack,
                poll_interval=poll_interval,
                timeout=timeout,
                retry_policy=retry_policy,
            )
            for stack in confirmed.stacks
        )
        self._pending: deque[StackDetection] = deque(self._machines)
        self._in_flight: list[StackDetection] = []
        self._recorded: set[int] = set()
        self._peak_in_flight = 0
        self._results: Optional[DetectionResults] = None

        self._operation: Optional[Operation] = (
            progress.start(
                OperationKind.DETECTION,
                tuple(m.stack.name for m in self._machines),
            )
            if progress is not None
            else None
        )

        # An empty set is trivially complete.
        if not self._machines:
            self._signal_complete()

    # -- Introspection -------------------------------------------------------

    @property
    def operation(self) -> Optional[Operation]:
        """The Progress_Tracker operation receiving per-stack outcomes."""
        return self._operation

    @property
    def in_flight_count(self) -> int:
        return len(self._in_flight)

    @property
    def peak_in_flight(self) -> int:
        """Highest number of simultaneously in-flight stacks observed."""
        return self._peak_in_flight

    @property
    def is_complete(self) -> bool:
        """``True`` once every stack has exactly one terminal outcome."""
        return self._results is not None

    @property
    def results(self) -> Optional[DetectionResults]:
        """Bulk results, available once :attr:`is_complete`."""
        return self._results

    # -- Driving -------------------------------------------------------------

    def tick(self) -> bool:
        """Run one admit/wait/poll round. Returns :attr:`is_complete`."""
        if self.is_complete:
            return True

        self._admit()
        if self._in_flight:
            due_at = min(m.next_poll_at for m in self._in_flight)  # type: ignore[type-var]
            delay = due_at - self._clock.monotonic()
            if delay > 0:
                self._clock.sleep(delay)
            now = self._clock.monotonic()
            for machine in list(self._in_flight):
                poll_at = machine.next_poll_at
                if poll_at is not None and poll_at <= now:
                    machine.step()
                    if machine.is_terminal:
                        self._release(machine)
            # Fill any slots released by this round before reporting.
            self._admit()

        if not self._pending and not self._in_flight:
            self._signal_complete()
        return self.is_complete

    def run(self) -> DetectionResults:
        """Drive every stack to a terminal outcome and return the results."""
        while not self.tick():
            pass
        assert self._results is not None
        return self._results

    # -- Internals -----------------------------------------------------------

    def _admit(self) -> None:
        """Start pending stacks while a slot is free."""
        while self._pending and len(self._in_flight) < self._max_concurrency:
            machine = self._pending.popleft()
            machine.step()  # Pending -> Detecting | Skipped | Unknown
            if machine.is_terminal:
                self._record(machine)
            else:
                self._in_flight.append(machine)
                self._peak_in_flight = max(
                    self._peak_in_flight, len(self._in_flight)
                )

    def _release(self, machine: StackDetection) -> None:
        self._in_flight.remove(machine)
        self._record(machine)

    def _record(self, machine: StackDetection) -> None:
        """Report a terminal outcome to the Progress_Tracker exactly once."""
        key = id(machine)
        if key in self._recorded:
            return
        self._recorded.add(key)
        if self._operation is None:
            return
        outcome = machine.outcome
        assert outcome is not None
        name = machine.stack.name
        result = outcome.outcome
        if isinstance(result, Detected):
            self._operation.record_success(name)
        elif isinstance(result, Unknown):
            self._operation.record_failure(name, result.reason)
        else:
            self._operation.record_skip(name, result.reason)

    def _signal_complete(self) -> None:
        outcomes = []
        for machine in self._machines:
            outcome = machine.outcome
            assert outcome is not None, "completion requires a terminal outcome"
            outcomes.append(outcome)
        self._results = DetectionResults(outcomes=tuple(outcomes))
        if self._on_complete is not None:
            self._on_complete(self._results)


def detect_all(
    cfn: CfnClient,
    clock: DetectionClock,
    confirmed_set: ConfirmedSet,
    *,
    progress: Optional[ProgressTracker] = None,
    max_concurrency: int = MAX_CONCURRENCY,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    timeout: float = PER_STACK_TIMEOUT_SECONDS,
    on_complete: Optional[Callable[[DetectionResults], None]] = None,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> DetectionResults:
    """Run bulk drift detection over ``confirmed_set`` (requirements 2.1, 2.7).

    Returns exactly one :class:`StackDetectionOutcome` per stack, in the
    confirmed set's order.
    """
    return BulkDetection(
        cfn,
        clock,
        confirmed_set,
        progress=progress,
        max_concurrency=max_concurrency,
        poll_interval=poll_interval,
        timeout=timeout,
        on_complete=on_complete,
        retry_policy=retry_policy,
    ).run()


# ---------------------------------------------------------------------------
# CloudFormation record -> model conversion
# ---------------------------------------------------------------------------


def _to_resource_drift(record: dict[str, Any]) -> ResourceDrift:
    try:
        resource_status = ResourceDriftStatus(record.get("StackResourceDriftStatus"))
    except ValueError:
        resource_status = ResourceDriftStatus.UNKNOWN
    return ResourceDrift(
        logicalResourceId=record.get("LogicalResourceId", ""),
        resourceType=record.get("ResourceType", ""),
        resourceDriftStatus=resource_status,
        propertyDifferences=tuple(
            _to_property_difference(d) for d in record.get("PropertyDifferences", [])
        ),
        physicalResourceId=record.get("PhysicalResourceId"),
    )


def _to_property_difference(record: dict[str, Any]) -> PropertyDifference:
    difference_type = DifferenceType(record["DifferenceType"])
    # ADD: value exists only in actual -> expected is absent.
    # REMOVE: value exists only in expected -> actual is absent.
    expected = (
        ABSENT
        if difference_type is DifferenceType.ADD
        else record.get("ExpectedValue")
    )
    actual = (
        ABSENT
        if difference_type is DifferenceType.REMOVE
        else record.get("ActualValue")
    )
    return PropertyDifference(
        propertyPath=record.get("PropertyPath", ""),
        expectedValue=expected,
        actualValue=actual,
        differenceType=difference_type,
    )
