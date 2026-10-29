"""Operation summaries for bulk detection and reconciliation (task 7.3).

Produces :class:`~drift_manager.models.OperationSummary` values for both bulk
operations (Requirements 4.9, 6.1-6.5).

Detection outcome → summary item mapping (Requirement 6.1):

* ``Detected(status)`` → ``SUCCEEDED``, ``driftStatus = status``
  (detection completed; the stack's final status is whatever CFN reported,
  including ``DRIFTED``).
* ``Unknown(reason)``  → ``FAILED``, ``driftStatus = UNKNOWN``, with reason
  (detection failed or timed out; Requirements 2.4, 2.6).
* ``Skipped(reason)``  → ``SKIPPED``, ``driftStatus = NOT_CHECKED``, with
  reason (state disallowed detection; Requirement 2.5).

The final drift status reuses :func:`drift_manager.reporter.outcome_status`,
so the summary and the consolidated report always agree.

Reconciliation result → summary item mapping (Requirement 6.2):

* ``Succeeded``                → ``SUCCEEDED``
* ``Failed(reason)``           → ``FAILED`` with reason
* ``ReconciliationSkipped(r)`` → ``SKIPPED`` with reason

Every item carries the applied ``action``. Failed/skipped items always carry a
non-empty reason (Requirement 6.4); a blank recorded reason is replaced by
:data:`MISSING_REASON`.

Summary production never raises (Requirement 6.5): the ``summarize_*``
functions return :data:`SummaryResult`, which is either :class:`Produced` or
:class:`SummaryError`. A ``SummaryError`` carries an error message and the
recorded per-item outcomes, untouched.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Union

from drift_manager.models import (
    Detected,
    DetectionResults,
    DriftStatus,
    Failed,
    ItemOutcome,
    OperationKind,
    OperationSummary,
    ReconciliationOutcome,
    ReconciliationResults,
    ReconciliationSkipped,
    Skipped,
    StackDetectionOutcome,
    Succeeded,
    SummaryItem,
    Unknown,
)
from drift_manager.reporter import outcome_status

#: Reason used when a failed/skipped item was recorded with a blank reason.
MISSING_REASON = "No reason was recorded."


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Produced:
    """The operation summary was produced successfully."""

    summary: OperationSummary


@dataclass(frozen=True)
class SummaryError:
    """Summary production failed (Requirement 6.5).

    ``message`` is the error indication to present; ``outcomes`` holds the
    recorded per-item outcomes exactly as they were passed in.
    """

    kind: OperationKind
    message: str
    outcomes: tuple[Union[StackDetectionOutcome, ReconciliationOutcome], ...] = ()
    unmatchedNames: tuple[str, ...] = ()


SummaryResult = Union[Produced, SummaryError]


# ---------------------------------------------------------------------------
# Item builders
# ---------------------------------------------------------------------------


def _reason(reason: object) -> str:
    text = reason if isinstance(reason, str) else ""
    return text if text.strip() else MISSING_REASON


def _detection_item(stack: StackDetectionOutcome) -> SummaryItem:
    outcome = stack.outcome
    status = outcome_status(outcome)  # raises TypeError on unsupported outcome
    if isinstance(outcome, Detected):
        return SummaryItem(id=stack.stackName, outcome=ItemOutcome.SUCCEEDED, driftStatus=status)
    if isinstance(outcome, Unknown):
        return SummaryItem(
            id=stack.stackName,
            outcome=ItemOutcome.FAILED,
            reason=_reason(outcome.reason),
            driftStatus=DriftStatus.UNKNOWN,
        )
    # outcome_status guarantees the only remaining variant is Skipped.
    assert isinstance(outcome, Skipped)
    return SummaryItem(
        id=stack.stackName,
        outcome=ItemOutcome.SKIPPED,
        reason=_reason(outcome.reason),
        driftStatus=DriftStatus.NOT_CHECKED,
    )


def _reconciliation_item(rec: ReconciliationOutcome) -> SummaryItem:
    result = rec.result
    if isinstance(result, Succeeded):
        return SummaryItem(id=rec.resourceId, outcome=ItemOutcome.SUCCEEDED, action=rec.action)
    if isinstance(result, Failed):
        return SummaryItem(
            id=rec.resourceId,
            outcome=ItemOutcome.FAILED,
            reason=_reason(result.reason),
            action=rec.action,
        )
    if isinstance(result, ReconciliationSkipped):
        return SummaryItem(
            id=rec.resourceId,
            outcome=ItemOutcome.SKIPPED,
            reason=_reason(result.reason),
            action=rec.action,
        )
    raise TypeError(f"Unsupported reconciliation result: {result!r}")


def _build_summary(
    kind: OperationKind,
    items: Iterable[SummaryItem],
    unmatched_names: tuple[str, ...] = (),
) -> OperationSummary:
    """Assemble a summary whose counts are derived from the items themselves."""
    items = tuple(items)
    counts = Counter(item.outcome for item in items)
    return OperationSummary(
        kind=kind,
        items=items,
        succeeded=counts.get(ItemOutcome.SUCCEEDED, 0),
        failed=counts.get(ItemOutcome.FAILED, 0),
        skipped=counts.get(ItemOutcome.SKIPPED, 0),
        unmatchedNames=tuple(unmatched_names),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def summarize_detection(
    results: DetectionResults, unmatched_names: Iterable[str] = ()
) -> SummaryResult:
    """Produce the detection Operation Summary (Requirements 1.5, 6.1, 6.3-6.5).

    One item per stack in ``results`` (input order), each with its final
    drift status. ``unmatched_names`` (from selection) are recorded on the
    summary but are not items.
    """
    unmatched = tuple(unmatched_names)
    try:
        items = [_detection_item(stack) for stack in results.outcomes]
        return Produced(_build_summary(OperationKind.DETECTION, items, unmatched))
    except Exception as exc:  # noqa: BLE001 - 6.5: never lose recorded outcomes
        return SummaryError(
            kind=OperationKind.DETECTION,
            message=f"Failed to produce the detection operation summary: {exc}",
            outcomes=tuple(results.outcomes),
            unmatchedNames=unmatched,
        )


def summarize_reconciliation(results: ReconciliationResults) -> SummaryResult:
    """Produce the reconciliation Operation Summary (Requirements 4.9, 6.2-6.5).

    One item per targeted resource (input order) with outcome succeeded,
    failed, or skipped; ``summary.total`` is the count of selected resources.
    """
    try:
        items = [_reconciliation_item(rec) for rec in results.outcomes]
        return Produced(_build_summary(OperationKind.RECONCILIATION, items))
    except Exception as exc:  # noqa: BLE001 - 6.5: never lose recorded outcomes
        return SummaryError(
            kind=OperationKind.RECONCILIATION,
            message=f"Failed to produce the reconciliation operation summary: {exc}",
            outcomes=tuple(results.outcomes),
        )
