"""Test doubles for deterministic drift_manager tests.

Houses the :class:`FakeClock` and the in-memory :class:`FakeCfnClient` that let
the orchestration logic (selector, detector, reporter, reconciler) run without
AWS. Both are driven by a controllable clock so polling cadence (5s), the
per-stack timeout (300s), and the 2s progress-update window can be asserted
deterministically, and every CloudFormation call is recorded (count, order,
timestamp) so cadence, gating, and concurrency bounds can be checked by later
tests. Implemented by task 1.4.

Design references:
- ``CfnClient`` protocol: ``src/drift_manager/cfn_client.py``
- Detection state machine / polling / timeout: design "Drift_Detector"
- Reconciliation primitives (update vs import change set): design "Reconciler"

Dictionaries use the CloudFormation PascalCase key convention, matching the
protocol docstrings so a test asserting against the fake reads the same as it
would against the real boto3 adapter.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# FakeClock
# ---------------------------------------------------------------------------


class FakeClock:
    """A deterministic, controllable clock with no real wall-clock sleeping.

    Time is a monotonic float number of seconds starting at ``start`` (default
    ``0.0``). ``time`` and ``monotonic`` read the current value; ``advance`` and
    ``sleep`` move it forward. ``sleep`` is intentionally identical to
    ``advance`` so code under test can call ``clock.sleep(5)`` for its poll
    interval and the test observes the elapsed time without any real delay.

    The clock is thread-safe so it can back the detector's bounded-concurrency
    worker pool during tests.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self._lock = threading.Lock()

    def time(self) -> float:
        """Return the current time in seconds (wall-clock analogue)."""
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        """Return the current monotonic time in seconds.

        Identical to :meth:`time` for the fake; provided so callers that prefer
        a monotonic source for measuring elapsed intervals have one.
        """
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> float:
        """Move the clock forward by ``seconds`` and return the new time."""
        if seconds < 0:
            raise ValueError("cannot advance the clock backwards")
        with self._lock:
            self._now += float(seconds)
            return self._now

    def sleep(self, seconds: float) -> None:
        """Advance the clock by ``seconds`` without any real delay."""
        self.advance(seconds)


# ---------------------------------------------------------------------------
# Scripting data structures
# ---------------------------------------------------------------------------


@dataclass
class ScriptedStack:
    """A scripted entry in the fake stack inventory.

    Attributes mirror the CloudFormation shapes the ``CfnClient`` protocol
    exchanges. ``tags`` is a plain key/value mapping; it is serialised to the
    CloudFormation ``[{"Key", "Value"}]`` form on the way out.
    """

    name: str
    region: str = "us-east-1"
    status: str = "CREATE_COMPLETE"
    tags: dict[str, str] = field(default_factory=dict)
    stack_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.stack_id is None:
            self.stack_id = (
                f"arn:aws:cloudformation:{self.region}:123456789012:"
                f"stack/{self.name}/{self.name}-id"
            )

    def to_summary(self) -> dict[str, Any]:
        """Return the ``StackSummaries`` entry shape for ``list_stacks``."""
        return {
            "StackName": self.name,
            "StackId": self.stack_id,
            "StackStatus": self.status,
            "Region": self.region,
        }

    def to_description(self) -> dict[str, Any]:
        """Return the ``Stacks`` entry shape for ``describe_stacks``."""
        return {
            "StackName": self.name,
            "StackId": self.stack_id,
            "StackStatus": self.status,
            "Region": self.region,
            "Tags": [{"Key": k, "Value": v} for k, v in self.tags.items()],
        }


@dataclass
class ScriptedDetection:
    """A scripted drift-detection progression for one stack.

    ``status_progression`` is the ordered sequence of ``DetectionStatus`` values
    returned by successive ``describe_stack_drift_detection_status`` calls (for
    example ``["DETECTION_IN_PROGRESS", "DETECTION_IN_PROGRESS",
    "DETECTION_COMPLETE"]``). The last value is repeated once the sequence is
    exhausted so a caller that polls past completion keeps seeing the terminal
    status.

    ``stack_drift_status`` is the ``StackDriftStatus`` reported once detection
    completes. ``detection_status_reason`` is surfaced on ``DETECTION_FAILED``.
    ``resource_drift_pages`` is a list of pages; each page is a list of
    per-resource drift records, served one page per
    ``describe_stack_resource_drifts`` call and linked via ``NextToken``.
    ``initiation_error`` (if set) is raised by ``detect_stack_drift`` to model
    an initiation failure.
    """

    status_progression: list[str] = field(
        default_factory=lambda: ["DETECTION_COMPLETE"]
    )
    stack_drift_status: str = "IN_SYNC"
    detection_status_reason: Optional[str] = None
    drifted_resource_count: Optional[int] = None
    resource_drift_pages: list[list[dict[str, Any]]] = field(default_factory=list)
    initiation_error: Optional[Exception] = None


@dataclass
class ScriptedReconciliation:
    """Scripted reconciliation outcome for one stack / change-set operation.

    When ``error`` is set, the corresponding reconciliation primitive
    (``create_change_set`` / ``execute_change_set`` / ``update_stack``) raises
    it to model a failure; otherwise the call succeeds. ``error`` may be keyed
    by operation when finer control is needed, but the common case is a single
    terminal failure for the stack.
    """

    create_change_set_error: Optional[Exception] = None
    execute_change_set_error: Optional[Exception] = None
    update_stack_error: Optional[Exception] = None


class FakeClientError(Exception):
    """Mimics botocore's ``ClientError`` shape (``response["Error"]["Code"]``).

    ``FakeClientError("Throttling")`` is classified as transient by
    :func:`drift_manager.retry.is_transient_error`; a code such as
    ``"ValidationError"`` is not.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.response = {"Error": {"Code": code, "Message": message or code}}
        super().__init__(f"An error occurred ({code}): {message or code}")


def throttling_error(message: str = "Rate exceeded") -> FakeClientError:
    """Return a CloudFormation-style ``Throttling`` error."""
    return FakeClientError("Throttling", message)


@dataclass
class CallRecord:
    """A single recorded CloudFormation call.

    ``seq`` is the global monotonically increasing call order index; ``time`` is
    the :class:`FakeClock` timestamp at which the call was made; ``kwargs`` is a
    shallow copy of the keyword arguments passed to the method.
    """

    seq: int
    method: str
    time: float
    kwargs: dict[str, Any]


# ---------------------------------------------------------------------------
# FakeCfnClient
# ---------------------------------------------------------------------------


class FakeCfnClient:
    """In-memory :class:`~drift_manager.cfn_client.CfnClient` for tests.

    Backed by a scripted stack inventory and scripted detection /
    reconciliation responses, driven by a :class:`FakeClock`. Every call is
    recorded (count, order, timestamp, kwargs) so later tests can assert polling
    cadence, confirmation gating, and the concurrency bound.

    Scripting helpers:
    - :meth:`add_stack` registers a stack in the inventory.
    - :meth:`script_detection` sets the detection progression for a stack.
    - :meth:`script_reconciliation` sets reconciliation success/failure for a
      stack.

    Call introspection helpers:
    - :attr:`calls` is the ordered list of :class:`CallRecord`.
    - :meth:`call_count` / :meth:`calls_for` / :meth:`times_for` filter by
      method name.
    """

    def __init__(self, clock: Optional[FakeClock] = None) -> None:
        self.clock = clock if clock is not None else FakeClock()

        # Scripted state.
        self._stacks: dict[str, ScriptedStack] = {}
        self._detections: dict[str, ScriptedDetection] = {}
        self._reconciliations: dict[str, ScriptedReconciliation] = {}

        # Runtime bookkeeping.
        #: StackDriftDetectionId -> (stack name, poll index consumed so far).
        self._detection_ids: dict[str, str] = {}
        self._poll_counts: dict[str, int] = {}
        self._detection_counter = 0
        self._change_set_counter = 0
        #: (method, stack name) -> errors raised by successive calls before
        #: the method's normal behaviour resumes.
        self._scripted_errors: dict[tuple[str, str], list[Exception]] = {}

        # Call recording.
        self.calls: list[CallRecord] = []
        self._seq = 0
        self._lock = threading.Lock()

    # -- Scripting -----------------------------------------------------------

    def add_stack(
        self,
        name: str,
        *,
        region: str = "us-east-1",
        status: str = "CREATE_COMPLETE",
        tags: Optional[dict[str, str]] = None,
        stack_id: Optional[str] = None,
    ) -> ScriptedStack:
        """Register a stack in the fake inventory and return it."""
        stack = ScriptedStack(
            name=name,
            region=region,
            status=status,
            tags=dict(tags or {}),
            stack_id=stack_id,
        )
        self._stacks[name] = stack
        return stack

    def script_detection(
        self,
        stack_name: str,
        *,
        status_progression: Optional[list[str]] = None,
        stack_drift_status: str = "IN_SYNC",
        detection_status_reason: Optional[str] = None,
        drifted_resource_count: Optional[int] = None,
        resource_drift_pages: Optional[list[list[dict[str, Any]]]] = None,
        initiation_error: Optional[Exception] = None,
    ) -> ScriptedDetection:
        """Script the detection progression and results for ``stack_name``."""
        detection = ScriptedDetection(
            status_progression=list(
                status_progression
                if status_progression is not None
                else ["DETECTION_COMPLETE"]
            ),
            stack_drift_status=stack_drift_status,
            detection_status_reason=detection_status_reason,
            drifted_resource_count=drifted_resource_count,
            resource_drift_pages=[list(p) for p in (resource_drift_pages or [])],
            initiation_error=initiation_error,
        )
        self._detections[stack_name] = detection
        return detection

    def script_reconciliation(
        self,
        stack_name: str,
        *,
        create_change_set_error: Optional[Exception] = None,
        execute_change_set_error: Optional[Exception] = None,
        update_stack_error: Optional[Exception] = None,
    ) -> ScriptedReconciliation:
        """Script reconciliation success/failure for ``stack_name``."""
        recon = ScriptedReconciliation(
            create_change_set_error=create_change_set_error,
            execute_change_set_error=execute_change_set_error,
            update_stack_error=update_stack_error,
        )
        self._reconciliations[stack_name] = recon
        return recon

    def script_errors(
        self, stack_name: str, method: str, errors: list[Exception]
    ) -> None:
        """Make the next ``len(errors)`` calls of ``method`` for a stack raise.

        Each call to ``method`` concerning ``stack_name`` (still recorded)
        raises the next error in order; once the list is consumed the method
        behaves normally. Use with :func:`throttling_error` to model transient
        throttling, or a non-transient error to model a hard failure. Applies
        to ``detect_stack_drift``, ``describe_stack_drift_detection_status``
        (matched via the detection id's stack), ``describe_stack_resource_drifts``,
        ``create_change_set``, ``execute_change_set`` and ``update_stack``.
        """
        with self._lock:
            self._scripted_errors.setdefault((method, stack_name), []).extend(errors)

    def _raise_scripted_error(self, method: str, stack_name: Optional[str]) -> None:
        if stack_name is None:
            return
        with self._lock:
            queue = self._scripted_errors.get((method, stack_name))
            error = queue.pop(0) if queue else None
        if error is not None:
            raise error

    # -- Call recording helpers ---------------------------------------------

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        with self._lock:
            record = CallRecord(
                seq=self._seq,
                method=method,
                time=self.clock.time(),
                kwargs={k: v for k, v in kwargs.items() if v is not None},
            )
            self.calls.append(record)
            self._seq += 1

    def call_count(self, method: Optional[str] = None) -> int:
        """Return the number of recorded calls, optionally filtered by method."""
        if method is None:
            return len(self.calls)
        return sum(1 for c in self.calls if c.method == method)

    def calls_for(self, method: str) -> list[CallRecord]:
        """Return the recorded calls for ``method`` in call order."""
        return [c for c in self.calls if c.method == method]

    def times_for(self, method: str) -> list[float]:
        """Return the clock timestamps of each call to ``method`` in order."""
        return [c.time for c in self.calls if c.method == method]

    # -- Internal lookups ----------------------------------------------------

    def _stack_by_id(self, stack_id: str) -> Optional[ScriptedStack]:
        for stack in self._stacks.values():
            if stack.stack_id == stack_id:
                return stack
        return None

    # -- CfnClient: stack inventory -----------------------------------------

    def list_stacks(
        self, *, NextToken: Optional[str] = None, **kwargs: Any
    ) -> dict[str, Any]:
        self._record("list_stacks", {"NextToken": NextToken, **kwargs})
        return {
            "StackSummaries": [s.to_summary() for s in self._stacks.values()],
        }

    def describe_stacks(
        self,
        *,
        StackName: Optional[str] = None,
        NextToken: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._record(
            "describe_stacks",
            {"StackName": StackName, "NextToken": NextToken, **kwargs},
        )
        if StackName is not None:
            stack = self._stacks.get(StackName)
            stacks = [stack.to_description()] if stack is not None else []
        else:
            stacks = [s.to_description() for s in self._stacks.values()]
        return {"Stacks": stacks}

    # -- CfnClient: drift detection -----------------------------------------

    def detect_stack_drift(self, *, StackName: str, **kwargs: Any) -> dict[str, Any]:
        self._record("detect_stack_drift", {"StackName": StackName, **kwargs})
        self._raise_scripted_error("detect_stack_drift", StackName)

        detection = self._detections.get(StackName)
        if detection is not None and detection.initiation_error is not None:
            raise detection.initiation_error

        with self._lock:
            self._detection_counter += 1
            detection_id = f"drift-{self._detection_counter:04d}-{StackName}"
            self._detection_ids[detection_id] = StackName
            self._poll_counts[detection_id] = 0
        return {"StackDriftDetectionId": detection_id}

    def describe_stack_drift_detection_status(
        self, *, StackDriftDetectionId: str, **kwargs: Any
    ) -> dict[str, Any]:
        self._record(
            "describe_stack_drift_detection_status",
            {"StackDriftDetectionId": StackDriftDetectionId, **kwargs},
        )

        stack_name = self._detection_ids.get(StackDriftDetectionId)
        if stack_name is None:
            raise KeyError(f"unknown StackDriftDetectionId: {StackDriftDetectionId}")
        self._raise_scripted_error("describe_stack_drift_detection_status", stack_name)

        detection = self._detections.get(stack_name) or ScriptedDetection()
        stack = self._stacks.get(stack_name)

        with self._lock:
            idx = self._poll_counts.get(StackDriftDetectionId, 0)
            progression = detection.status_progression
            # Clamp to the last status so polling past the end stays terminal.
            if idx < len(progression):
                status = progression[idx]
            else:
                status = progression[-1] if progression else "DETECTION_COMPLETE"
            self._poll_counts[StackDriftDetectionId] = idx + 1

        response: dict[str, Any] = {
            "StackDriftDetectionId": StackDriftDetectionId,
            "DetectionStatus": status,
        }
        if stack is not None and stack.stack_id is not None:
            response["StackId"] = stack.stack_id

        if status == "DETECTION_COMPLETE":
            response["StackDriftStatus"] = detection.stack_drift_status
            if detection.drifted_resource_count is not None:
                response["DriftedStackResourceCount"] = (
                    detection.drifted_resource_count
                )
            else:
                response["DriftedStackResourceCount"] = sum(
                    len(page) for page in detection.resource_drift_pages
                )
        elif status == "DETECTION_FAILED":
            if detection.detection_status_reason is not None:
                response["DetectionStatusReason"] = detection.detection_status_reason

        return response

    def describe_stack_resource_drifts(
        self,
        *,
        StackName: str,
        NextToken: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._record(
            "describe_stack_resource_drifts",
            {"StackName": StackName, "NextToken": NextToken, **kwargs},
        )
        self._raise_scripted_error("describe_stack_resource_drifts", StackName)

        detection = self._detections.get(StackName) or ScriptedDetection()
        pages = detection.resource_drift_pages

        page_index = 0 if NextToken is None else int(NextToken)
        if page_index >= len(pages):
            return {"StackResourceDrifts": []}

        response: dict[str, Any] = {"StackResourceDrifts": list(pages[page_index])}
        if page_index + 1 < len(pages):
            response["NextToken"] = str(page_index + 1)
        return response

    # -- CfnClient: reconciliation ------------------------------------------

    def create_change_set(
        self,
        *,
        StackName: str,
        ChangeSetName: str,
        ChangeSetType: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._record(
            "create_change_set",
            {
                "StackName": StackName,
                "ChangeSetName": ChangeSetName,
                "ChangeSetType": ChangeSetType,
                **kwargs,
            },
        )
        self._raise_scripted_error("create_change_set", StackName)
        recon = self._reconciliations.get(StackName)
        if recon is not None and recon.create_change_set_error is not None:
            raise recon.create_change_set_error

        with self._lock:
            self._change_set_counter += 1
            change_set_id = (
                f"arn:aws:cloudformation:::changeSet/{ChangeSetName}/"
                f"{self._change_set_counter:04d}"
            )
        stack = self._stacks.get(StackName)
        stack_id = stack.stack_id if stack is not None else None
        return {"Id": change_set_id, "StackId": stack_id}

    def execute_change_set(
        self, *, ChangeSetName: str, StackName: Optional[str] = None, **kwargs: Any
    ) -> dict[str, Any]:
        self._record(
            "execute_change_set",
            {"ChangeSetName": ChangeSetName, "StackName": StackName, **kwargs},
        )
        self._raise_scripted_error("execute_change_set", StackName)
        if StackName is not None:
            recon = self._reconciliations.get(StackName)
            if recon is not None and recon.execute_change_set_error is not None:
                raise recon.execute_change_set_error
        return {}

    def update_stack(self, *, StackName: str, **kwargs: Any) -> dict[str, Any]:
        self._record("update_stack", {"StackName": StackName, **kwargs})
        self._raise_scripted_error("update_stack", StackName)
        recon = self._reconciliations.get(StackName)
        if recon is not None and recon.update_stack_error is not None:
            raise recon.update_stack_error
        stack = self._stacks.get(StackName)
        stack_id = stack.stack_id if stack is not None else None
        return {"StackId": stack_id}
