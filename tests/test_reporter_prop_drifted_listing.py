"""Property test for conditional drifted-resource listing in the consolidated report.

Feature: multi-stack-drift-manager, Property 12: Drifted resources are shown if and only if the stack is DRIFTED

**Validates: Requirements 3.3, 3.4**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import (
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
from drift_manager.reporter import report

# ---------------------------------------------------------------------------
# Local strategies
# ---------------------------------------------------------------------------

_ids = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122), min_size=1, max_size=12
)
_values = st.one_of(st.none(), st.integers(), st.text(max_size=8), st.booleans())

_differences = st.builds(
    PropertyDifference,
    propertyPath=_ids,
    expectedValue=_values,
    actualValue=_values,
    differenceType=st.sampled_from(list(DifferenceType)),
)

_resources = st.builds(
    ResourceDrift,
    logicalResourceId=_ids,
    resourceType=st.sampled_from(
        ["AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::IAM::Role", "AWS::EC2::Instance"]
    ),
    resourceDriftStatus=st.sampled_from(
        [ResourceDriftStatus.MODIFIED, ResourceDriftStatus.DELETED]
    ),
    propertyDifferences=st.lists(_differences, max_size=3).map(tuple),
    physicalResourceId=st.one_of(st.none(), _ids),
)

# Detected outcomes of any status may carry recorded drifted resources, so the
# report must actively suppress them for non-DRIFTED statuses.
_outcomes = st.one_of(
    st.builds(
        Detected,
        status=st.sampled_from(list(DriftStatus)),
        driftedResources=st.lists(_resources, max_size=4).map(tuple),
    ),
    st.builds(Unknown, reason=_ids),
    st.builds(Skipped, reason=_ids),
)

_results = st.lists(
    st.builds(StackDetectionOutcome, stackName=_ids, outcome=_outcomes),
    max_size=10,
).map(lambda xs: DetectionResults(outcomes=tuple(xs)))


def _identity(resource: ResourceDrift) -> tuple:
    return (resource.logicalResourceId, resource.resourceType)


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------


# Feature: multi-stack-drift-manager, Property 12: Drifted resources are shown if and only if the stack is DRIFTED
@settings(max_examples=100)
@given(results=_results)
def test_drifted_resources_listed_iff_status_drifted(results: DetectionResults) -> None:
    drift_report = report(results)

    assert len(drift_report.entries) == len(results.outcomes)
    for stack, entry in zip(results.outcomes, drift_report.entries):
        if entry.status is DriftStatus.DRIFTED:
            assert isinstance(stack.outcome, Detected)
            recorded = stack.outcome.driftedResources
            # Same resources, same order, each with identifier and type.
            assert [_identity(r) for r in entry.driftedResources] == [
                _identity(r) for r in recorded
            ]
            for r in entry.driftedResources:
                assert r.logicalResourceId
                assert r.resourceType
        else:
            assert entry.driftedResources == ()
