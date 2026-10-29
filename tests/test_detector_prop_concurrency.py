"""Property test for the bulk-detection concurrency bound (task 5.3).

Feature: multi-stack-drift-manager, Property 6: Concurrency never exceeds the bound

In-flight is measured independently of the detector's own bookkeeping, from the
FakeCfnClient call log: a stack is in flight from its successful
``DetectStackDrift`` call through the last status poll of that detection (the
poll on which it reached Completed, Unknown/failed, or Unknown/timeout).
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.detector import MAX_CONCURRENCY, BulkDetection
from drift_manager.models import Names
from drift_manager.selector import StackSelector

from tests.fakes import FakeCfnClient, FakeClock

IN_PROGRESS = "DETECTION_IN_PROGRESS"
COMPLETE = "DETECTION_COMPLETE"
FAILED = "DETECTION_FAILED"


class _TrackingCfnClient(FakeCfnClient):
    """FakeCfnClient that remembers the call seq of each successful initiation."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        #: StackDriftDetectionId -> seq of the DetectStackDrift call.
        self.initiated_at: dict[str, int] = {}

    def detect_stack_drift(self, *, StackName: str, **kwargs: Any) -> dict[str, Any]:
        seq = len(self.calls)  # index the super() call will be recorded at
        response = super().detect_stack_drift(StackName=StackName, **kwargs)
        self.initiated_at[response["StackDriftDetectionId"]] = seq
        return response


# Per-stack behaviour, covering every way a stack can hold or skip a slot.
_stack_script = st.one_of(
    # Completes after n in-progress polls.
    st.tuples(st.just("complete"), st.integers(min_value=0, max_value=15)),
    # Fails after n in-progress polls.
    st.tuples(st.just("failed"), st.integers(min_value=0, max_value=15)),
    # Never completes -> per-stack timeout.
    st.tuples(st.just("timeout"), st.just(0)),
    # Initiation raises -> Unknown without ever holding a slot.
    st.tuples(st.just("init_error"), st.just(0)),
    # Stack status disallows detection -> Skipped without initiating.
    st.tuples(st.just("skipped"), st.just(0)),
)


def _max_overlap(cfn: _TrackingCfnClient) -> int:
    """Max number of detections in flight at any recorded call."""
    last_poll: dict[str, int] = {}
    for record in cfn.calls:
        if record.method == "describe_stack_drift_detection_status":
            last_poll[record.kwargs["StackDriftDetectionId"]] = record.seq

    intervals = []
    for detection_id, start in cfn.initiated_at.items():
        # Every initiated detection must reach a terminal poll.
        assert detection_id in last_poll, f"{detection_id} never polled"
        intervals.append((start, last_poll[detection_id]))

    peak = 0
    for record in cfn.calls:
        live = sum(1 for start, end in intervals if start <= record.seq <= end)
        peak = max(peak, live)
    return peak


# Feature: multi-stack-drift-manager, Property 6: Concurrency never exceeds the bound
@settings(max_examples=100, deadline=None)
@given(scripts=st.lists(_stack_script, min_size=1, max_size=40))
def test_concurrency_never_exceeds_bound(scripts: list[tuple[str, int]]) -> None:
    """**Validates: Requirements 2.1**"""
    clock = FakeClock()
    cfn = _TrackingCfnClient(clock)
    names = [f"stack-{i:02d}" for i in range(len(scripts))]
    for name, (kind, polls) in zip(names, scripts):
        if kind == "skipped":
            cfn.add_stack(name, status="UPDATE_IN_PROGRESS")
            continue
        cfn.add_stack(name)
        if kind == "complete":
            cfn.script_detection(
                name, status_progression=[IN_PROGRESS] * polls + [COMPLETE]
            )
        elif kind == "failed":
            cfn.script_detection(
                name,
                status_progression=[IN_PROGRESS] * polls + [FAILED],
                detection_status_reason="scripted failure",
            )
        elif kind == "timeout":
            cfn.script_detection(name, status_progression=[IN_PROGRESS])
        else:  # init_error
            cfn.script_detection(
                name, initiation_error=RuntimeError("AccessDenied: scripted")
            )

    selector = StackSelector(cfn)
    confirmed = selector.confirm(selector.resolve(Names(names=tuple(names))))
    bulk = BulkDetection(cfn, clock, confirmed)

    # Check the detector's live in-flight count after every scheduling round.
    while not bulk.tick():
        assert bulk.in_flight_count <= MAX_CONCURRENCY

    assert MAX_CONCURRENCY == 10
    observed_peak = _max_overlap(cfn)
    assert observed_peak <= MAX_CONCURRENCY
    assert bulk.peak_in_flight <= MAX_CONCURRENCY

    # Non-vacuity: the pool fills up to the bound when enough stacks initiate.
    assert observed_peak == min(MAX_CONCURRENCY, len(cfn.initiated_at))
    assert len(bulk.results.outcomes) == len(names)
