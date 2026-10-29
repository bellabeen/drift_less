"""Property test for Progress_Tracker count conservation (task 4.2).

Feature: multi-stack-drift-manager, Property 21: Progress conserves the item total
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import OperationKind
from drift_manager.progress import ProgressTracker
from tests.fakes import FakeClock

_OUTCOMES = ("success", "failure", "skip")


@st.composite
def operations(draw):
    """An operation kind, its unique item ids, and a sequence of status events.

    Events reference items by index (so they always target real items) and may
    repeat an item, exercising the tracker's first-outcome-wins idempotency.
    Each event also advances the fake clock by a small non-negative amount.
    """
    kind = draw(st.sampled_from(list(OperationKind)))
    item_ids = draw(
        st.lists(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12),
            min_size=0,
            max_size=30,
            unique=True,
        )
    )
    if item_ids:
        event = st.tuples(
            st.integers(min_value=0, max_value=len(item_ids) - 1),
            st.sampled_from(_OUTCOMES),
            st.floats(min_value=0.0, max_value=10.0, allow_nan=False),
        )
        events = draw(st.lists(event, max_size=60))
    else:
        events = []
    return kind, tuple(item_ids), events


def _assert_conserved(snapshot, expected_total: int) -> None:
    assert snapshot.total == expected_total
    assert snapshot.completed + snapshot.remaining == snapshot.total
    assert 0 <= snapshot.completed <= snapshot.total
    assert 0 <= snapshot.remaining <= snapshot.total


# Feature: multi-stack-drift-manager, Property 21: Progress conserves the item total
@settings(max_examples=100)
@given(operations())
def test_progress_conserves_item_total(op):
    """**Validates: Requirements 5.1, 5.2**

    At every observed instant of a detection or reconciliation operation,
    completed + remaining == total.
    """
    kind, item_ids, events = op
    clock = FakeClock()
    tracker = ProgressTracker(clock)
    operation = tracker.start(kind, item_ids)

    _assert_conserved(tracker.observe(operation), len(item_ids))

    for index, outcome, delta in events:
        clock.advance(delta)
        item_id = item_ids[index]
        if outcome == "success":
            operation.record_success(item_id)
        elif outcome == "failure":
            operation.record_failure(item_id, "boom")
        else:
            operation.record_skip(item_id, "not applicable")
        _assert_conserved(tracker.observe(operation), len(item_ids))
