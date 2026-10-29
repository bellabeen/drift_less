"""Unit tests for the bounded-concurrency detection pool (task 5.2).

Requirements: 2.1, 2.7 (plus progress emission per 5.1 / 5.4).
"""

from __future__ import annotations

import pytest

from drift_manager.detector import BulkDetection, detect_all
from drift_manager.models import (
    Detected,
    DriftStatus,
    ItemOutcome,
    Names,
    Skipped,
    Unknown,
)
from drift_manager.progress import ProgressTracker
from drift_manager.selector import ConfirmedSet, StackSelector

from tests.fakes import FakeCfnClient, FakeClock

IN_PROGRESS = "DETECTION_IN_PROGRESS"
COMPLETE = "DETECTION_COMPLETE"
FAILED = "DETECTION_FAILED"


def _confirm(cfn: FakeCfnClient, names: list[str]) -> ConfirmedSet:
    selector = StackSelector(cfn)
    return selector.confirm(selector.resolve(Names(names=tuple(names))))


def _max_overlap_from_calls(cfn: FakeCfnClient) -> int:
    """Max simultaneously in-flight detections, reconstructed from call order.

    A detection is in flight from its ``detect_stack_drift`` call until its last
    ``describe_stack_drift_detection_status`` call.
    """
    detect_calls = cfn.calls_for("detect_stack_drift")
    polls = cfn.calls_for("describe_stack_drift_detection_status")
    # Detection ids are assigned in initiation order: drift-0001-<name>, ...
    intervals = []
    for i, call in enumerate(detect_calls, start=1):
        det_id = f"drift-{i:04d}-{call.kwargs['StackName']}"
        last = max(p.seq for p in polls if p.kwargs["StackDriftDetectionId"] == det_id)
        intervals.append((call.seq, last))
    return max(
        sum(1 for s, e in intervals if s <= start <= e) for start, _ in intervals
    )


def test_concurrency_never_exceeds_ten_and_slots_are_refilled(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    names = [f"s{i:02d}" for i in range(25)]
    for i, name in enumerate(names):
        cfn.add_stack(name)
        cfn.script_detection(
            name, status_progression=[IN_PROGRESS] * (i % 4) + [COMPLETE]
        )

    pool = BulkDetection(cfn, clock, _confirm(cfn, names))
    results = pool.run()

    assert pool.peak_in_flight == 10
    assert _max_overlap_from_calls(cfn) <= 10
    assert cfn.call_count("detect_stack_drift") == 25
    assert len(results.outcomes) == 25

    # The 11th initiation happens only after some stack reached a terminal poll.
    eleventh = cfn.calls_for("detect_stack_drift")[10].seq
    terminal_polls = [
        c.seq
        for c in cfn.calls_for("describe_stack_drift_detection_status")
        if c.seq < eleventh
    ]
    assert terminal_polls, "a slot must be released before admitting the 11th stack"


def test_custom_bound_is_respected(cfn: FakeCfnClient, clock: FakeClock) -> None:
    names = [f"s{i}" for i in range(7)]
    for name in names:
        cfn.add_stack(name)
        cfn.script_detection(name, status_progression=[IN_PROGRESS, COMPLETE])

    pool = BulkDetection(cfn, clock, _confirm(cfn, names), max_concurrency=3)
    pool.run()

    assert pool.peak_in_flight == 3
    assert _max_overlap_from_calls(cfn) <= 3


def test_every_stack_gets_exactly_one_terminal_outcome_in_order(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    cfn.add_stack("ok")
    cfn.script_detection("ok", stack_drift_status="DRIFTED")
    cfn.add_stack("failed")
    cfn.script_detection(
        "failed", status_progression=[FAILED], detection_status_reason="boom"
    )
    cfn.add_stack("busy", status="UPDATE_IN_PROGRESS")
    cfn.add_stack("init-error")
    cfn.script_detection("init-error", initiation_error=RuntimeError("throttled"))
    cfn.add_stack("slow")
    cfn.script_detection("slow", status_progression=[IN_PROGRESS])

    names = ["ok", "failed", "busy", "init-error", "slow"]
    results = detect_all(cfn, clock, _confirm(cfn, names))

    assert [o.stackName for o in results.outcomes] == names
    by_name = {o.stackName: o.outcome for o in results.outcomes}
    assert by_name["ok"] == Detected(status=DriftStatus.DRIFTED)
    assert isinstance(by_name["failed"], Unknown)
    assert isinstance(by_name["busy"], Skipped)
    assert isinstance(by_name["init-error"], Unknown)
    assert isinstance(by_name["slow"], Unknown)
    assert "timeout" in by_name["slow"].reason

    initiated = {c.kwargs["StackName"] for c in cfn.calls_for("detect_stack_drift")}
    assert "busy" not in initiated
    # The timeout bounds total elapsed time: 300s plus at most one poll interval.
    assert clock.monotonic() <= 305


def test_progress_is_emitted_per_stack_and_conserves_total(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    names = [f"s{i:02d}" for i in range(12)]
    for i, name in enumerate(names):
        cfn.add_stack(name)
        cfn.script_detection(name, status_progression=[IN_PROGRESS] * (i % 3) + [COMPLETE])
    cfn.add_stack("bad")
    cfn.script_detection("bad", status_progression=[FAILED], detection_status_reason="x")
    cfn.add_stack("busy", status="DELETE_IN_PROGRESS")
    names += ["bad", "busy"]

    tracker = ProgressTracker(clock)
    pool = BulkDetection(cfn, clock, _confirm(cfn, names), progress=tracker)
    operation = pool.operation
    assert operation is not None

    previous_completed = 0
    while not pool.tick():
        snap = tracker.observe(operation)
        assert snap.total == len(names)
        assert snap.completed + snap.remaining == snap.total
        assert snap.completed >= previous_completed
        # Stacks still in flight are never counted as completed.
        assert snap.remaining >= pool.in_flight_count
        previous_completed = snap.completed

    snap = tracker.observe(operation)
    assert snap.completed == len(names) and snap.remaining == 0
    assert [f.id for f in snap.failures] == ["bad"]
    assert tracker.is_complete(operation)

    summary = tracker.summary(operation)
    assert (summary.succeeded, summary.failed, summary.skipped) == (12, 1, 1)
    skipped = [i for i in summary.items if i.outcome is ItemOutcome.SKIPPED]
    assert [i.id for i in skipped] == ["busy"]


def test_completion_is_signalled_once_after_all_terminal(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    names = ["a", "b"]
    for name in names:
        cfn.add_stack(name)
    cfn.script_detection("a", status_progression=[IN_PROGRESS, IN_PROGRESS, COMPLETE])
    cfn.script_detection("b", status_progression=[COMPLETE])

    signals = []
    pool = BulkDetection(
        cfn, clock, _confirm(cfn, names), on_complete=signals.append
    )

    assert not pool.is_complete and pool.results is None
    pool.tick()  # b completes, a still in flight
    assert not pool.is_complete and signals == []

    results = pool.run()
    assert pool.is_complete
    assert signals == [results]
    assert pool.tick() is True  # further ticks are no-ops
    assert signals == [results]
    assert len(results.outcomes) == 2


def test_requires_confirmed_set(cfn: FakeCfnClient, clock: FakeClock) -> None:
    cfn.add_stack("a")
    resolved = StackSelector(cfn).resolve(Names(names=("a",)))
    with pytest.raises(TypeError):
        detect_all(cfn, clock, resolved)  # type: ignore[arg-type]
    assert cfn.call_count("detect_stack_drift") == 0
