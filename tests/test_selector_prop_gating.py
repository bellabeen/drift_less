"""Property-based test for the Stack_Selector confirmation gate (task 2.6).

Feature: multi-stack-drift-manager, Property 5: Detection is gated on confirmation

For any resolved set, no drift detection is initiated for any stack before the
resolved set has been presented for confirmation; and if the user cancels, no
drift detection is initiated for any stack.

**Validates: Requirements 1.9, 1.11**
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.detector import BulkDetection
from drift_manager.models import Names
from drift_manager.selector import Aborted, Resolved, StackSelector
from tests.fakes import FakeCfnClient, FakeClock

_stack_names = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")) | st.just("-"),
    min_size=1,
    max_size=12,
)


@st.composite
def gating_scenarios(draw):
    """An inventory, a names criterion resolving to >=1 stack, and a decision."""
    inventory = draw(st.lists(_stack_names, min_size=1, max_size=25, unique=True))
    selected = draw(
        st.lists(st.sampled_from(inventory), min_size=1, max_size=len(inventory))
    )
    extra = draw(st.lists(_stack_names, max_size=5))  # possibly unmatched names
    names = tuple(draw(st.permutations(selected + extra)))
    decision = draw(st.sampled_from(["confirm", "cancel"]))
    return inventory, names, decision


# Feature: multi-stack-drift-manager, Property 5: Detection is gated on confirmation
@settings(max_examples=100, deadline=None)
@given(scenario=gating_scenarios())
def test_detection_is_gated_on_confirmation(scenario):
    inventory, names, decision = scenario
    clock = FakeClock()
    cfn = FakeCfnClient(clock=clock)
    for name in inventory:
        cfn.add_stack(name)

    selector = StackSelector(cfn)
    resolved = selector.resolve(Names(names=names))
    assert isinstance(resolved, Resolved)

    # Before confirmation: resolution initiated no detection.
    assert cfn.call_count("detect_stack_drift") == 0

    # An unconfirmed resolved set cannot start detection.
    with pytest.raises(TypeError):
        BulkDetection(cfn, clock, resolved)  # type: ignore[arg-type]
    assert cfn.call_count("detect_stack_drift") == 0

    presented_at_seq = len(cfn.calls)

    if decision == "cancel":
        aborted = selector.cancel(resolved)
        assert isinstance(aborted, Aborted)
        # A cancelled selection can never be fed to the detector.
        with pytest.raises(TypeError):
            BulkDetection(cfn, clock, aborted)  # type: ignore[arg-type]
        assert cfn.call_count("detect_stack_drift") == 0
    else:
        confirmed = selector.confirm(resolved)
        BulkDetection(cfn, clock, confirmed).run()
        detect_calls = cfn.calls_for("detect_stack_drift")
        # Every initiation happens strictly after the set was presented.
        assert all(c.seq >= presented_at_seq for c in detect_calls)
        # Detection runs on exactly the confirmed stacks.
        assert sorted(c.kwargs["StackName"] for c in detect_calls) == sorted(
            s.name for s in resolved.stacks
        )
