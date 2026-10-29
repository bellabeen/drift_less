"""Unit test for the 5s detection-status poll cadence (task 5.8).

Requirement 2.2: while drift detection is in progress for a stack, the
Drift_Detector polls CloudFormation for the detection outcome every 5 seconds
and retrieves the outcome once CloudFormation reports completion.

Driven entirely by the FakeClock: every FakeCfnClient call is stamped with the
fake time, so the cadence is asserted from the recorded call timestamps.
"""

from __future__ import annotations

from drift_manager.detector import POLL_INTERVAL_SECONDS, detect_all, detect_stack
from drift_manager.models import Detected, DriftStatus, Names
from drift_manager.selector import Resolved, Stack, StackSelector

from tests.fakes import FakeCfnClient, FakeClock

IN_PROGRESS = "DETECTION_IN_PROGRESS"
COMPLETE = "DETECTION_COMPLETE"
STATUS_POLL = "describe_stack_drift_detection_status"
RESOURCE_DRIFTS = "describe_stack_resource_drifts"


def _drift_record(logical_id: str) -> dict:
    return {
        "LogicalResourceId": logical_id,
        "PhysicalResourceId": f"phys-{logical_id}",
        "ResourceType": "AWS::S3::Bucket",
        "StackResourceDriftStatus": "MODIFIED",
        "PropertyDifferences": [],
    }


def _script_slow_detection(cfn: FakeCfnClient, name: str, in_progress_polls: int) -> None:
    cfn.script_detection(
        name,
        status_progression=[IN_PROGRESS] * in_progress_polls + [COMPLETE],
        stack_drift_status="DRIFTED",
        resource_drift_pages=[[_drift_record("Bucket")]],
    )


def test_poll_interval_constant_is_five_seconds() -> None:
    assert POLL_INTERVAL_SECONDS == 5.0


def test_single_stack_polls_every_5s_until_complete_then_fetches_drifts(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    scripted = cfn.add_stack("s1")
    _script_slow_detection(cfn, "s1", in_progress_polls=4)
    stack = Stack(
        name="s1",
        region=scripted.region,
        status=scripted.status,
        stackId=scripted.stack_id,
    )

    result = detect_stack(cfn, clock, stack)

    # Initiated at t=0, then one status poll every 5s: 4 in-progress + 1 complete.
    assert cfn.times_for("detect_stack_drift") == [0.0]
    poll_times = cfn.times_for(STATUS_POLL)
    assert poll_times == [5.0, 10.0, 15.0, 20.0, 25.0]
    assert all(
        b - a == POLL_INTERVAL_SECONDS for a, b in zip(poll_times, poll_times[1:])
    )

    # Outcome retrieved once completion is reported: resource drifts are fetched
    # only after the completing poll, with no further status polling.
    assert cfn.times_for(RESOURCE_DRIFTS) == [25.0]
    methods = [c.method for c in cfn.calls]
    assert methods.index(RESOURCE_DRIFTS) > max(
        i for i, m in enumerate(methods) if m == STATUS_POLL
    )
    assert methods[-1] == RESOURCE_DRIFTS

    assert isinstance(result.outcome, Detected)
    assert result.outcome.status is DriftStatus.DRIFTED
    assert [r.logicalResourceId for r in result.outcome.driftedResources] == ["Bucket"]


def test_bulk_detection_polls_each_stack_every_5s_until_complete(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    cfn.add_stack("a")
    cfn.add_stack("b")
    _script_slow_detection(cfn, "a", in_progress_polls=1)
    _script_slow_detection(cfn, "b", in_progress_polls=3)

    selector = StackSelector(cfn)
    resolved = selector.resolve(Names(names=("a", "b")))
    assert isinstance(resolved, Resolved)
    results = detect_all(cfn, clock, selector.confirm(resolved))

    def poll_times_for(stack_name: str) -> list[float]:
        detection_ids = {
            c.kwargs["StackDriftDetectionId"]
            for c in cfn.calls_for(STATUS_POLL)
            if c.kwargs["StackDriftDetectionId"].endswith(f"-{stack_name}")
        }
        assert len(detection_ids) == 1
        return [
            c.time
            for c in cfn.calls_for(STATUS_POLL)
            if c.kwargs["StackDriftDetectionId"] in detection_ids
        ]

    def drift_fetch_times_for(stack_name: str) -> list[float]:
        return [
            c.time
            for c in cfn.calls_for(RESOURCE_DRIFTS)
            if c.kwargs["StackName"] == stack_name
        ]

    assert poll_times_for("a") == [5.0, 10.0]
    assert poll_times_for("b") == [5.0, 10.0, 15.0, 20.0]
    # Each stack's drifts are fetched at its completing poll, not before.
    assert drift_fetch_times_for("a") == [10.0]
    assert drift_fetch_times_for("b") == [20.0]

    assert len(results.outcomes) == 2
    assert all(isinstance(o.outcome, Detected) for o in results.outcomes)
