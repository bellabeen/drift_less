"""Reconciler: translates reconciliation intent into template operations.

Task 8.1 implements scope recording, invalidation, and confirmation gating:

- :meth:`Reconciler.set_scope` records 1-500 selected drifted resources as the
  reconciliation scope (requirement 4.1). Resources come from many stacks, so
  each one is wrapped in a :class:`ScopedResource` whose identity
  (:class:`ResourceKey`) includes the owning stack.
- :meth:`Reconciler.confirm_scope` compares the recorded scope against the
  operator's *current* selection and returns exactly one of:

  * :class:`RejectedEmpty` - the scope (or current selection) holds zero
    resources; the confirmation is rejected and the previously recorded scope
    is retained unchanged (4.2).
  * :class:`ScopeInvalidated` - the selection changed after the scope was
    recorded (or the scope is no longer the current one); the old scope is
    invalidated, the updated selection is recorded as a new, *unconfirmed*
    scope, and the operator must re-confirm it (4.3).
  * :class:`ConfirmedScope` - the gate token proving explicit confirmation
    (4.4, 4.10).

- A :class:`ConfirmedScope` can only be produced by ``confirm_scope`` (direct
  construction raises). ``reconcile`` (task 8.2) must pass its input through
  :meth:`Reconciler.require_confirmed`, which also rejects confirmed scopes
  made stale by a later ``set_scope`` / invalidation, so no reconciliation
  action can be applied before confirmation of the current scope.

Task 8.2 adds :meth:`Reconciler.reconcile` on top of this gate:

- ``AlignToTemplate`` re-asserts the stack's current template via
  ``update_stack(UsePreviousTemplate=True)`` (4.5).
- ``AdoptActual`` imports the actual configuration via
  ``create_change_set(ChangeSetType="IMPORT")`` followed by
  ``execute_change_set`` (4.6).
- The action is applied exactly once per scoped resource. On success the
  resource's drift status becomes ``IN_SYNC``; on failure the outcome records
  ``Failed(reason)``, the prior drift status is left unchanged, the failure is
  surfaced to the Progress_Tracker, and the remaining resources are still
  processed (4.7, 4.8). The returned :class:`ReconciliationResults` carries
  total / succeeded / failed counts (4.9).
- Every CloudFormation call is wrapped in
  :func:`drift_manager.retry.call_with_retry`; retry exhaustion records
  ``Failed(reason)``.
"""

from __future__ import annotations

import dataclasses
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Union

from .models import (
    Failed,
    OperationKind,
    ReconciliationAction,
    ReconciliationOutcome,
    ReconciliationResults,
    ResourceDrift,
    ResourceDriftStatus,
    Succeeded,
)
from .progress import Operation, ProgressTracker
from .retry import DEFAULT_RETRY_POLICY, RetryPolicy, call_with_retry

#: ``ChangeSetType`` used for adopt-actual (resource import).
IMPORT_CHANGE_SET_TYPE = "IMPORT"

#: Prefix for generated import change-set names (must match ``[a-zA-Z][-a-zA-Z0-9]*``).
CHANGE_SET_NAME_PREFIX = "drift-manager-import"

#: Maximum number of drifted resources a reconciliation scope may contain.
MAX_SCOPE_RESOURCES = 500

#: Message presented when an empty scope is confirmed (4.2).
EMPTY_SCOPE_MESSAGE = "at least one Drifted Resource must be selected"

#: Process-wide scope version source, so versions never collide across
#: Reconciler instances (a scope from another Reconciler is never "current").
_SCOPE_VERSIONS = itertools.count(1)


# ---------------------------------------------------------------------------
# Resource identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class ResourceKey:
    """Stable identity of a drifted resource across stacks.

    Logical resource ids are only unique within a stack, so the owning stack
    name is part of the identity.
    """

    stackName: str
    logicalResourceId: str

    def __str__(self) -> str:
        return f"{self.stackName}/{self.logicalResourceId}"


@dataclass(frozen=True)
class ScopedResource:
    """A drifted resource together with the stack that owns it."""

    stackName: str
    resource: ResourceDrift

    @property
    def key(self) -> ResourceKey:
        return ResourceKey(self.stackName, self.resource.logicalResourceId)

    @property
    def resourceId(self) -> str:
        """String identifier used in outcomes and summaries (``stack/logicalId``)."""
        return str(self.key)


def _normalize(resources: Iterable[ScopedResource]) -> tuple[ScopedResource, ...]:
    """Validate and de-duplicate ``resources`` by identity, preserving order."""
    seen: set[ResourceKey] = set()
    unique: list[ScopedResource] = []
    for item in resources:
        if not isinstance(item, ScopedResource):
            raise TypeError(
                "scope entries must be ScopedResource instances; "
                f"got {type(item).__name__}"
            )
        if item.key in seen:
            continue
        seen.add(item.key)
        unique.append(item)
    return tuple(unique)


def _keys(resources: Iterable[ScopedResource]) -> frozenset[ResourceKey]:
    return frozenset(r.key for r in resources)


# ---------------------------------------------------------------------------
# Scope and confirmation results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """A recorded (unconfirmed) reconciliation scope.

    ``version`` identifies the recording within its :class:`Reconciler`; a
    scope whose version is no longer current has been superseded or
    invalidated and cannot be confirmed.
    """

    resources: tuple[ScopedResource, ...]
    version: int = field(default=0, compare=False)

    @property
    def keys(self) -> frozenset[ResourceKey]:
        return _keys(self.resources)

    def __len__(self) -> int:
        return len(self.resources)


#: Module-private capability proving a ``ConfirmedScope`` came from ``confirm_scope``.
_CONFIRM_TOKEN = object()


@dataclass(frozen=True)
class ConfirmedScope:
    """An explicitly confirmed reconciliation scope (the reconciliation gate token).

    Only :meth:`Reconciler.confirm_scope` can produce one; direct construction
    raises :class:`ValueError`.
    """

    resources: tuple[ScopedResource, ...]
    version: int = field(default=0, compare=False)
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _CONFIRM_TOKEN:
            raise ValueError(
                "ConfirmedScope can only be produced by Reconciler.confirm_scope()"
            )

    @property
    def keys(self) -> frozenset[ResourceKey]:
        return _keys(self.resources)

    def __len__(self) -> int:
        return len(self.resources)


@dataclass(frozen=True)
class ScopeInvalidated:
    """The selection changed before confirmation; re-confirmation is required.

    ``updated`` is the newly recorded (unconfirmed) scope built from the
    current selection; confirm it to proceed.
    """

    previous: Scope
    updated: Scope
    message: str = "selection changed; re-confirm the updated reconciliation scope"


@dataclass(frozen=True)
class RejectedEmpty:
    """Confirmation of a zero-resource scope was rejected.

    ``retained`` is the recorded scope, left unchanged (``None`` if no scope
    has been recorded yet).
    """

    retained: Optional[Scope]
    message: str = EMPTY_SCOPE_MESSAGE


# The result of :meth:`Reconciler.confirm_scope`.
ConfirmationResult = Union[ConfirmedScope, ScopeInvalidated, RejectedEmpty]


def require_confirmed_scope(candidate: object) -> ConfirmedScope:
    """Return ``candidate`` only if it is a :class:`ConfirmedScope`.

    Raises :class:`TypeError` for an unconfirmed :class:`Scope`, a
    :class:`ScopeInvalidated`, a :class:`RejectedEmpty`, or anything else.
    """
    if not isinstance(candidate, ConfirmedScope):
        raise TypeError(
            "reconciliation requires a ConfirmedScope from Reconciler.confirm_scope(); "
            f"got {type(candidate).__name__}"
        )
    return candidate


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class Reconciler:
    """Records, invalidates, and confirms reconciliation scopes.

    ``cfn`` and ``clock`` are used by ``reconcile`` (task 8.2); scope handling
    does not touch CloudFormation.
    """

    def __init__(
        self,
        cfn: Any = None,
        clock: Any = None,
        *,
        progress: Optional[ProgressTracker] = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        self._cfn = cfn
        self._clock = clock if clock is not None else _SystemClock()
        self._progress = progress
        self._retry_policy = retry_policy
        self._scope: Optional[Scope] = None
        self._confirmed_version: Optional[int] = None
        #: Current drift record of every resource this reconciler has processed.
        self._resources: dict[ResourceKey, ResourceDrift] = {}
        self._operation: Optional[Operation] = None
        self._change_set_counter = itertools.count(1)

    # -- State ---------------------------------------------------------------

    @property
    def scope(self) -> Optional[Scope]:
        """The currently recorded scope, or ``None`` if none has been recorded."""
        return self._scope

    # -- Scope recording -----------------------------------------------------

    def set_scope(self, resources: Iterable[ScopedResource]) -> Scope:
        """Record the selected drifted resources as the reconciliation scope (4.1).

        Duplicates (same stack and logical id) are collapsed. More than 500
        resources raises :class:`ValueError`. An empty selection returns an
        empty :class:`Scope` *without* replacing the recorded scope, so a later
        confirmation of it is rejected and the prior scope retained (4.2).
        Recording a non-empty scope invalidates any earlier confirmation.
        """
        normalized = _normalize(resources)
        if len(normalized) > MAX_SCOPE_RESOURCES:
            raise ValueError(
                f"a reconciliation scope may contain at most {MAX_SCOPE_RESOURCES} "
                f"resources; got {len(normalized)}"
            )
        if not normalized:
            return Scope(resources=(), version=0)
        return self._record(normalized)

    def _record(self, resources: tuple[ScopedResource, ...]) -> Scope:
        scope = Scope(resources=resources, version=next(_SCOPE_VERSIONS))
        self._scope = scope
        self._confirmed_version = None
        return scope

    # -- Confirmation --------------------------------------------------------

    def confirm_scope(
        self, scope: Scope, current_selection: Iterable[ScopedResource]
    ) -> ConfirmationResult:
        """Confirm ``scope`` against the operator's current selection.

        - Empty scope or empty current selection -> :class:`RejectedEmpty`;
          the recorded scope is unchanged (4.2).
        - Current selection differs from ``scope`` (by resource identity), or
          ``scope`` is not the currently recorded scope -> :class:`ScopeInvalidated`;
          the current selection is recorded as a new unconfirmed scope (4.3).
        - Otherwise -> :class:`ConfirmedScope` (4.4, 4.10).
        """
        if not isinstance(scope, Scope):
            raise TypeError(
                f"only a recorded Scope can be confirmed; got {type(scope).__name__}"
            )
        selection = _normalize(current_selection)

        if len(scope) == 0 or len(selection) == 0:
            return RejectedEmpty(retained=self._scope)

        if len(selection) > MAX_SCOPE_RESOURCES:
            raise ValueError(
                f"a reconciliation scope may contain at most {MAX_SCOPE_RESOURCES} "
                f"resources; got {len(selection)}"
            )

        is_current = self._scope is not None and scope.version == self._scope.version
        if not is_current or _keys(selection) != scope.keys:
            updated = self._record(selection)
            return ScopeInvalidated(previous=scope, updated=updated)

        self._confirmed_version = scope.version
        return ConfirmedScope(
            resources=scope.resources, version=scope.version, _token=_CONFIRM_TOKEN
        )

    # -- Gate ----------------------------------------------------------------

    def require_confirmed(self, candidate: object) -> ConfirmedScope:
        """Gate for ``reconcile``: accept only the current confirmed scope (4.10).

        Raises :class:`TypeError` if ``candidate`` is not a
        :class:`ConfirmedScope`, and :class:`ValueError` if it was superseded by
        a later ``set_scope`` or invalidation.
        """
        confirmed = require_confirmed_scope(candidate)
        if (
            self._confirmed_version is None
            or confirmed.version != self._confirmed_version
        ):
            raise ValueError(
                "confirmed scope is stale; the scope changed and must be re-confirmed"
            )
        return confirmed

    # -- Reconciliation state --------------------------------------------------

    @property
    def operation(self) -> Optional[Operation]:
        """Progress_Tracker operation of the most recent ``reconcile`` call."""
        return self._operation

    @property
    def resources(self) -> Mapping[ResourceKey, ResourceDrift]:
        """Current drift record of every resource processed by ``reconcile``.

        Succeeded resources are ``IN_SYNC``; failed ones keep their prior
        drift record unchanged (4.7).
        """
        return dict(self._resources)

    def resource_status(self, key: ResourceKey) -> Optional[ResourceDriftStatus]:
        """Current drift status of ``key``, or ``None`` if never reconciled."""
        drift = self._resources.get(key)
        return drift.resourceDriftStatus if drift is not None else None

    # -- Reconciliation --------------------------------------------------------

    def reconcile(
        self,
        scope: ConfirmedScope,
        action: ReconciliationAction,
        *,
        progress: Optional[ProgressTracker] = None,
    ) -> ReconciliationResults:
        """Apply ``action`` exactly once to every resource in ``scope``.

        ``scope`` must be the current :class:`ConfirmedScope` (4.4, 4.10).
        Failures are isolated: each records ``Failed(reason)``, leaves the
        resource's prior drift status, is reported to the Progress_Tracker,
        and processing continues (4.7, 4.8). Returns per-resource outcomes and
        total / succeeded / failed counts (4.9).
        """
        confirmed = self.require_confirmed(scope)
        action = ReconciliationAction(action)
        if self._cfn is None:
            raise ValueError("reconcile requires a CloudFormation client")

        tracker = progress if progress is not None else self._progress
        operation = (
            tracker.start(
                OperationKind.RECONCILIATION,
                tuple(item.resourceId for item in confirmed.resources),
            )
            if tracker is not None
            else None
        )
        self._operation = operation

        outcomes: list[ReconciliationOutcome] = []
        succeeded = failed = 0
        for item in confirmed.resources:
            # Record the prior status first so a failure leaves it observable.
            self._resources.setdefault(item.key, item.resource)
            try:
                self._apply(item, action)
            except Exception as exc:  # noqa: BLE001 - isolate per-resource failure
                reason = _failure_reason(action, exc)
                failed += 1
                outcomes.append(
                    ReconciliationOutcome(item.resourceId, action, Failed(reason))
                )
                if operation is not None:
                    operation.record_failure(item.resourceId, reason)
                continue

            self._resources[item.key] = dataclasses.replace(
                self._resources[item.key],
                resourceDriftStatus=ResourceDriftStatus.IN_SYNC,
                propertyDifferences=(),
            )
            succeeded += 1
            outcomes.append(ReconciliationOutcome(item.resourceId, action, Succeeded()))
            if operation is not None:
                operation.record_success(item.resourceId)

        return ReconciliationResults(
            outcomes=tuple(outcomes),
            total=len(outcomes),
            succeeded=succeeded,
            failed=failed,
        )

    def _call(self, fn: Any) -> Any:
        return call_with_retry(fn, clock=self._clock, policy=self._retry_policy)

    def _apply(self, item: ScopedResource, action: ReconciliationAction) -> None:
        """Translate ``action`` into CloudFormation calls for one resource."""
        cfn = self._cfn
        stack = item.stackName
        if action is ReconciliationAction.ALIGN_TO_TEMPLATE:
            # Re-assert the stack's current template (4.5).
            self._call(
                lambda: cfn.update_stack(StackName=stack, UsePreviousTemplate=True)
            )
            return

        # AdoptActual: import the actual configuration (4.6).
        drift = item.resource
        name = f"{CHANGE_SET_NAME_PREFIX}-{next(self._change_set_counter)}"
        identifier = {
            "PhysicalResourceId": drift.physicalResourceId or drift.logicalResourceId
        }
        created = self._call(
            lambda: cfn.create_change_set(
                StackName=stack,
                ChangeSetName=name,
                ChangeSetType=IMPORT_CHANGE_SET_TYPE,
                UsePreviousTemplate=True,
                ResourcesToImport=[
                    {
                        "ResourceType": drift.resourceType,
                        "LogicalResourceId": drift.logicalResourceId,
                        "ResourceIdentifier": identifier,
                    }
                ],
            )
        )
        change_set = (created or {}).get("Id") or name
        self._call(
            lambda: cfn.execute_change_set(ChangeSetName=change_set, StackName=stack)
        )


def _failure_reason(action: ReconciliationAction, exc: BaseException) -> str:
    message = str(exc) or type(exc).__name__
    return f"{action.value} failed: {message}"


class _SystemClock:
    """Real clock used when none is injected (retry backoff sleeps)."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
