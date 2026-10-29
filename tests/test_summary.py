"""Unit tests for operation summaries (task 7.3).

**Validates: Requirements 1.5, 4.9, 6.1, 6.2, 6.3, 6.4, 6.5**
"""

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
from drift_manager.summary import (
    MISSING_REASON,
    Produced,
    SummaryError,
    summarize_detection,
    summarize_reconciliation,
)

ALIGN = ReconciliationAction.ALIGN_TO_TEMPLATE
ADOPT = ReconciliationAction.ADOPT_ACTUAL


def _detection_results():
    return DetectionResults(
        outcomes=(
            StackDetectionOutcome("a", Detected(DriftStatus.DRIFTED)),
            StackDetectionOutcome("b", Detected(DriftStatus.IN_SYNC)),
            StackDetectionOutcome("c", Unknown("timed out after 300s")),
            StackDetectionOutcome("d", Skipped("UPDATE_IN_PROGRESS")),
        )
    )


class TestDetectionSummary:
    def test_lists_every_stack_with_final_status_and_outcome(self):
        result = summarize_detection(_detection_results())
        assert isinstance(result, Produced)
        s = result.summary
        assert s.kind is OperationKind.DETECTION
        assert [(i.id, i.outcome, i.driftStatus) for i in s.items] == [
            ("a", ItemOutcome.SUCCEEDED, DriftStatus.DRIFTED),
            ("b", ItemOutcome.SUCCEEDED, DriftStatus.IN_SYNC),
            ("c", ItemOutcome.FAILED, DriftStatus.UNKNOWN),
            ("d", ItemOutcome.SKIPPED, DriftStatus.NOT_CHECKED),
        ]
        assert (s.succeeded, s.failed, s.skipped) == (2, 1, 1)
        assert s.succeeded + s.failed + s.skipped == s.total == 4

    def test_failed_and_skipped_items_carry_reason(self):
        s = summarize_detection(_detection_results()).summary
        reasons = {i.id: i.reason for i in s.items}
        assert reasons == {
            "a": None,
            "b": None,
            "c": "timed out after 300s",
            "d": "UPDATE_IN_PROGRESS",
        }

    def test_blank_reason_is_replaced(self):
        results = DetectionResults((StackDetectionOutcome("x", Unknown("  ")),))
        item = summarize_detection(results).summary.items[0]
        assert item.reason == MISSING_REASON

    def test_unmatched_names_recorded_but_not_counted(self):
        s = summarize_detection(_detection_results(), unmatched_names=["ghost"]).summary
        assert s.unmatchedNames == ("ghost",)
        assert s.total == 4

    def test_empty_results(self):
        s = summarize_detection(DetectionResults()).summary
        assert s.items == () and (s.succeeded, s.failed, s.skipped) == (0, 0, 0)


class TestReconciliationSummary:
    def test_lists_every_resource_with_outcome_and_action(self):
        results = ReconciliationResults(
            outcomes=(
                ReconciliationOutcome("r1", ALIGN, Succeeded()),
                ReconciliationOutcome("r2", ADOPT, Failed("AccessDenied")),
                ReconciliationOutcome("r3", ALIGN, ReconciliationSkipped("no longer drifted")),
            ),
            total=3,
            succeeded=1,
            failed=1,
        )
        result = summarize_reconciliation(results)
        assert isinstance(result, Produced)
        s = result.summary
        assert s.kind is OperationKind.RECONCILIATION
        assert [(i.id, i.outcome, i.reason, i.action) for i in s.items] == [
            ("r1", ItemOutcome.SUCCEEDED, None, ALIGN),
            ("r2", ItemOutcome.FAILED, "AccessDenied", ADOPT),
            ("r3", ItemOutcome.SKIPPED, "no longer drifted", ALIGN),
        ]
        assert (s.total, s.succeeded, s.failed, s.skipped) == (3, 1, 1, 1)


class TestSummaryProductionFailure:
    def test_detection_failure_returns_error_and_preserves_outcomes(self):
        bad = StackDetectionOutcome("bad", object())  # unsupported outcome
        good = StackDetectionOutcome("ok", Detected(DriftStatus.IN_SYNC))
        results = DetectionResults((good, bad))
        result = summarize_detection(results, unmatched_names=("ghost",))
        assert isinstance(result, SummaryError)
        assert result.kind is OperationKind.DETECTION
        assert result.message
        assert result.outcomes == (good, bad)
        assert result.unmatchedNames == ("ghost",)

    def test_reconciliation_failure_returns_error_and_preserves_outcomes(self):
        good = ReconciliationOutcome("r1", ALIGN, Succeeded())
        bad = ReconciliationOutcome("r2", ALIGN, object())
        result = summarize_reconciliation(ReconciliationResults((good, bad), total=2))
        assert isinstance(result, SummaryError)
        assert result.kind is OperationKind.RECONCILIATION
        assert result.message
        assert result.outcomes == (good, bad)
