"""Property test for operation-summary conservation (task 7.9).

Feature: multi-stack-drift-manager, Property 22: Operation summaries conserve the item total

For any operation summary (detection or reconciliation), the succeeded, failed,
and skipped counts each equal the number of items with that outcome and
together sum to the total number of items listed, and reconciliation summaries
list every targeted resource with an outcome of succeeded, failed, or skipped.
The Progress_Tracker's completion summary obeys the same conservation.

**Validates: Requirements 4.9, 5.5, 6.2, 6.3**
"""

from __future__ import annotations

from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import (
    Detected,
    DetectionResults,
    DriftStatus,
    Failed,
    ItemOutcome,
    OperationKind,
    ReconciliationAction,
    ReconciliationOutcome,
    ReconciliationResults,
    ReconciliationSkipped,
    Skipped,
    StackDetectionOutcome,
    Succeeded,
    Unknown,
)
from drift_manager.progress import ProgressTracker
from drift_manager.summary import Produced, summarize_detection, summarize_reconciliation
from tests.fakes import FakeClock

# ---------------------------------------------------------------------------
# Local strategies
# ---------------------------------------------------------------------------

_ids = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_"),
    min_size=1,
    max_size=12,
)
# Reasons may be blank: the summary must still count those items correctly.
_reasons = st.text(max_size=20)

_detection_outcomes = st.one_of(
    st.builds(Detected, status=st.sampled_from([DriftStatus.DRIFTED, DriftStatus.IN_SYNC])),
    st.builds(Unknown, reason=_reasons),
    st.builds(Skipped, reason=_reasons),
)

_reconciliation_results = st.one_of(
    st.just(Succeeded()),
    st.builds(Failed, reason=_reasons),
    st.builds(ReconciliationSkipped, reason=_reasons),
)


@st.composite
def detection_results(draw) -> DetectionResults:
    names = draw(st.lists(_ids, max_size=25, unique=True))
    return DetectionResults(
        outcomes=tuple(
            StackDetectionOutcome(stackName=n, outcome=draw(_detection_outcomes)) for n in names
        )
    )


@st.composite
def reconciliation_results(draw) -> ReconciliationResults:
    ids = draw(st.lists(_ids, max_size=25, unique=True))
    outcomes = tuple(
        ReconciliationOutcome(
            resourceId=r,
            action=draw(st.sampled_from(list(ReconciliationAction))),
            result=draw(_reconciliation_results),
        )
        for r in ids
    )
    succeeded = sum(isinstance(o.result, Succeeded) for o in outcomes)
    failed = sum(isinstance(o.result, Failed) for o in outcomes)
    return ReconciliationResults(
        outcomes=outcomes, total=len(outcomes), succeeded=succeeded, failed=failed
    )


# ---------------------------------------------------------------------------
# Independent oracles
# ---------------------------------------------------------------------------


def _expected_detection_outcome(outcome) -> ItemOutcome:
    if isinstance(outcome, Detected):
        return ItemOutcome.SUCCEEDED
    if isinstance(outcome, Unknown):
        return ItemOutcome.FAILED
    return ItemOutcome.SKIPPED


def _expected_reconciliation_outcome(result) -> ItemOutcome:
    if isinstance(result, Succeeded):
        return ItemOutcome.SUCCEEDED
    if isinstance(result, Failed):
        return ItemOutcome.FAILED
    return ItemOutcome.SKIPPED


def _assert_conserved(summary, expected: Counter, n_items: int) -> None:
    listed = Counter(item.outcome for item in summary.items)
    assert summary.succeeded == expected[ItemOutcome.SUCCEEDED] == listed[ItemOutcome.SUCCEEDED]
    assert summary.failed == expected[ItemOutcome.FAILED] == listed[ItemOutcome.FAILED]
    assert summary.skipped == expected[ItemOutcome.SKIPPED] == listed[ItemOutcome.SKIPPED]
    assert summary.succeeded + summary.failed + summary.skipped == summary.total
    assert summary.total == len(summary.items) == n_items
    assert all(item.outcome in set(ItemOutcome) for item in summary.items)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


# Feature: multi-stack-drift-manager, Property 22: Operation summaries conserve the item total
@settings(max_examples=100)
@given(results=detection_results())
def test_detection_summary_conserves_item_total(results: DetectionResults) -> None:
    produced = summarize_detection(results)
    assert isinstance(produced, Produced)
    summary = produced.summary
    assert summary.kind is OperationKind.DETECTION

    expected = Counter(_expected_detection_outcome(o.outcome) for o in results.outcomes)
    _assert_conserved(summary, expected, len(results.outcomes))

    # Every stack is listed once, in order, with its expected outcome.
    assert [(i.id, i.outcome) for i in summary.items] == [
        (o.stackName, _expected_detection_outcome(o.outcome)) for o in results.outcomes
    ]


# Feature: multi-stack-drift-manager, Property 22: Operation summaries conserve the item total
@settings(max_examples=100)
@given(results=reconciliation_results())
def test_reconciliation_summary_conserves_item_total(results: ReconciliationResults) -> None:
    produced = summarize_reconciliation(results)
    assert isinstance(produced, Produced)
    summary = produced.summary
    assert summary.kind is OperationKind.RECONCILIATION

    expected = Counter(_expected_reconciliation_outcome(o.result) for o in results.outcomes)
    _assert_conserved(summary, expected, len(results.outcomes))

    # Total is the count of targeted resources (Requirement 4.9).
    assert summary.total == results.total

    # Every targeted resource is listed with succeeded/failed/skipped (Requirement 6.2).
    assert [(i.id, i.outcome, i.action) for i in summary.items] == [
        (o.resourceId, _expected_reconciliation_outcome(o.result), o.action)
        for o in results.outcomes
    ]


@st.composite
def tracker_scenarios(draw):
    """Unique item ids plus a success/failure decision and recording order."""
    item_ids = draw(st.lists(_ids, max_size=25, unique=True))
    decisions = draw(st.lists(st.booleans(), min_size=len(item_ids), max_size=len(item_ids)))
    order = draw(st.permutations(range(len(item_ids))))
    kind = draw(st.sampled_from(list(OperationKind)))
    return kind, item_ids, decisions, order


# Feature: multi-stack-drift-manager, Property 22: Operation summaries conserve the item total
@settings(max_examples=100)
@given(scenario=tracker_scenarios(), reason=st.text(min_size=1, max_size=20))
def test_progress_tracker_summary_conserves_recorded_outcomes(scenario, reason: str) -> None:
    kind, item_ids, decisions, order = scenario
    tracker = ProgressTracker(FakeClock())
    operation = tracker.start(kind, tuple(item_ids))

    for idx in order:
        if decisions[idx]:
            operation.record_success(item_ids[idx])
        else:
            operation.record_failure(item_ids[idx], reason)

    summary = tracker.summary(operation)
    expected_succeeded = sum(decisions)
    expected_failed = len(decisions) - expected_succeeded

    assert tracker.is_complete(operation)
    assert summary.kind is kind
    assert summary.succeeded == expected_succeeded
    assert summary.failed == expected_failed
    assert summary.skipped == 0
    assert summary.succeeded + summary.failed == len(item_ids) == summary.total

    # Each item is listed with exactly the outcome that was recorded for it.
    assert [(i.id, i.outcome) for i in summary.items] == [
        (iid, ItemOutcome.SUCCEEDED if ok else ItemOutcome.FAILED)
        for iid, ok in zip(item_ids, decisions)
    ]
