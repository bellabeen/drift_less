"""Property test for the consolidated report and detection summary (task 7.4).

Feature: multi-stack-drift-manager, Property 11: Report is a bijection over the resolved set

For any detection results over a resolved set, the multiset of stacks in the
report (and in the detection operation summary) equals the multiset of stacks
in the resolved set: every stack appears exactly once, none omitted or
duplicated, each paired with its final drift status.

**Validates: Requirements 3.1, 6.1**
"""

from __future__ import annotations

from collections import Counter

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
from drift_manager.reporter import outcome_status, report
from drift_manager.summary import Produced, summarize_detection

# ---------------------------------------------------------------------------
# Strategies (local to this file)
# ---------------------------------------------------------------------------

stack_names = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-"),
    min_size=1,
    max_size=20,
)
reasons = st.text(max_size=30)


@st.composite
def property_differences(draw) -> PropertyDifference:
    kind = draw(st.sampled_from(list(DifferenceType)))
    value = st.one_of(st.none(), st.integers(), st.text(max_size=10))
    expected = ABSENT if kind is DifferenceType.ADD else draw(value)
    actual = ABSENT if kind is DifferenceType.REMOVE else draw(value)
    return PropertyDifference(
        propertyPath=draw(st.text(min_size=1, max_size=15)),
        expectedValue=expected,
        actualValue=actual,
        differenceType=kind,
    )


resource_drifts = st.builds(
    ResourceDrift,
    logicalResourceId=st.text(min_size=1, max_size=15),
    resourceType=st.sampled_from(["AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::IAM::Role"]),
    resourceDriftStatus=st.sampled_from(
        [ResourceDriftStatus.MODIFIED, ResourceDriftStatus.DELETED]
    ),
    propertyDifferences=st.lists(property_differences(), max_size=3).map(tuple),
    physicalResourceId=st.one_of(st.none(), st.text(min_size=1, max_size=15)),
)

detected = st.one_of(
    st.builds(
        Detected,
        status=st.just(DriftStatus.DRIFTED),
        driftedResources=st.lists(resource_drifts, min_size=1, max_size=3).map(tuple),
    ),
    st.builds(
        Detected,
        status=st.sampled_from(
            [DriftStatus.IN_SYNC, DriftStatus.NOT_CHECKED, DriftStatus.UNKNOWN]
        ),
    ),
)
outcomes = st.one_of(
    detected,
    st.builds(Unknown, reason=reasons),
    st.builds(Skipped, reason=reasons),
)


@st.composite
def detection_results(draw) -> DetectionResults:
    """Detection results over a resolved set: one outcome per distinct stack."""
    names = draw(st.lists(stack_names, unique=True, max_size=25))
    return DetectionResults(
        outcomes=tuple(
            StackDetectionOutcome(stackName=name, outcome=draw(outcomes)) for name in names
        )
    )


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------


# Feature: multi-stack-drift-manager, Property 11: Report is a bijection over the resolved set
@settings(max_examples=100)
@given(results=detection_results())
def test_report_and_summary_are_bijection_over_resolved_set(results: DetectionResults) -> None:
    """**Validates: Requirements 3.1, 6.1**"""
    expected = Counter(
        (o.stackName, outcome_status(o.outcome)) for o in results.outcomes
    )
    resolved_names = Counter(o.stackName for o in results.outcomes)

    # Consolidated report: every stack exactly once with its final status.
    drift_report = report(results)
    report_pairs = Counter((e.stackName, e.status) for e in drift_report.entries)
    assert report_pairs == expected
    assert Counter(e.stackName for e in drift_report.entries) == resolved_names

    # Detection operation summary: same bijection, same final statuses.
    produced = summarize_detection(results)
    assert isinstance(produced, Produced)
    summary_pairs = Counter((i.id, i.driftStatus) for i in produced.summary.items)
    assert summary_pairs == expected
    assert Counter(i.id for i in produced.summary.items) == resolved_names
