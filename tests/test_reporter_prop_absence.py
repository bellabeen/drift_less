"""Property test for property-difference absence markers (task 7.6).

Feature: multi-stack-drift-manager, Property 13: Property differences expose expected/actual with correct absence marker

For any drifted resource with property-level differences, each difference
exposes its property identifier, expected value, and actual value, where the
expected value is marked absent exactly when the difference is add-only and
the actual value is marked absent exactly when the difference is remove-only.

**Validates: Requirements 3.5**
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.detector import _to_property_difference, _to_resource_drift
from drift_manager.models import (
    ABSENT,
    Detected,
    DetectionResults,
    DifferenceType,
    DriftStatus,
    PropertyDifference,
    ResourceDrift,
    ResourceDriftStatus,
    StackDetectionOutcome,
)
from drift_manager.reporter import report

# ---------------------------------------------------------------------------
# Local strategies
# ---------------------------------------------------------------------------

# Concrete (non-absent) property values, including a genuine ``None``, which
# must never be confused with ABSENT.
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-1000, max_value=1000)
    | st.text(max_size=12),
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(st.text(max_size=6), children, max_size=3),
    max_leaves=6,
)

property_paths = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=20
).map(lambda s: "/" + s)

difference_types = st.sampled_from(list(DifferenceType))


@st.composite
def raw_cfn_differences(draw: st.DrawFn) -> dict[str, Any]:
    """A raw CloudFormation PropertyDifferences entry.

    CloudFormation may include or omit ExpectedValue/ActualValue on ADD/REMOVE
    records; both shapes are generated.
    """
    record: dict[str, Any] = {
        "PropertyPath": draw(property_paths),
        "DifferenceType": draw(difference_types).value,
    }
    if draw(st.booleans()) or record["DifferenceType"] == "NOT_EQUAL":
        record["ExpectedValue"] = draw(json_values)
    if draw(st.booleans()) or record["DifferenceType"] == "NOT_EQUAL":
        record["ActualValue"] = draw(json_values)
    return record


@st.composite
def model_differences(draw: st.DrawFn) -> PropertyDifference:
    """A PropertyDifference with concrete values on both sides (pre-normalization)."""
    return PropertyDifference(
        propertyPath=draw(property_paths),
        expectedValue=draw(json_values),
        actualValue=draw(json_values),
        differenceType=draw(difference_types),
    )


@st.composite
def drifted_resources(draw: st.DrawFn, diffs: st.SearchStrategy) -> ResourceDrift:
    return ResourceDrift(
        logicalResourceId=draw(st.text(min_size=1, max_size=10)),
        resourceType=draw(st.sampled_from(["AWS::S3::Bucket", "AWS::SQS::Queue"])),
        resourceDriftStatus=ResourceDriftStatus.MODIFIED,
        propertyDifferences=tuple(draw(st.lists(diffs, min_size=1, max_size=5))),
    )


def _results_for(resource_lists: list[list[ResourceDrift]]) -> DetectionResults:
    return DetectionResults(
        outcomes=tuple(
            StackDetectionOutcome(
                stackName=f"stack-{i}",
                outcome=Detected(DriftStatus.DRIFTED, tuple(resources)),
            )
            for i, resources in enumerate(resource_lists)
        )
    )


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def _assert_absence_markers(diff: PropertyDifference) -> None:
    assert isinstance(diff.propertyPath, str) and diff.propertyPath
    assert (diff.expectedValue is ABSENT) == (diff.differenceType is DifferenceType.ADD)
    assert (diff.actualValue is ABSENT) == (diff.differenceType is DifferenceType.REMOVE)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


# Feature: multi-stack-drift-manager, Property 13: Property differences expose expected/actual with correct absence marker
@settings(max_examples=100)
@given(st.lists(st.lists(drifted_resources(model_differences()), min_size=1, max_size=3), min_size=1, max_size=3))
def test_report_applies_absence_markers(resource_lists: list[list[ResourceDrift]]) -> None:
    """report() exposes each difference with ABSENT exactly for ADD/REMOVE sides."""
    results = _results_for(resource_lists)
    drift_report = report(results)

    for entry, resources in zip(drift_report.entries, resource_lists):
        assert len(entry.driftedResources) == len(resources)
        for out_res, in_res in zip(entry.driftedResources, resources):
            assert len(out_res.propertyDifferences) == len(in_res.propertyDifferences)
            for out_d, in_d in zip(out_res.propertyDifferences, in_res.propertyDifferences):
                _assert_absence_markers(out_d)
                # Identifier and type preserved; present sides carry the input value.
                assert out_d.propertyPath == in_d.propertyPath
                assert out_d.differenceType is in_d.differenceType
                if out_d.differenceType is not DifferenceType.ADD:
                    assert out_d.expectedValue == in_d.expectedValue
                if out_d.differenceType is not DifferenceType.REMOVE:
                    assert out_d.actualValue == in_d.actualValue


# Feature: multi-stack-drift-manager, Property 13: Property differences expose expected/actual with correct absence marker
@settings(max_examples=100)
@given(st.lists(st.lists(raw_cfn_differences(), min_size=1, max_size=5), min_size=1, max_size=3))
def test_detector_conversion_then_report_applies_absence_markers(
    raw_resources: list[list[dict[str, Any]]],
) -> None:
    """Raw CFN records -> detector conversion -> report() keep correct markers."""
    resources = [
        _to_resource_drift(
            {
                "LogicalResourceId": f"Res{i}",
                "ResourceType": "AWS::S3::Bucket",
                "StackResourceDriftStatus": "MODIFIED",
                "PropertyDifferences": raw_diffs,
            }
        )
        for i, raw_diffs in enumerate(raw_resources)
    ]
    drift_report = report(_results_for([resources]))

    (entry,) = drift_report.entries
    for out_res, raw_diffs in zip(entry.driftedResources, raw_resources):
        assert len(out_res.propertyDifferences) == len(raw_diffs)
        for out_d, raw in zip(out_res.propertyDifferences, raw_diffs):
            _assert_absence_markers(out_d)
            assert out_d.propertyPath == raw["PropertyPath"]
            assert out_d.differenceType.value == raw["DifferenceType"]
            if out_d.differenceType is not DifferenceType.ADD:
                assert out_d.expectedValue == raw.get("ExpectedValue")
            if out_d.differenceType is not DifferenceType.REMOVE:
                assert out_d.actualValue == raw.get("ActualValue")
            # Conversion alone already yields the same markers.
            assert _to_property_difference(raw) == out_d
