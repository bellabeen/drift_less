"""Unit tests for the per-stack detection state machine (task 5.1).

Requirements: 2.2, 2.3, 2.4, 2.5, 2.6.
"""

from __future__ import annotations

from drift_manager.detector import (
    DetectionState,
    StackDetection,
    detect_stack,
)
from drift_manager.models import (
    ABSENT,
    Detected,
    DifferenceType,
    DriftStatus,
    ResourceDriftStatus,
    Skipped,
    Unknown,
)
from drift_manager.selector import Stack

from tests.fakes import FakeCfnClient, FakeClock

IN_PROGRESS = "DETECTION_IN_PROGRESS"
COMPLETE = "DETECTION_COMPLETE"
FAILED = "DETECTION_FAILED"


def _stack(cfn: FakeCfnClient, name: str = "s1", status: str = "CREATE_COMPLETE") -> Stack:
    scripted = cfn.add_stack(name, status=status)
    return Stack(
        name=name,
        region=scripted.region,
        status=status,
        stackId=scripted.stack_id,
    )


def _drift_record(logical_id: str, status: str = "MODIFIED", diffs=None) -> dict:
    return {
        "LogicalResourceId": logical_id,
        "PhysicalResourceId": f"phys-{logical_id}",
        "ResourceType": "AWS::S3::Bucket",
        "StackResourceDriftStatus": status,
        "PropertyDifferences": diffs or [],
    }


def test_completed_records_status_and_paginated_drifted_resources(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_detection(
        "s1",
        status_progression=[IN_PROGRESS, COMPLETE],
        stack_drift_status="DRIFTED",
        resource_drift_pages=[
            [
                _drift_record(
                    "A",
                    diffs=[
                        {
                            "PropertyPath": "/Tags/0",
                            "ActualValue": "x",
                            "DifferenceType": "ADD",
                        },
                        {
                            "PropertyPath": "/Versioning",
                            "ExpectedValue": "on",
                            "DifferenceType": "REMOVE",
                        },
                    ],
                ),
                _drift_record("B", status="IN_SYNC"),
            ],
            [_drift_record("C", status="DELETED")],
        ],
    )

    result = detect_stack(cfn, clock, stack)

    assert isinstance(result.outcome, Detected)
    assert result.stackName == "s1"
    assert result.stackId == stack.stackId
    assert result.outcome.status is DriftStatus.DRIFTED
    # Both pages fetched; only drifted resources recorded.
    assert cfn.call_count("describe_stack_resource_drifts") == 2
    ids = [r.logicalResourceId for r in result.outcome.driftedResources]
    assert ids == ["A", "C"]
    assert result.outcome.driftedResources[1].resourceDriftStatus is ResourceDriftStatus.DELETED
    add, remove = result.outcome.driftedResources[0].propertyDifferences
    assert add.differenceType is DifferenceType.ADD and add.expectedValue is ABSENT
    assert add.actualValue == "x"
    assert remove.actualValue is ABSENT and remove.expectedValue == "on"


def test_polls_every_five_seconds_until_completion(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_detection(
        "s1", status_progression=[IN_PROGRESS, IN_PROGRESS, COMPLETE]
    )

    result = detect_stack(cfn, clock, stack)

    assert isinstance(result.outcome, Detected)
    assert result.outcome.status is DriftStatus.IN_SYNC
    assert cfn.times_for("detect_stack_drift") == [0.0]
    assert cfn.times_for("describe_stack_drift_detection_status") == [5.0, 10.0, 15.0]


def test_detection_failed_records_unknown_with_reason(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_detection(
        "s1",
        status_progression=[IN_PROGRESS, FAILED],
        detection_status_reason="boom",
    )

    result = detect_stack(cfn, clock, stack)

    assert isinstance(result.outcome, Unknown)
    assert "boom" in result.outcome.reason
    assert cfn.call_count("describe_stack_resource_drifts") == 0


def test_initiation_error_records_unknown_without_polling(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_detection("s1", initiation_error=RuntimeError("throttled"))

    machine = StackDetection(cfn, clock, stack)
    result = machine.run()

    assert machine.state is DetectionState.UNKNOWN
    assert isinstance(result.outcome, Unknown)
    assert "throttled" in result.outcome.reason
    assert cfn.call_count("describe_stack_drift_detection_status") == 0


def test_timeout_records_unknown_and_stops_polling(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn)
    cfn.script_detection("s1", status_progression=[IN_PROGRESS])

    machine = StackDetection(cfn, clock, stack)
    result = machine.run()

    assert isinstance(result.outcome, Unknown)
    assert "timeout" in result.outcome.reason
    assert clock.monotonic() == 300.0
    polls = cfn.call_count("describe_stack_drift_detection_status")
    assert polls == 60

    # Terminal state is final: stepping again makes no further calls.
    assert machine.step() is DetectionState.UNKNOWN
    assert cfn.call_count("describe_stack_drift_detection_status") == polls
    assert cfn.call_count("detect_stack_drift") == 1


def test_disallowed_stack_state_is_skipped_without_initiation(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    stack = _stack(cfn, status="UPDATE_IN_PROGRESS")

    machine = StackDetection(cfn, clock, stack)
    result = machine.run()

    assert machine.state is DetectionState.SKIPPED
    assert isinstance(result.outcome, Skipped)
    assert "UPDATE_IN_PROGRESS" in result.outcome.reason
    assert cfn.call_count() == 0


def test_completed_state_is_final(cfn: FakeCfnClient, clock: FakeClock) -> None:
    stack = _stack(cfn)
    cfn.script_detection("s1", stack_drift_status="IN_SYNC")

    machine = StackDetection(cfn, clock, stack)
    assert machine.state is DetectionState.PENDING
    assert machine.outcome is None
    assert machine.step() is DetectionState.DETECTING
    assert machine.step() is DetectionState.COMPLETED
    calls = cfn.call_count()

    assert machine.step() is DetectionState.COMPLETED
    assert cfn.call_count() == calls
