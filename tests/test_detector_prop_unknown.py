"""Property test for terminal UNKNOWN on non-completion (task 5.5).

Feature: multi-stack-drift-manager, Property 8: Non-completion yields a terminal UNKNOWN with reason

**Validates: Requirements 2.4, 2.6**

For any stack whose detection fails (``DETECTION_FAILED`` or an initiation
error) or does not complete within the 300s per-stack timeout, the recorded
outcome is ``UNKNOWN`` with a stored reason, and no further polling or state
transition occurs for that stack afterward.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.detector import (
    PER_STACK_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
    BulkDetection,
    DetectionState,
    StackDetection,
)
from drift_manager.models import Detected, Names, Unknown
from drift_manager.selector import StackSelector

from tests.fakes import FakeCfnClient, FakeClock

IN_PROGRESS = "DETECTION_IN_PROGRESS"
COMPLETE = "DETECTION_COMPLETE"
FAILED = "DETECTION_FAILED"
POLL_METHOD = "describe_stack_drift_detection_status"


@dataclass(frozen=True)
class Scenario:
    """How one stack's detection behaves.

    kind: ``init_error`` | ``failed`` | ``timeout`` | ``complete`` (the last is
    a control so non-completing stacks are mixed with completing ones).
    """

    kind: str
    in_progress_polls: int = 0
    reason: Optional[str] = None


_reason = st.one_of(
    st.none(), st.text(min_size=1, max_size=20).filter(lambda s: s.strip())
)

non_completion = st.one_of(
    st.builds(Scenario, kind=st.just("init_error"), reason=st.text(min_size=1, max_size=20)),
    st.builds(
        Scenario,
        kind=st.just("failed"),
        in_progress_polls=st.integers(min_value=0, max_value=70),
        reason=_reason,
    ),
    st.builds(Scenario, kind=st.just("timeout")),
)

any_scenario = st.one_of(
    non_completion,
    st.builds(
        Scenario,
        kind=st.just("complete"),
        in_progress_polls=st.integers(min_value=0, max_value=10),
    ),
)


def _script(cfn: FakeCfnClient, name: str, scenario: Scenario) -> None:
    cfn.add_stack(name)
    if scenario.kind == "init_error":
        cfn.script_detection(name, initiation_error=RuntimeError(scenario.reason))
    elif scenario.kind == "failed":
        cfn.script_detection(
            name,
            status_progression=[IN_PROGRESS] * scenario.in_progress_polls + [FAILED],
            detection_status_reason=scenario.reason,
        )
    elif scenario.kind == "timeout":
        cfn.script_detection(name, status_progression=[IN_PROGRESS])
    else:
        cfn.script_detection(
            name,
            status_progression=[IN_PROGRESS] * scenario.in_progress_polls + [COMPLETE],
        )


def _polls_for(cfn: FakeCfnClient, name: str) -> list[float]:
    return [
        c.time
        for c in cfn.calls_for(POLL_METHOD)
        if c.kwargs["StackDriftDetectionId"].endswith(f"-{name}")
    ]


def _assert_unknown_matches(
    cfn: FakeCfnClient, name: str, scenario: Scenario, outcome: object
) -> None:
    assert isinstance(outcome, Unknown), (name, scenario, outcome)
    assert outcome.reason and outcome.reason.strip()

    polls = _polls_for(cfn, name)
    init_times = [
        c.time
        for c in cfn.calls_for("detect_stack_drift")
        if c.kwargs["StackName"] == name
    ]
    assert len(init_times) == 1

    if scenario.kind == "init_error":
        assert polls == []
        assert scenario.reason in outcome.reason
    elif scenario.kind == "failed":
        # Detection stops at the first DETECTION_FAILED poll (or times out first
        # if failure would only be reported after 300s).
        expected_polls = min(
            scenario.in_progress_polls + 1,
            int(PER_STACK_TIMEOUT_SECONDS // POLL_INTERVAL_SECONDS),
        )
        assert len(polls) == expected_polls
        if len(polls) == scenario.in_progress_polls + 1 and scenario.reason:
            assert scenario.reason in outcome.reason
    else:  # timeout
        assert "timeout" in outcome.reason
        elapsed = polls[-1] - init_times[0]
        assert PER_STACK_TIMEOUT_SECONDS <= elapsed < (
            PER_STACK_TIMEOUT_SECONDS + POLL_INTERVAL_SECONDS
        )


# Feature: multi-stack-drift-manager, Property 8: Non-completion yields a terminal UNKNOWN with reason
@settings(max_examples=100, deadline=None)
@given(
    scenario=non_completion,
    extra_steps=st.integers(min_value=1, max_value=5),
    extra_wait=st.floats(min_value=0, max_value=1000, allow_nan=False),
)
def test_single_stack_non_completion_is_terminal_unknown(
    scenario: Scenario, extra_steps: int, extra_wait: float
) -> None:
    """**Validates: Requirements 2.4, 2.6**"""
    clock = FakeClock()
    cfn = FakeCfnClient(clock=clock)
    _script(cfn, "s1", scenario)
    stack = StackSelector(cfn).resolve(Names(names=("s1",))).stacks[0]

    machine = StackDetection(cfn, clock, stack)
    result = machine.run()

    assert machine.state is DetectionState.UNKNOWN
    assert machine.is_terminal
    _assert_unknown_matches(cfn, "s1", scenario, result.outcome)

    # No further polling or state transition after the terminal outcome.
    calls_before = cfn.call_count()
    outcome_before = machine.outcome
    clock.advance(extra_wait)
    for _ in range(extra_steps):
        assert machine.step() is DetectionState.UNKNOWN
    assert machine.run() == outcome_before
    assert cfn.call_count() == calls_before
    assert machine.outcome == outcome_before
    assert machine.next_poll_at is None


# Feature: multi-stack-drift-manager, Property 8: Non-completion yields a terminal UNKNOWN with reason
@settings(max_examples=100, deadline=None)
@given(scenarios=st.lists(any_scenario, min_size=1, max_size=15))
def test_bulk_non_completion_is_terminal_unknown(scenarios: list[Scenario]) -> None:
    """**Validates: Requirements 2.4, 2.6**"""
    clock = FakeClock()
    cfn = FakeCfnClient(clock=clock)
    names = [f"s{i:02d}" for i in range(len(scenarios))]
    for name, scenario in zip(names, scenarios):
        _script(cfn, name, scenario)
    selector = StackSelector(cfn)
    confirmed = selector.confirm(selector.resolve(Names(names=tuple(names))))

    bulk = BulkDetection(cfn, clock, confirmed)
    results = bulk.run()

    by_name = {o.stackName: o.outcome for o in results.outcomes}
    assert set(by_name) == set(names)
    for name, scenario in zip(names, scenarios):
        if scenario.kind == "complete":
            assert isinstance(by_name[name], Detected)
        else:
            _assert_unknown_matches(cfn, name, scenario, by_name[name])

    # Once complete, further ticks make no CloudFormation calls and leave the
    # recorded outcomes unchanged.
    calls_before = cfn.call_count()
    clock.advance(1000)
    assert bulk.tick() is True
    assert cfn.call_count() == calls_before
    assert bulk.results == results
