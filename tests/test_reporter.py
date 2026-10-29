"""Unit tests for consolidated report aggregation (task 7.1).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**
"""

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
    StatusCounts,
    Unknown,
)
from drift_manager.reporter import EMPTY_REPORT_MESSAGE, report


def _resource(rid="Bucket", diffs=()):
    return ResourceDrift(
        logicalResourceId=rid,
        resourceType="AWS::S3::Bucket",
        resourceDriftStatus=ResourceDriftStatus.MODIFIED,
        propertyDifferences=tuple(diffs),
    )


def test_empty_results_produce_empty_report_with_message():
    r = report(DetectionResults())
    assert r.entries == ()
    assert r.counts == StatusCounts()
    assert r.emptyMessage == EMPTY_REPORT_MESSAGE


def test_mixed_outcomes_map_status_and_counts():
    drifted = _resource()
    results = DetectionResults(
        outcomes=(
            StackDetectionOutcome("a", Detected(DriftStatus.DRIFTED, (drifted,))),
            StackDetectionOutcome("b", Detected(DriftStatus.IN_SYNC)),
            StackDetectionOutcome("c", Unknown("timeout")),
            StackDetectionOutcome("d", Skipped("UPDATE_IN_PROGRESS")),
        )
    )
    r = report(results)
    assert [(e.stackName, e.status) for e in r.entries] == [
        ("a", DriftStatus.DRIFTED),
        ("b", DriftStatus.IN_SYNC),
        ("c", DriftStatus.UNKNOWN),
        ("d", DriftStatus.NOT_CHECKED),
    ]
    assert r.entries[0].driftedResources == (drifted,)
    assert all(e.driftedResources == () for e in r.entries[1:])
    assert r.counts == StatusCounts(DRIFTED=1, IN_SYNC=1, NOT_CHECKED=1, UNKNOWN=1)
    assert r.emptyMessage is None


def test_non_drifted_status_lists_no_resources_even_if_recorded():
    results = DetectionResults(
        outcomes=(StackDetectionOutcome("a", Detected(DriftStatus.IN_SYNC, (_resource(),))),)
    )
    assert report(results).entries[0].driftedResources == ()


def test_property_differences_carry_absent_marker():
    diffs = (
        PropertyDifference("/Tags/0", None, "x", DifferenceType.ADD),
        PropertyDifference("/Versioning", "Enabled", None, DifferenceType.REMOVE),
        PropertyDifference("/Name", "old", "new", DifferenceType.NOT_EQUAL),
    )
    results = DetectionResults(
        outcomes=(
            StackDetectionOutcome(
                "a", Detected(DriftStatus.DRIFTED, (_resource(diffs=diffs),))
            ),
        )
    )
    out = report(results).entries[0].driftedResources[0].propertyDifferences
    assert out[0].expectedValue is ABSENT and out[0].actualValue == "x"
    assert out[1].expectedValue == "Enabled" and out[1].actualValue is ABSENT
    assert (out[2].expectedValue, out[2].actualValue) == ("old", "new")
