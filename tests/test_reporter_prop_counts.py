"""Property test for status-count conservation in the consolidated report.

Feature: multi-stack-drift-manager, Property 14: Status counts conserve the stack total
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import (
    Detected,
    DetectionResults,
    DriftStatus,
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

_stack_names = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-"),
    min_size=1,
    max_size=20,
)

_resources = st.builds(
    ResourceDrift,
    logicalResourceId=st.text(min_size=1, max_size=10),
    resourceType=st.sampled_from(["AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::IAM::Role"]),
    resourceDriftStatus=st.sampled_from(list(ResourceDriftStatus)),
)

_detected = st.builds(
    Detected,
    status=st.sampled_from(list(DriftStatus)),
    driftedResources=st.lists(_resources, max_size=3).map(tuple),
)

_outcomes = st.one_of(
    _detected,
    st.builds(Unknown, reason=st.text(max_size=20)),
    st.builds(Skipped, reason=st.text(max_size=20)),
)


@st.composite
def _detection_results(draw) -> DetectionResults:
    """A resolved set: unique stack names, one outcome each (possibly empty)."""
    names = draw(st.lists(_stack_names, unique=True, max_size=25))
    return DetectionResults(
        outcomes=tuple(
            StackDetectionOutcome(stackName=name, outcome=draw(_outcomes))
            for name in names
        )
    )


def _expected_status(outcome) -> DriftStatus:
    """Independent oracle for the outcome → status mapping."""
    if isinstance(outcome, Unknown):
        return DriftStatus.UNKNOWN
    if isinstance(outcome, Skipped):
        return DriftStatus.NOT_CHECKED
    return outcome.status


# ---------------------------------------------------------------------------
# Property 14
# ---------------------------------------------------------------------------


# Feature: multi-stack-drift-manager, Property 14: Status counts conserve the stack total
@settings(max_examples=100)
@given(results=_detection_results())
def test_status_counts_conserve_stack_total(results: DetectionResults) -> None:
    """**Validates: Requirements 3.6**"""
    drift_report = report(results)
    counts = drift_report.counts

    # Expected counts computed from the input outcomes, not from report entries.
    expected = {status: 0 for status in DriftStatus}
    for stack in results.outcomes:
        expected[_expected_status(stack.outcome)] += 1

    assert counts.DRIFTED == expected[DriftStatus.DRIFTED]
    assert counts.IN_SYNC == expected[DriftStatus.IN_SYNC]
    assert counts.NOT_CHECKED == expected[DriftStatus.NOT_CHECKED]
    assert counts.UNKNOWN == expected[DriftStatus.UNKNOWN]

    total = counts.DRIFTED + counts.IN_SYNC + counts.NOT_CHECKED + counts.UNKNOWN
    assert total == len(results.outcomes)
