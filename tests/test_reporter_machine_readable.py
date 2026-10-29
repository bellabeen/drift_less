"""Unit tests for the machine-readable structured report (task 7.2, Req 3.7)."""

from __future__ import annotations

import json

import pytest

from drift_manager.models import (
    ABSENT,
    Detected,
    DetectionResults,
    DifferenceType,
    DriftStatus,
    PropertyDifference,
    ResourceDrift,
    ResourceDriftStatus,
    Skipped,
    StackDetectionOutcome,
    Unknown,
)
from drift_manager.reporter import (
    from_json,
    from_machine_readable,
    report,
    to_json,
    to_machine_readable,
)


def _sample_report():
    bucket = ResourceDrift(
        logicalResourceId="Bucket",
        resourceType="AWS::S3::Bucket",
        resourceDriftStatus=ResourceDriftStatus.MODIFIED,
        physicalResourceId="my-bucket-123",
        propertyDifferences=(
            PropertyDifference("/Versioning", "Enabled", "Suspended", DifferenceType.NOT_EQUAL),
            PropertyDifference("/Tags/0", ABSENT, {"Key": "x"}, DifferenceType.ADD),
            PropertyDifference("/Cors", ["a"], ABSENT, DifferenceType.REMOVE),
            PropertyDifference("/Policy", "p", None, DifferenceType.NOT_EQUAL),
        ),
    )
    queue = ResourceDrift(
        logicalResourceId="Queue",
        resourceType="AWS::SQS::Queue",
        resourceDriftStatus=ResourceDriftStatus.DELETED,
    )
    results = DetectionResults(
        outcomes=(
            StackDetectionOutcome("drifted", Detected(DriftStatus.DRIFTED, (bucket, queue))),
            StackDetectionOutcome("synced", Detected(DriftStatus.IN_SYNC)),
            StackDetectionOutcome("failed", Unknown("timeout")),
            StackDetectionOutcome("skipped", Skipped("UPDATE_IN_PROGRESS")),
        )
    )
    return report(results)


def test_structured_form_exposes_stack_and_resource_fields():
    data = to_machine_readable(_sample_report())

    assert [s["stackName"] for s in data["stacks"]] == ["drifted", "synced", "failed", "skipped"]
    assert [s["driftStatus"] for s in data["stacks"]] == [
        "DRIFTED", "IN_SYNC", "UNKNOWN", "NOT_CHECKED",
    ]
    resources = data["stacks"][0]["driftedResources"]
    assert [(r["logicalResourceId"], r["resourceType"]) for r in resources] == [
        ("Bucket", "AWS::S3::Bucket"),
        ("Queue", "AWS::SQS::Queue"),
    ]
    assert resources[0]["propertyDifferences"][0] == {
        "propertyPath": "/Versioning",
        "differenceType": "NOT_EQUAL",
        "expectedValue": {"absent": False, "value": "Enabled"},
        "actualValue": {"absent": False, "value": "Suspended"},
    }
    for entry in data["stacks"][1:]:
        assert entry["driftedResources"] == []
    assert data["counts"] == {"DRIFTED": 1, "IN_SYNC": 1, "NOT_CHECKED": 1, "UNKNOWN": 1}


def test_absent_is_distinct_from_null_value():
    diffs = to_machine_readable(_sample_report())["stacks"][0]["driftedResources"][0][
        "propertyDifferences"
    ]
    assert diffs[1]["expectedValue"] == {"absent": True}
    assert diffs[2]["actualValue"] == {"absent": True}
    assert diffs[3]["actualValue"] == {"absent": False, "value": None}


def test_json_round_trip_reconstructs_report():
    original = _sample_report()
    text = to_json(original)
    json.loads(text)  # valid JSON
    parsed = from_json(text)
    assert parsed == original
    assert parsed.entries[0].driftedResources[0].propertyDifferences[1].expectedValue is ABSENT
    assert parsed.entries[0].driftedResources[0].propertyDifferences[3].actualValue is None


def test_empty_report_round_trips_with_message():
    empty = report(DetectionResults())
    data = to_machine_readable(empty)
    assert data["stacks"] == []
    assert data["emptyMessage"] == empty.emptyMessage
    assert from_machine_readable(data) == empty


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("stacks"),
        lambda d: d.update(schemaVersion=99),
        lambda d: d["stacks"][0]["driftedResources"][0]["propertyDifferences"][0].update(
            expectedValue={"value": 1}
        ),
    ],
)
def test_malformed_structured_report_is_rejected(mutate):
    data = json.loads(to_json(_sample_report()))
    mutate(data)
    with pytest.raises(ValueError):
        from_machine_readable(data)
