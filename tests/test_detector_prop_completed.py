"""Property test for completed-status recording (task 5.4).

Feature: multi-stack-drift-manager, Property 7: Completed detection records the reported stack status
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.detector import detect_all
from drift_manager.models import Detected, DriftStatus, Names
from drift_manager.selector import StackSelector

from tests.fakes import FakeCfnClient, FakeClock

IN_PROGRESS = "DETECTION_IN_PROGRESS"
COMPLETE = "DETECTION_COMPLETE"

# Each stack: (reported stack drift status, number of in-progress polls before
# completion). In-progress polls are capped well under the 300s / 5s = 60-poll
# timeout so every generated detection actually reaches completion.
_stack_script = st.tuples(
    st.sampled_from(list(DriftStatus)),
    st.integers(min_value=0, max_value=20),
)


# Feature: multi-stack-drift-manager, Property 7: Completed detection records the reported stack status
@settings(max_examples=100, deadline=None)
@given(scripts=st.lists(_stack_script, min_size=1, max_size=25))
def test_completed_detection_records_reported_status(
    scripts: list[tuple[DriftStatus, int]],
) -> None:
    """**Validates: Requirements 2.3**"""
    clock = FakeClock()
    cfn = FakeCfnClient(clock)
    names = [f"stack-{i:02d}" for i in range(len(scripts))]
    for name, (status, in_progress_polls) in zip(names, scripts):
        cfn.add_stack(name)
        cfn.script_detection(
            name,
            status_progression=[IN_PROGRESS] * in_progress_polls + [COMPLETE],
            stack_drift_status=status.value,
        )

    selector = StackSelector(cfn)
    confirmed = selector.confirm(selector.resolve(Names(names=tuple(names))))
    results = detect_all(cfn, clock, confirmed)

    by_name = {o.stackName: o.outcome for o in results.outcomes}
    assert set(by_name) == set(names)
    for name, (status, _) in zip(names, scripts):
        outcome = by_name[name]
        assert isinstance(outcome, Detected), f"{name}: {outcome!r}"
        assert outcome.status is status
