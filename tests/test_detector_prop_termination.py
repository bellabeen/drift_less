"""Property test for bulk-detection termination (task 5.7).

Feature: multi-stack-drift-manager, Property 10: Bulk detection terminates with exactly one terminal outcome per stack

**Validates: Requirements 2.7**

For any confirmed set, bulk detection signals completion if and only if every
stack in the set has exactly one recorded terminal outcome (detected status,
UNKNOWN, or skipped).
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.detector import BulkDetection
from drift_manager.models import Detected, DetectionResults, Names, Skipped, Unknown
from drift_manager.progress import ProgressTracker
from drift_manager.selector import StackSelector

from tests.fakes import FakeCfnClient, FakeClock

IN_PROGRESS = "DETECTION_IN_PROGRESS"
COMPLETE = "DETECTION_COMPLETE"
FAILED = "DETECTION_FAILED"

#: Upper bound on scheduling rounds; generous enough for 20 stacks that each
#: time out at 300s with concurrency 1 (20 * 61 polls), so hitting it means the
#: bulk run failed to terminate.
MAX_TICKS = 5000


@dataclass(frozen=True)
class Scenario:
    """kind: ``complete`` | ``failed`` | ``timeout`` | ``init_error`` | ``skipped``."""

    kind: str
    in_progress_polls: int = 0
    drift_status: str = "IN_SYNC"


_polls = st.integers(min_value=0, max_value=8)

scenarios = st.one_of(
    st.builds(
        Scenario,
        kind=st.just("complete"),
        in_progress_polls=_polls,
        drift_status=st.sampled_from(["IN_SYNC", "DRIFTED"]),
    ),
    st.builds(Scenario, kind=st.just("failed"), in_progress_polls=_polls),
    st.builds(Scenario, kind=st.just("timeout")),
    st.builds(Scenario, kind=st.just("init_error")),
    st.builds(Scenario, kind=st.just("skipped")),
)

EXPECTED_OUTCOME = {
    "complete": Detected,
    "failed": Unknown,
    "timeout": Unknown,
    "init_error": Unknown,
    "skipped": Skipped,
}


def _script(cfn: FakeCfnClient, name: str, scenario: Scenario) -> None:
    if scenario.kind == "skipped":
        cfn.add_stack(name, status="UPDATE_IN_PROGRESS")
        cfn.script_detection(name)
        return
    cfn.add_stack(name)
    if scenario.kind == "init_error":
        cfn.script_detection(name, initiation_error=RuntimeError("throttled"))
    elif scenario.kind == "failed":
        cfn.script_detection(
            name,
            status_progression=[IN_PROGRESS] * scenario.in_progress_polls + [FAILED],
            detection_status_reason="resource read failed",
        )
    elif scenario.kind == "timeout":
        cfn.script_detection(name, status_progression=[IN_PROGRESS])
    else:
        cfn.script_detection(
            name,
            status_progression=[IN_PROGRESS] * scenario.in_progress_polls + [COMPLETE],
            stack_drift_status=scenario.drift_status,
        )


# Feature: multi-stack-drift-manager, Property 10: Bulk detection terminates with exactly one terminal outcome per stack
@settings(max_examples=100, deadline=None)
@given(
    stack_scenarios=st.lists(scenarios, min_size=1, max_size=20),
    max_concurrency=st.integers(min_value=1, max_value=10),
)
def test_bulk_detection_completes_iff_every_stack_has_one_terminal_outcome(
    stack_scenarios: list[Scenario], max_concurrency: int
) -> None:
    """**Validates: Requirements 2.7**"""
    clock = FakeClock()
    cfn = FakeCfnClient(clock=clock)
    names = [f"stack-{i:02d}" for i in range(len(stack_scenarios))]
    for name, scenario in zip(names, stack_scenarios):
        _script(cfn, name, scenario)
    selector = StackSelector(cfn)
    confirmed = selector.confirm(selector.resolve(Names(names=tuple(names))))

    tracker = ProgressTracker(clock)
    signals: list[DetectionResults] = []
    bulk = BulkDetection(
        cfn,
        clock,
        confirmed,
        progress=tracker,
        max_concurrency=max_concurrency,
        on_complete=signals.append,
    )
    operation = bulk.operation
    assert operation is not None

    # Drive round by round; after every round, completion is signalled iff
    # every stack has a recorded terminal outcome.
    ticks = 0
    while True:
        done = bulk.tick()
        ticks += 1
        snapshot = tracker.observe(operation)
        all_terminal = snapshot.completed == snapshot.total == len(names)
        assert done == bulk.is_complete == all_terminal, (ticks, snapshot)
        if done:
            break
        assert bulk.results is None
        assert signals == []
        assert ticks < MAX_TICKS, "bulk detection did not terminate"

    # Completion signalled exactly once, with the bulk results.
    assert len(signals) == 1
    results = bulk.results
    assert results is not None and signals[0] == results

    # Exactly one terminal outcome per stack, in confirmed-set order, of the
    # kind dictated by the stack's scenario.
    assert [o.stackName for o in results.outcomes] == names
    for outcome, scenario in zip(results.outcomes, stack_scenarios):
        assert isinstance(outcome.outcome, EXPECTED_OUTCOME[scenario.kind]), (
            scenario,
            outcome,
        )
        if isinstance(outcome.outcome, (Unknown, Skipped)):
            assert outcome.outcome.reason.strip()

    # Initiation happens at most once per stack (skipped stacks never).
    initiated = [c.kwargs["StackName"] for c in cfn.calls_for("detect_stack_drift")]
    assert len(initiated) == len(set(initiated))
    assert set(initiated) == {
        n for n, s in zip(names, stack_scenarios) if s.kind != "skipped"
    }

    # Completion is final: further ticks signal nothing new and call nothing.
    calls_before = cfn.call_count()
    clock.advance(1000)
    assert bulk.tick() is True
    assert cfn.call_count() == calls_before
    assert len(signals) == 1
    assert bulk.results == results
