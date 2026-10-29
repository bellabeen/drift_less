"""Shared immutable data models for the Multi-Stack Drift Manager.

Implements the Data Models section of the design document (task 1.2):
selection criteria, drift status, resource/property drift records, operation
summaries, and the detection / report / reconciliation result containers.

All structures are immutable (frozen dataclasses / enums) so that the artifacts
produced by each pipeline stage are reproducible and safe to share.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Union


# ---------------------------------------------------------------------------
# Selection criteria
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TagFilter:
    """A single tag filter: key and value matched exactly, case-sensitively."""

    key: str
    value: str


@dataclass(frozen=True)
class Names:
    """Select stacks by explicit names (exact match)."""

    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tags:
    """Select stacks by tag filters with AND, exact, case-sensitive semantics."""

    filters: tuple[TagFilter, ...] = ()


@dataclass(frozen=True)
class Region:
    """Select all stacks in a given region."""

    region: str


# A SelectionCriterion is one of the three variants above.
SelectionCriterion = Union[Names, Tags, Region]


# ---------------------------------------------------------------------------
# Drift status enums
# ---------------------------------------------------------------------------


class DriftStatus(str, Enum):
    """Stack-level drift status reported by CloudFormation."""

    DRIFTED = "DRIFTED"
    IN_SYNC = "IN_SYNC"
    NOT_CHECKED = "NOT_CHECKED"
    UNKNOWN = "UNKNOWN"


class ResourceDriftStatus(str, Enum):
    """Per-resource drift status reported by CloudFormation."""

    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    IN_SYNC = "IN_SYNC"
    NOT_CHECKED = "NOT_CHECKED"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


class DifferenceType(str, Enum):
    """Type of a property-level difference."""

    ADD = "ADD"
    REMOVE = "REMOVE"
    NOT_EQUAL = "NOT_EQUAL"


# ---------------------------------------------------------------------------
# ABSENT sentinel
# ---------------------------------------------------------------------------


class _Absent:
    """Explicit sentinel marking the absence of a property value.

    Used by PropertyDifference: the expected value is ABSENT for an ADD-only
    difference (value exists only in actual) and the actual value is ABSENT for
    a REMOVE-only difference (value exists only in expected).

    A single immutable instance (``ABSENT``) is exposed; it is distinct from
    ``None`` so that a genuine ``None`` property value is not confused with
    absence.
    """

    _instance: Optional["_Absent"] = None

    def __new__(cls) -> "_Absent":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "ABSENT"

    def __bool__(self) -> bool:
        return False


ABSENT = _Absent()

# A property value is either any concrete value or the ABSENT sentinel.
Value = Any
MaybeAbsent = Union[Value, _Absent]


# ---------------------------------------------------------------------------
# Property / resource drift
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PropertyDifference:
    """A single property-level difference for a drifted resource.

    ``expectedValue`` is ABSENT exactly when ``differenceType`` is ADD (the
    value exists only in the actual configuration); ``actualValue`` is ABSENT
    exactly when ``differenceType`` is REMOVE (the value exists only in the
    expected template configuration).
    """

    propertyPath: str
    expectedValue: MaybeAbsent
    actualValue: MaybeAbsent
    differenceType: DifferenceType


@dataclass(frozen=True)
class ResourceDrift:
    """Drift record for a single stack resource."""

    logicalResourceId: str
    resourceType: str
    resourceDriftStatus: ResourceDriftStatus
    propertyDifferences: tuple[PropertyDifference, ...] = ()
    physicalResourceId: Optional[str] = None


# ---------------------------------------------------------------------------
# Operation summary
# ---------------------------------------------------------------------------


class OperationKind(str, Enum):
    """Which bulk operation an OperationSummary describes."""

    DETECTION = "Detection"
    RECONCILIATION = "Reconciliation"


class ItemOutcome(str, Enum):
    """Outcome of a single item in an operation summary."""

    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    SKIPPED = "Skipped"


@dataclass(frozen=True)
class SummaryItem:
    """A single item in an OperationSummary.

    ``reason`` is present for FAILED and SKIPPED outcomes.

    ``driftStatus`` carries the final stack-level drift status for detection
    summary items (Requirement 6.1); it is ``None`` for reconciliation items.
    ``action`` carries the applied reconciliation action for reconciliation
    summary items; it is ``None`` for detection items.
    """

    id: str
    outcome: ItemOutcome
    reason: Optional[str] = None
    driftStatus: Optional["DriftStatus"] = None
    action: Optional["ReconciliationAction"] = None


@dataclass(frozen=True)
class OperationSummary:
    """Consolidated end-of-run summary for a bulk operation.

    Invariant: succeeded + failed + skipped == len(items).

    ``unmatchedNames`` records explicitly submitted stack names that matched no
    stack (Requirement 1.5). They are not items (no operation ran for them), so
    they do not participate in the count invariant.
    """

    kind: OperationKind
    items: tuple[SummaryItem, ...] = ()
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    unmatchedNames: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        """Total number of items listed in the summary (Requirement 4.9)."""
        return len(self.items)


# ---------------------------------------------------------------------------
# Detection result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Detected:
    """A stack whose detection completed with a reported drift status."""

    status: DriftStatus
    driftedResources: tuple[ResourceDrift, ...] = ()


@dataclass(frozen=True)
class Unknown:
    """A stack whose detection failed or did not complete (terminal UNKNOWN)."""

    reason: str


@dataclass(frozen=True)
class Skipped:
    """A stack whose state disallowed detection (terminal, never initiated)."""

    reason: str


# The outcome of detection for a single stack.
DetectionOutcome = Union[Detected, Unknown, Skipped]


@dataclass(frozen=True)
class StackDetectionOutcome:
    """Detection outcome for a single stack within the resolved set."""

    stackName: str
    outcome: DetectionOutcome
    stackId: Optional[str] = None


@dataclass(frozen=True)
class DetectionResults:
    """Bulk detection results: exactly one outcome per stack in the set."""

    outcomes: tuple[StackDetectionOutcome, ...] = ()


# ---------------------------------------------------------------------------
# Report containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusCounts:
    """Per-status stack counts; the four counts sum to the number of stacks."""

    DRIFTED: int = 0
    IN_SYNC: int = 0
    NOT_CHECKED: int = 0
    UNKNOWN: int = 0


@dataclass(frozen=True)
class StackReportEntry:
    """One consolidated report entry per stack.

    ``driftedResources`` is non-empty only when ``status`` is DRIFTED.
    """

    stackName: str
    status: DriftStatus
    driftedResources: tuple[ResourceDrift, ...] = ()


@dataclass(frozen=True)
class DriftReport:
    """Consolidated drift report over the resolved set.

    ``emptyMessage`` is present only when ``entries`` is empty.
    """

    entries: tuple[StackReportEntry, ...] = ()
    counts: StatusCounts = field(default_factory=StatusCounts)
    emptyMessage: Optional[str] = None


# ---------------------------------------------------------------------------
# Reconciliation result containers
# ---------------------------------------------------------------------------


class ReconciliationAction(str, Enum):
    """Reconciliation intent chosen for a scoped resource."""

    ALIGN_TO_TEMPLATE = "AlignToTemplate"
    ADOPT_ACTUAL = "AdoptActual"


@dataclass(frozen=True)
class Succeeded:
    """A resource whose reconciliation succeeded (now in-sync)."""


@dataclass(frozen=True)
class Failed:
    """A resource whose reconciliation failed; prior drift status retained."""

    reason: str


@dataclass(frozen=True)
class ReconciliationSkipped:
    """A resource skipped during reconciliation, with a reason."""

    reason: str


# The result of reconciling a single resource.
ReconciliationResult = Union[Succeeded, Failed, ReconciliationSkipped]


@dataclass(frozen=True)
class ReconciliationOutcome:
    """Reconciliation outcome for a single scoped resource."""

    resourceId: str
    action: ReconciliationAction
    result: ReconciliationResult


@dataclass(frozen=True)
class ReconciliationResults:
    """Bulk reconciliation results with aggregate counts.

    Invariant: succeeded + failed <= total; total == len(outcomes).
    """

    outcomes: tuple[ReconciliationOutcome, ...] = ()
    total: int = 0
    succeeded: int = 0
    failed: int = 0


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    """A per-item failure recorded during an in-flight operation."""

    id: str
    reason: str


@dataclass(frozen=True)
class ProgressSnapshot:
    """A point-in-time view of an in-flight bulk operation.

    Invariant: completed + remaining == total.
    """

    total: int = 0
    completed: int = 0
    remaining: int = 0
    failures: tuple[Failure, ...] = ()
