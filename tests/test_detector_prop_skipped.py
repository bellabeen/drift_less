"""Property test for skipped-stack handling in the Drift_Detector (task 5.6).

Feature: multi-stack-drift-manager, Property 9: Detection-disallowed stacks are skipped without initiation

For any stack in a state that does not permit drift detection, the recorded
outcome is a skipped outcome with a reason, and no detection is initiated for
that stack.

**Validates: Requirements 2.5**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.detector import DETECTABLE_STACK_STATUSES, detect_all
from drift_manager.models import Names, Skipped
from drift_manager.selector import ConfirmedSet, StackSelector

from tests.fakes import FakeCfnClient, FakeClock

#: CloudFormation stack statuses on which drift detection is not permitted.
DISALLOWED_STATUSES = (
    "CREATE_IN_PROGRESS",
    "CREATE_FAILED",
    "ROLLBACK_IN_PROGRESS",
    "ROLLBACK_FAILED",
    "ROLLBACK_COMPLETE",
    "DELETE_IN_PROGRESS",
    "DELETE_FAILED",
    "DELETE_COMPLETE",
    "UPDATE_IN_PROGRESS",
    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
    "UPDATE_ROLLBACK_IN_PROGRESS",
    "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
    "UPDATE_FAILED",
    "REVIEW_IN_PROGRESS",
    "IMPORT_IN_PROGRESS",
    "IMPORT_ROLLBACK_IN_PROGRESS",
)
assert not set(DISALLOWED_STATUSES) & DETECTABLE_STACK_STATUSES

ALLOWED_STATUSES = tuple(sorted(DETECTABLE_STACK_STATUSES))


# Each stack: (disallowed?, status). Mix disallowed stacks with detectable ones
# so the property is checked inside a realistic bulk run, not only in isolation.
_stack_spec = st.one_of(
    st.tuples(st.just(True), st.sampled_from(DISALLOWED_STATUSES)),
    st.tuples(st.just(False), st.sampled_from(ALLOWED_STATUSES)),
)

_stack_specs = st.lists(_stack_spec, min_size=1, max_size=25).filter(
    lambda specs: any(disallowed for disallowed, _ in specs)
)


def _confirm(cfn: FakeCfnClient, names: list[str]) -> ConfirmedSet:
    selector = StackSelector(cfn)
    return selector.confirm(selector.resolve(Names(names=tuple(names))))


# Feature: multi-stack-drift-manager, Property 9: Detection-disallowed stacks are skipped without initiation
@settings(max_examples=100, deadline=None)
@given(specs=_stack_specs)
def test_detection_disallowed_stacks_are_skipped_without_initiation(
    specs: list[tuple[bool, str]],
) -> None:
    clock = FakeClock()
    cfn = FakeCfnClient(clock)
    names = [f"stack-{i:02d}" for i in range(len(specs))]
    disallowed = set()
    for name, (is_disallowed, status) in zip(names, specs):
        cfn.add_stack(name, status=status)
        cfn.script_detection(name, status_progression=["DETECTION_COMPLETE"])
        if is_disallowed:
            disallowed.add(name)

    results = detect_all(cfn, clock, _confirm(cfn, names))

    initiated = {c.kwargs["StackName"] for c in cfn.calls_for("detect_stack_drift")}
    by_name = {o.stackName: o.outcome for o in results.outcomes}

    for name in disallowed:
        outcome = by_name[name]
        # Recorded outcome is a skipped outcome carrying a reason.
        assert isinstance(outcome, Skipped)
        assert isinstance(outcome.reason, str) and outcome.reason.strip()
        # No detection was initiated for the stack.
        assert name not in initiated

    # Detectable stacks are not skipped (the skip is driven by stack state).
    for name in set(names) - disallowed:
        assert not isinstance(by_name[name], Skipped)
        assert name in initiated
