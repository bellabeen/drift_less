"""Edge-case unit tests for the Drift_Reporter and operation summaries (task 7.11).

Complements tests/test_reporter.py and tests/test_summary.py with coverage of:

* 3.2 — an empty resolved set yields an empty report whose message and zero
  counts survive the machine-readable / JSON round-trip, and a non-empty
  report never carries the empty message.
* 6.5 — when summary production fails, an error indication is returned and
  every recorded per-item outcome is preserved (including those after the
  failing item, with their original object identities); empty results are a
  successful summary, not an error.

**Validates: Requirements 3.2, 6.5**
"""

import json

from drift_manager.models import (
    Detected,
    DetectionResults,
    DriftStatus,
    Failed,
    OperationKind,
    ReconciliationAction,
    ReconciliationOutcome,
    ReconciliationResults,
    ReconciliationSkipped,
    Skipped,
    StackDetectionOutcome,
    StatusCounts,
    Succeeded,
    Unknown,
)
from drift_manager.reporter import (
    EMPTY_REPORT_MESSAGE,
    from_json,
    from_machine_readable,
    report,
    to_json,
    to_machine_readable,
)
from drift_manager.summary import (
    Produced,
    SummaryError,
    summarize_detection,
    summarize_reconciliation,
)

ALIGN = ReconciliationAction.ALIGN_TO_TEMPLATE
ADOPT = ReconciliationAction.ADOPT_ACTUAL


class _Unsupported:
    """An outcome/result variant the summarizer does not know how to handle."""

    def __repr__(self) -> str:
        return "<unsupported-variant>"


# ---------------------------------------------------------------------------
# Requirement 3.2 — empty resolved set
# ---------------------------------------------------------------------------


class TestEmptyReport:
    def test_empty_report_has_zero_counts_and_descriptive_message(self):
        r = report(DetectionResults())
        assert r.entries == ()
        counts = r.counts
        assert (counts.DRIFTED, counts.IN_SYNC, counts.NOT_CHECKED, counts.UNKNOWN) == (0, 0, 0, 0)
        assert isinstance(r.emptyMessage, str) and r.emptyMessage.strip()
        assert r.emptyMessage == EMPTY_REPORT_MESSAGE

    def test_empty_report_structured_form_exposes_no_stacks_and_message(self):
        data = to_machine_readable(report(DetectionResults()))
        assert data["stacks"] == []
        assert data["counts"] == {"DRIFTED": 0, "IN_SYNC": 0, "NOT_CHECKED": 0, "UNKNOWN": 0}
        assert data["emptyMessage"] == EMPTY_REPORT_MESSAGE

    def test_empty_report_round_trips_via_json_with_message(self):
        original = report(DetectionResults())
        text = to_json(original)
        assert json.loads(text)["emptyMessage"] == EMPTY_REPORT_MESSAGE
        restored = from_json(text)
        assert restored == original
        assert restored.emptyMessage == EMPTY_REPORT_MESSAGE
        assert from_machine_readable(to_machine_readable(original)) == original

    def test_single_stack_report_has_no_empty_message(self):
        # Any non-empty resolved set — even one where nothing was checked —
        # must not be reported as empty.
        r = report(DetectionResults((StackDetectionOutcome("a", Skipped("UPDATE_IN_PROGRESS")),)))
        assert len(r.entries) == 1
        assert r.emptyMessage is None
        assert r.counts == StatusCounts(NOT_CHECKED=1)
        assert from_json(to_json(r)).emptyMessage is None


# ---------------------------------------------------------------------------
# Requirement 6.5 — summary production failure
# ---------------------------------------------------------------------------


class TestDetectionSummaryFailure:
    def _results_with_mid_list_bad_item(self):
        before = StackDetectionOutcome("before", Detected(DriftStatus.DRIFTED))
        bad = StackDetectionOutcome("bad", _Unsupported())
        after_unknown = StackDetectionOutcome("after-unknown", Unknown("timed out"))
        after_skipped = StackDetectionOutcome("after-skipped", Skipped("ROLLBACK_IN_PROGRESS"))
        outcomes = (before, bad, after_unknown, after_skipped)
        return DetectionResults(outcomes), outcomes

    def test_mid_list_failure_preserves_all_outcomes_in_order_and_identity(self):
        results, outcomes = self._results_with_mid_list_bad_item()
        result = summarize_detection(results)
        assert isinstance(result, SummaryError)
        assert result.kind is OperationKind.DETECTION
        assert len(result.outcomes) == len(outcomes)
        # Items after the failing one are kept, untouched (same objects).
        assert all(kept is orig for kept, orig in zip(result.outcomes, outcomes))

    def test_error_message_is_non_empty_and_describes_the_cause(self):
        results, _ = self._results_with_mid_list_bad_item()
        result = summarize_detection(results)
        assert isinstance(result, SummaryError)
        assert result.message.strip()
        assert "detection" in result.message.lower()
        assert "<unsupported-variant>" in result.message

    def test_failure_does_not_raise_when_every_item_is_bad(self):
        bad = (StackDetectionOutcome("x", _Unsupported()), StackDetectionOutcome("y", None))
        result = summarize_detection(DetectionResults(bad))
        assert isinstance(result, SummaryError)
        assert result.outcomes == bad

    def test_empty_results_produce_summary_not_error(self):
        result = summarize_detection(DetectionResults(), unmatched_names=("ghost",))
        assert isinstance(result, Produced)
        assert result.summary.total == 0
        assert result.summary.unmatchedNames == ("ghost",)


class TestReconciliationSummaryFailure:
    def test_mid_list_failure_preserves_all_outcomes_in_order_and_identity(self):
        outcomes = (
            ReconciliationOutcome("r1", ALIGN, Succeeded()),
            ReconciliationOutcome("r2", ADOPT, _Unsupported()),
            ReconciliationOutcome("r3", ALIGN, Failed("AccessDenied")),
            ReconciliationOutcome("r4", ADOPT, ReconciliationSkipped("no longer drifted")),
        )
        result = summarize_reconciliation(ReconciliationResults(outcomes, total=4, succeeded=1, failed=1))
        assert isinstance(result, SummaryError)
        assert result.kind is OperationKind.RECONCILIATION
        assert "reconciliation" in result.message.lower()
        assert "<unsupported-variant>" in result.message
        assert len(result.outcomes) == len(outcomes)
        assert all(kept is orig for kept, orig in zip(result.outcomes, outcomes))
        # Recorded actions and results are preserved as-is.
        assert [(o.resourceId, o.action) for o in result.outcomes] == [
            ("r1", ALIGN),
            ("r2", ADOPT),
            ("r3", ALIGN),
            ("r4", ADOPT),
        ]

    def test_empty_results_produce_summary_not_error(self):
        result = summarize_reconciliation(ReconciliationResults())
        assert isinstance(result, Produced)
        s = result.summary
        assert s.kind is OperationKind.RECONCILIATION
        assert s.items == ()
        assert (s.total, s.succeeded, s.failed, s.skipped) == (0, 0, 0, 0)
