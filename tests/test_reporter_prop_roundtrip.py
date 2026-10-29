"""Property test for the machine-readable report round-trip.

Feature: multi-stack-drift-manager, Property 15: Machine-readable report round-trips report data

**Validates: Requirements 3.7**
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

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

# ---------------------------------------------------------------------------
# Local strategies
# ---------------------------------------------------------------------------

_text = st.text(min_size=1, max_size=12)

# JSON-compatible values, including genuine None and nested lists/dicts.
# NaN/inf are excluded: they are not valid JSON and NaN != NaN.
_json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53), max_value=2**53)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=10)
)
json_values = st.recursive(
    _json_scalars,
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(max_size=6), children, max_size=4),
    max_leaves=10,
)


@st.composite
def property_differences(draw) -> PropertyDifference:
    diff_type = draw(st.sampled_from(list(DifferenceType)))
    expected = ABSENT if diff_type is DifferenceType.ADD else draw(json_values)
    actual = ABSENT if diff_type is DifferenceType.REMOVE else draw(json_values)
    return PropertyDifference(
        propertyPath=draw(_text),
        expectedValue=expected,
        actualValue=actual,
        differenceType=diff_type,
    )


resource_drifts = st.builds(
    ResourceDrift,
    logicalResourceId=_text,
    resourceType=_text,
    resourceDriftStatus=st.sampled_from(
        [ResourceDriftStatus.MODIFIED, ResourceDriftStatus.DELETED]
    ),
    propertyDifferences=st.lists(property_differences(), max_size=4).map(tuple),
    physicalResourceId=st.none() | _text,
)

outcomes = (
    st.builds(
        Detected,
        status=st.just(DriftStatus.DRIFTED),
        driftedResources=st.lists(resource_drifts, max_size=4).map(tuple),
    )
    | st.builds(Detected, status=st.just(DriftStatus.IN_SYNC))
    | st.builds(Unknown, reason=_text)
    | st.builds(Skipped, reason=_text)
)


@st.composite
def detection_results(draw) -> DetectionResults:
    names = draw(st.lists(_text, unique=True, max_size=6))
    return DetectionResults(
        outcomes=tuple(
            StackDetectionOutcome(stackName=n, outcome=draw(outcomes)) for n in names
        )
    )


def _decode_raw(value: dict):
    """Interpret an encoded value using only plain dict access."""
    return ABSENT if value["absent"] is True else value["value"]


# ---------------------------------------------------------------------------
# Property 15
# ---------------------------------------------------------------------------


# Feature: multi-stack-drift-manager, Property 15: Machine-readable report round-trips report data
@settings(max_examples=100)
@given(results=detection_results())
def test_machine_readable_report_round_trips(results: DetectionResults) -> None:
    drift_report = report(results)

    # Round-trip through the structured dict and through JSON text.
    assert from_machine_readable(to_machine_readable(drift_report)) == drift_report
    text = to_json(drift_report)
    assert from_json(text) == drift_report

    # Parse with plain json.loads (no reporter helpers) and check fields directly.
    data = json.loads(text)
    assert len(data["stacks"]) == len(drift_report.entries)
    for raw_entry, entry in zip(data["stacks"], drift_report.entries):
        assert raw_entry["stackName"] == entry.stackName
        assert raw_entry["driftStatus"] == entry.status.value
        assert len(raw_entry["driftedResources"]) == len(entry.driftedResources)
        for raw_res, res in zip(raw_entry["driftedResources"], entry.driftedResources):
            assert raw_res["logicalResourceId"] == res.logicalResourceId
            assert raw_res["physicalResourceId"] == res.physicalResourceId
            assert raw_res["resourceType"] == res.resourceType
            assert raw_res["resourceDriftStatus"] == res.resourceDriftStatus.value
            assert len(raw_res["propertyDifferences"]) == len(res.propertyDifferences)
            for raw_diff, diff in zip(
                raw_res["propertyDifferences"], res.propertyDifferences
            ):
                assert raw_diff["propertyPath"] == diff.propertyPath
                assert raw_diff["differenceType"] == diff.differenceType.value
                for key, original in (
                    ("expectedValue", diff.expectedValue),
                    ("actualValue", diff.actualValue),
                ):
                    decoded = _decode_raw(raw_diff[key])
                    # ABSENT and a genuine None must stay distinguishable.
                    if original is ABSENT:
                        assert raw_diff[key] == {"absent": True}
                        assert decoded is ABSENT
                    else:
                        assert raw_diff[key]["absent"] is False
                        assert decoded is not ABSENT
                        assert decoded == original
                        if original is None:
                            assert decoded is None
