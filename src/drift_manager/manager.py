"""Drift_Manager: orchestrates the selection -> detection -> report -> reconcile flow.

Task 10.1. :class:`DriftManager` wires the Stack_Selector, Drift_Detector,
Progress_Tracker, Drift_Reporter, operation summaries and Reconciler into one
operation, held as an explicit phase state machine::

    IDLE --select(Resolved)--> RESOLVED --cancel_selection--> ABORTED
      ^  (Empty/Failed/Rejected     |
      |   selections stay in IDLE)  confirm_selection
      |                             v
      |                         CONFIRMED --start_detection--> DETECTING
      |                                                            |
      |                                         (bulk completion)  v
      |                                                         DETECTED
      |                                                            | report
      |                                                            v
      |                                                         REPORTED
      |                                     select_scope (non-empty) |
      |                                                            v
      |   confirm_scope -> ScopeInvalidated / RejectedEmpty   SCOPE_RECORDED
      |   (stay; re-confirm required)                              | confirm_scope
      |                                                            v  (ConfirmedScope)
      |                                                     SCOPE_CONFIRMED
      |                                                            | reconcile
      |                                                            v
      |                                                        RECONCILED

Every phase-changing call checks the current phase and raises
:class:`PhaseError` when called out of order, so drift detection cannot start
before the resolved stack set is confirmed (requirements 1.9-1.11) and no
reconciliation action is applied before the scope is confirmed (4.4, 4.10).
Those gates are also enforced inside the components themselves
(:func:`~drift_manager.selector.require_confirmed`,
:meth:`~drift_manager.reconciler.Reconciler.require_confirmed`); the
orchestrator adds a clear, operation-level error on top.

The API is synchronous. Detection is driven to completion with the injected
clock (:meth:`DriftManager.detect`), or round by round
(:meth:`DriftManager.start_detection` + :meth:`DriftManager.advance_detection`)
so callers can observe progress snapshots while it is in flight.

The ``CfnClient`` and clock are injected, so the orchestrator runs against the
``FakeCfnClient`` / ``FakeClock`` in tests and a real boto3 adapter /
:class:`~drift_manager.detector.SystemClock` in production.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Optional

from .cfn_client import CfnClient
from .detector import (
    MAX_CONCURRENCY,
    PER_STACK_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
    BulkDetection,
    DetectionClock,
    SystemClock,
)
from .models import (
    DetectionResults,
    DriftReport,
    DriftStatus,
    ProgressSnapshot,
    ReconciliationAction,
    ReconciliationResults,
    SelectionCriterion,
)
from .progress import Operation, ProgressTracker
from .reconciler import (
    ConfirmationResult,
    ConfirmedScope,
    Reconciler,
    ResourceKey,
    Scope,
    ScopedResource,
    ScopeInvalidated,
)
from .reporter import report as build_report
from .reporter import to_json, to_machine_readable
from .retry import DEFAULT_RETRY_POLICY, RetryPolicy
from .selector import (
    Aborted,
    ConfirmedSet,
    Resolved,
    SelectionResult,
    StackSelector,
)
from .summary import SummaryResult, summarize_detection, summarize_reconciliation


class Phase(str, Enum):
    """Phases of a Drift_Manager operation."""

    IDLE = "Idle"
    RESOLVED = "Resolved"
    ABORTED = "Aborted"
    CONFIRMED = "Confirmed"
    DETECTING = "Detecting"
    DETECTED = "Detected"
    REPORTED = "Reported"
    SCOPE_RECORDED = "ScopeRecorded"
    SCOPE_CONFIRMED = "ScopeConfirmed"
    RECONCILED = "Reconciled"


#: Phases in which the consolidated report exists and can be read again.
_REPORT_AVAILABLE = frozenset(
    {
        Phase.REPORTED,
        Phase.SCOPE_RECORDED,
        Phase.SCOPE_CONFIRMED,
        Phase.RECONCILED,
    }
)


class PhaseError(RuntimeError):
    """Raised when an orchestrator call is made out of order."""

    def __init__(self, action: str, phase: Phase, allowed: Iterable[Phase]) -> None:
        self.action = action
        self.phase = phase
        self.allowed = tuple(allowed)
        expected = ", ".join(p.value for p in self.allowed)
        super().__init__(
            f"cannot {action} in phase {phase.value}; expected one of: {expected}"
        )


def drifted_scoped_resources(drift_report: DriftReport) -> tuple[ScopedResource, ...]:
    """Turn the report's DRIFTED entries into selectable :class:`ScopedResource`s.

    Only entries whose stack-level status is ``DRIFTED`` contribute; each of
    their drifted resources is paired with the owning stack name, in report
    order.
    """
    return tuple(
        ScopedResource(stackName=entry.stackName, resource=resource)
        for entry in drift_report.entries
        if entry.status is DriftStatus.DRIFTED
        for resource in entry.driftedResources
    )


class DriftManager:
    """Orchestrates one bulk drift operation and enforces its confirmation gates."""

    def __init__(
        self,
        cfn: CfnClient,
        clock: Optional[DetectionClock] = None,
        *,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        max_concurrency: int = MAX_CONCURRENCY,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        timeout: float = PER_STACK_TIMEOUT_SECONDS,
    ) -> None:
        self._cfn = cfn
        self._clock: DetectionClock = clock if clock is not None else SystemClock()
        self._retry_policy = retry_policy
        self._max_concurrency = max_concurrency
        self._poll_interval = poll_interval
        self._timeout = timeout

        self._tracker = ProgressTracker(self._clock)
        self._selector = StackSelector(cfn)
        self._reconciler = Reconciler(
            cfn, self._clock, progress=self._tracker, retry_policy=retry_policy
        )

        self._phase = Phase.IDLE
        self._selection_result: Optional[SelectionResult] = None
        self._resolved: Optional[Resolved] = None
        self._confirmed_set: Optional[ConfirmedSet] = None
        self._detection: Optional[BulkDetection] = None
        self._detection_results: Optional[DetectionResults] = None
        self._detection_summary: Optional[SummaryResult] = None
        self._report: Optional[DriftReport] = None
        self._pending_scope: Optional[Scope] = None
        self._current_selection: tuple[ScopedResource, ...] = ()
        self._confirmed_scope: Optional[ConfirmedScope] = None
        self._reconciliation_results: Optional[ReconciliationResults] = None
        self._reconciliation_summary: Optional[SummaryResult] = None
        self._latest_operation: Optional[Operation] = None

    # -- State ---------------------------------------------------------------

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def selection_result(self) -> Optional[SelectionResult]:
        """Result of the most recent :meth:`select` call."""
        return self._selection_result

    @property
    def confirmed_set(self) -> Optional[ConfirmedSet]:
        return self._confirmed_set

    @property
    def detection_results(self) -> Optional[DetectionResults]:
        return self._detection_results

    @property
    def detection_summary(self) -> Optional[SummaryResult]:
        """Detection Operation Summary (``Produced`` or ``SummaryError``)."""
        return self._detection_summary

    @property
    def scope(self) -> Optional[Scope]:
        """The scope awaiting confirmation (may be empty), if any."""
        return self._pending_scope

    @property
    def current_selection(self) -> tuple[ScopedResource, ...]:
        """The operator's current reconciliation selection."""
        return self._current_selection

    @property
    def confirmed_scope(self) -> Optional[ConfirmedScope]:
        return self._confirmed_scope

    @property
    def reconciliation_results(self) -> Optional[ReconciliationResults]:
        return self._reconciliation_results

    @property
    def reconciliation_summary(self) -> Optional[SummaryResult]:
        """Reconciliation Operation Summary (``Produced`` or ``SummaryError``)."""
        return self._reconciliation_summary

    @property
    def reconciler(self) -> Reconciler:
        """The Reconciler, exposing post-reconciliation resource drift status."""
        return self._reconciler

    def progress(self) -> Optional[ProgressSnapshot]:
        """Snapshot of the in-flight (or most recent) bulk operation, if any."""
        if self._latest_operation is None:
            return None
        return self._tracker.observe(self._latest_operation)

    def is_operation_complete(self) -> bool:
        """``True`` once the in-flight (or most recent) operation has finished."""
        if self._latest_operation is None:
            return False
        return self._tracker.is_complete(self._latest_operation)

    # -- Phase guard ---------------------------------------------------------

    def _require(self, action: str, *allowed: Phase) -> None:
        if self._phase not in allowed:
            raise PhaseError(action, self._phase, allowed)

    # -- Selection -----------------------------------------------------------

    def select(self, criterion: SelectionCriterion) -> SelectionResult:
        """Resolve ``criterion`` and present the resolved set for confirmation.

        A :class:`Resolved` result moves to ``RESOLVED`` (1.9). Empty, failed,
        or rejected selections are returned to the caller and leave the
        manager in ``IDLE``, so detection cannot proceed. Allowed again from
        ``RESOLVED`` (re-select before confirming) and ``ABORTED``.
        """
        self._require("select stacks", Phase.IDLE, Phase.RESOLVED, Phase.ABORTED)
        result = self._selector.resolve(criterion)
        self._selection_result = result
        if isinstance(result, Resolved):
            self._resolved = result
            self._phase = Phase.RESOLVED
        else:
            self._resolved = None
            self._phase = Phase.IDLE
        return result

    def confirm_selection(self) -> ConfirmedSet:
        """Confirm the resolved set; drift detection may now proceed (1.10)."""
        self._require("confirm the stack selection", Phase.RESOLVED)
        assert self._resolved is not None
        self._confirmed_set = self._selector.confirm(self._resolved)
        self._phase = Phase.CONFIRMED
        return self._confirmed_set

    def cancel_selection(self) -> Aborted:
        """Cancel the resolved set; the operation aborts with no detection (1.11)."""
        self._require("cancel the stack selection", Phase.RESOLVED)
        assert self._resolved is not None
        aborted = self._selector.cancel(self._resolved)
        self._resolved = None
        self._phase = Phase.ABORTED
        return aborted

    # -- Detection -----------------------------------------------------------

    def start_detection(self) -> BulkDetection:
        """Begin bulk detection over the confirmed set (2.1).

        No CloudFormation detection call is made until the first
        :meth:`advance_detection` round. An empty set completes immediately.
        """
        self._require("start drift detection", Phase.CONFIRMED)
        assert self._confirmed_set is not None
        self._phase = Phase.DETECTING
        self._detection = BulkDetection(
            self._cfn,
            self._clock,
            self._confirmed_set,
            progress=self._tracker,
            max_concurrency=self._max_concurrency,
            poll_interval=self._poll_interval,
            timeout=self._timeout,
            on_complete=self._on_detection_complete,
            retry_policy=self._retry_policy,
        )
        self._latest_operation = self._detection.operation
        return self._detection

    def advance_detection(self) -> bool:
        """Run one scheduling round of the in-flight detection.

        Returns ``True`` once bulk detection has signalled completion (2.7).
        """
        self._require("advance drift detection", Phase.DETECTING, Phase.DETECTED)
        assert self._detection is not None
        return self._detection.tick()

    def detect(self) -> DetectionResults:
        """Drive bulk detection to completion and return its results.

        Starts detection if it has not started yet (requires ``CONFIRMED``).
        """
        if self._phase is Phase.CONFIRMED:
            self.start_detection()
        self._require("run drift detection", Phase.DETECTING, Phase.DETECTED)
        assert self._detection is not None
        self._detection.run()
        assert self._detection_results is not None
        return self._detection_results

    def _on_detection_complete(self, results: DetectionResults) -> None:
        """Completion signal from BulkDetection: record results and summary (6.1)."""
        assert self._confirmed_set is not None
        self._detection_results = results
        self._detection_summary = summarize_detection(
            results, self._confirmed_set.unmatchedNames
        )
        self._phase = Phase.DETECTED

    # -- Report --------------------------------------------------------------

    def report(self) -> DriftReport:
        """Produce (or return) the consolidated drift report (3.1)."""
        if self._phase in _REPORT_AVAILABLE:
            assert self._report is not None
            return self._report
        self._require("produce the drift report", Phase.DETECTED, *_REPORT_AVAILABLE)
        assert self._detection_results is not None
        self._report = build_report(self._detection_results)
        self._phase = Phase.REPORTED
        return self._report

    def report_machine_readable(self) -> dict[str, Any]:
        """The consolidated report as structured data (3.7)."""
        return to_machine_readable(self.report())

    def report_json(self, *, indent: Optional[int] = None) -> str:
        """The consolidated report as a JSON string (3.7)."""
        return to_json(self.report(), indent=indent)

    def drifted_resources(self) -> tuple[ScopedResource, ...]:
        """Drifted resources from the report, selectable for reconciliation."""
        self._require("list drifted resources", *_REPORT_AVAILABLE)
        assert self._report is not None
        return drifted_scoped_resources(self._report)

    # -- Reconciliation scope ------------------------------------------------

    def select_scope(self, resources: Iterable[ScopedResource]) -> Scope:
        """Record the selected drifted resources as the reconciliation scope (4.1).

        Every resource must be a drifted resource from the consolidated report.
        An empty selection does not replace a previously recorded scope; its
        confirmation is rejected (4.2). Selecting again after confirmation
        invalidates that confirmation.
        """
        self._require(
            "select the reconciliation scope",
            Phase.REPORTED,
            Phase.SCOPE_RECORDED,
            Phase.SCOPE_CONFIRMED,
        )
        selection = tuple(resources)
        self._check_reportable(selection)
        scope = self._reconciler.set_scope(selection)
        self._pending_scope = scope
        self._current_selection = selection
        if len(scope) > 0:
            self._confirmed_scope = None
            self._phase = Phase.SCOPE_RECORDED
        elif self._phase is Phase.SCOPE_CONFIRMED:
            # An empty re-selection keeps the reconciler's recorded scope, but
            # the operator must confirm again before reconciling.
            self._confirmed_scope = None
            self._phase = Phase.SCOPE_RECORDED
        return scope

    def change_selection(self, resources: Iterable[ScopedResource]) -> None:
        """Change the current selection without recording a new scope.

        The next :meth:`confirm_scope` then detects the change and invalidates
        the recorded scope, requiring re-confirmation (4.3).
        """
        self._require(
            "change the reconciliation selection",
            Phase.REPORTED,
            Phase.SCOPE_RECORDED,
        )
        if self._pending_scope is None:
            raise PhaseError(
                "change the reconciliation selection before a scope is selected",
                self._phase,
                (Phase.SCOPE_RECORDED,),
            )
        selection = tuple(resources)
        self._check_reportable(selection)
        self._current_selection = selection

    def confirm_scope(self) -> ConfirmationResult:
        """Confirm the recorded scope against the current selection.

        ``ConfirmedScope`` moves to ``SCOPE_CONFIRMED`` (4.4, 4.10).
        ``ScopeInvalidated`` records the updated selection as the scope to
        re-confirm (4.3); ``RejectedEmpty`` leaves the scope unchanged (4.2).
        Both keep the manager out of ``SCOPE_CONFIRMED``.
        """
        self._require(
            "confirm the reconciliation scope", Phase.REPORTED, Phase.SCOPE_RECORDED
        )
        if self._pending_scope is None:
            raise PhaseError(
                "confirm the reconciliation scope before a scope is selected",
                self._phase,
                (Phase.SCOPE_RECORDED,),
            )
        result = self._reconciler.confirm_scope(
            self._pending_scope, self._current_selection
        )
        if isinstance(result, ConfirmedScope):
            self._confirmed_scope = result
            self._phase = Phase.SCOPE_CONFIRMED
        elif isinstance(result, ScopeInvalidated):
            self._pending_scope = result.updated
            self._phase = Phase.SCOPE_RECORDED
        return result

    def _check_reportable(self, selection: tuple[ScopedResource, ...]) -> None:
        assert self._report is not None
        allowed: set[ResourceKey] = {
            r.key for r in drifted_scoped_resources(self._report)
        }
        for item in selection:
            if not isinstance(item, ScopedResource):
                raise TypeError(
                    "scope entries must be ScopedResource instances; "
                    f"got {type(item).__name__}"
                )
            if item.key not in allowed:
                raise ValueError(
                    f"{item.resourceId} is not a drifted resource in the report"
                )

    # -- Reconciliation ------------------------------------------------------

    def reconcile(self, action: ReconciliationAction) -> ReconciliationResults:
        """Apply ``action`` to every resource in the confirmed scope (4.4).

        Produces the reconciliation Operation Summary (4.9, 6.2) and moves to
        ``RECONCILED``.
        """
        self._require("reconcile", Phase.SCOPE_CONFIRMED)
        assert self._confirmed_scope is not None
        action = ReconciliationAction(action)
        results = self._reconciler.reconcile(self._confirmed_scope, action)
        self._latest_operation = self._reconciler.operation
        self._reconciliation_results = results
        self._reconciliation_summary = summarize_reconciliation(results)
        self._phase = Phase.RECONCILED
        return results
