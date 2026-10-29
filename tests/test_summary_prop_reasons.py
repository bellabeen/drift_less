"""Property test: failed and skipped summary items carry identifier and reason.

Feature: multi-stack-drift-manager, Property 23: Failed and skipped summary items carry identifier and reason

*For any* operation summary, every item whose outcome is failed or skipped
exposes its item identifier and a recorded reason.

**Validates: Requirements 6.4**

Covers all three producers of an OperationSummary:

* ``summarize_detection``      (stacks; Unknown -> FAILED, Skipped -> SKIPPED)
* ``summarize_reconciliation`` (resources; Failed -> FAILED, ReconciliationSkipped -> SKIPPED)
* ``ProgressTracker.summary``  (items recorded via record_failure / record_skip,
  plus items with no recorded outcome, which are reported as skipped)
"""

from __future__ import annotations

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
from drift_manager.summary import (
    MISSING_REASON,
    Produced,
    summarize_detection,
    summarize_reconciliation,
)
from tests.fakes import FakeClock

# ---------------------------------------------------------------------------
# Local strategies
# ---------------------------------------------------------------------------

_ids = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=20,
)

# Reasons deliberately include empty and whitespace-only (blank) values.
_reasons = st.one_of(
    st.just(""),
    st.text(alphabet=" \t\n", min_size=1, max_size=4),
    st.text(min_size=1, max_size=40),
)

_detection_outcomes = st.one_of(
    st.builds(Detected, status=st.sampled_from(list(DriftStatus))),
    st.builds(Unknown, reason=_reasons),
    st.builds(Skipped, reason=_reasons),
)

_detection_results = st.lists(
    st.builds(StackDetectionOutcome, stackName=_ids, outcome=_detection_outcomes),
    max_size=15,
).map(lambda outs: DetectionResults(outcomes=tuple(outs)))

_reconciliation_results = st.lists(
    st.builds(
        ReconciliationOutcome,
        resourceId=_ids,
        action=st.sampled_from(list(ReconciliationAction)),
        result=st.one_of(
            st.just(Succeeded()),
            st.builds(Failed, reason=_reasons),
            st.builds(ReconciliationSkipped, reason=_reasons),
        ),
    ),
    max_size=15,
).map(
    lambda outs: ReconciliationResults(
        outcomes=tuple(outs),
        total=len(outs),
        succeeded=sum(isinstance(o.result, Succeeded) for o in outs),
        failed=sum(isinstance(o.result, Failed) for o in outs),
    )
)

# Tracker plan: unique item ids, each with an optional recorded outcome
# (None = never recorded before the summary is taken).
_tracker_plan = st.lists(_ids, unique=True, max_size=15).flatmap(
    lambda ids: st.tuples(
        st.just(ids),
        st.lists(
            st.one_of(
                st.none(),
                st.just(("success", None)),
                st.tuples(st.just("failure"), _reasons),
                st.tuples(st.just("skip"), _reasons),
            ),
            min_size=len(ids),
            max_size=len(ids),
        ),
    )
)


def _is_blank(text: object) -> bool:
    return not isinstance(text, str) or not text.strip()


def _expected_reason(reason: str) -> str:
    return MISSING_REASON if _is_blank(reason) else reason


_NON_SUCCESS = (ItemOutcome.FAILED, ItemOutcome.SKIPPED)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


# Feature: multi-stack-drift-manager, Property 23: Failed and skipped summary items carry identifier and reason
@settings(max_examples=100)
@given(results=_detection_results)
def test_detection_summary_failed_skipped_carry_id_and_reason(results):
    produced = summarize_detection(results)
    assert isinstance(produced, Produced)
    items = produced.summary.items
    assert len(items) == len(results.outcomes)

    for stack, item in zip(results.outcomes, items):
        assert item.id == stack.stackName
        outcome = stack.outcome
        if isinstance(outcome, (Unknown, Skipped)):
            expected = ItemOutcome.FAILED if isinstance(outcome, Unknown) else ItemOutcome.SKIPPED
            assert item.outcome is expected
            assert item.id  # non-empty identifier
            assert not _is_blank(item.reason)
            assert item.reason == _expected_reason(outcome.reason)
        else:
            assert item.outcome is ItemOutcome.SUCCEEDED
            assert item.reason is None


# Feature: multi-stack-drift-manager, Property 23: Failed and skipped summary items carry identifier and reason
@settings(max_examples=100)
@given(results=_reconciliation_results)
def test_reconciliation_summary_failed_skipped_carry_id_and_reason(results):
    produced = summarize_reconciliation(results)
    assert isinstance(produced, Produced)
    items = produced.summary.items
    assert len(items) == len(results.outcomes)

    for rec, item in zip(results.outcomes, items):
        assert item.id == rec.resourceId
        result = rec.result
        if isinstance(result, (Failed, ReconciliationSkipped)):
            expected = ItemOutcome.FAILED if isinstance(result, Failed) else ItemOutcome.SKIPPED
            assert item.outcome is expected
            assert item.id
            assert not _is_blank(item.reason)
            assert item.reason == _expected_reason(result.reason)
        else:
            assert item.outcome is ItemOutcome.SUCCEEDED
            assert item.reason is None


# Feature: multi-stack-drift-manager, Property 23: Failed and skipped summary items carry identifier and reason
@settings(max_examples=100)
@given(plan=_tracker_plan, kind=st.sampled_from(list(OperationKind)))
def test_tracker_summary_failed_skipped_carry_id_and_reason(plan, kind):
    ids, actions = plan
    tracker = ProgressTracker(FakeClock())
    op = tracker.start(kind, tuple(ids))
    for item_id, action in zip(ids, actions):
        if action is None:
            continue
        verb, reason = action
        if verb == "success":
            op.record_success(item_id)
        elif verb == "failure":
            op.record_failure(item_id, reason)
        else:
            op.record_skip(item_id, reason)

    items = tracker.summary(op).items
    assert [item.id for item in items] == list(ids)

    for item, action in zip(items, actions):
        if action is not None and action[0] == "success":
            assert item.outcome is ItemOutcome.SUCCEEDED
            assert item.reason is None
            continue
        assert item.outcome in _NON_SUCCESS
        assert item.id
        assert not _is_blank(item.reason)
        if action is not None and not _is_blank(action[1]):
            # A non-blank recorded reason is surfaced verbatim.
            assert item.reason == action[1]
