"""Unit tests for data model construction and the ABSENT marker (task 1.3).

**Validates: Requirements 3.5**
"""

import copy
import dataclasses
import pickle

import pytest

from drift_manager.models import (
    ABSENT,
    DifferenceType,
    DriftStatus,
    ItemOutcome,
    Names,
    OperationKind,
    OperationSummary,
    PropertyDifference,
    Region,
    ResourceDrift,
    ResourceDriftStatus,
    SummaryItem,
    TagFilter,
    Tags,
    _Absent,
)


class TestAbsentMarker:
    def test_absent_is_singleton(self):
        assert _Absent() is ABSENT
        assert copy.copy(ABSENT) is not None  # copy does not raise

    def test_absent_is_distinct_from_none_and_falsy_values(self):
        assert ABSENT is not None
        assert ABSENT != None  # noqa: E711
        assert ABSENT != ""
        assert ABSENT != 0
        assert ABSENT != False  # noqa: E712

    def test_absent_repr(self):
        assert repr(ABSENT) == "ABSENT"


class TestPropertyDifference:
    def test_add_only_marks_expected_absent(self):
        diff = PropertyDifference(
            propertyPath="/Tags/0",
            expectedValue=ABSENT,
            actualValue="prod",
            differenceType=DifferenceType.ADD,
        )
        assert diff.expectedValue is ABSENT
        assert diff.actualValue is not ABSENT
        assert diff.actualValue == "prod"
        assert diff.differenceType is DifferenceType.ADD

    def test_remove_only_marks_actual_absent(self):
        diff = PropertyDifference(
            propertyPath="/Timeout",
            expectedValue=30,
            actualValue=ABSENT,
            differenceType=DifferenceType.REMOVE,
        )
        assert diff.actualValue is ABSENT
        assert diff.expectedValue is not ABSENT
        assert diff.expectedValue == 30
        assert diff.differenceType is DifferenceType.REMOVE

    def test_not_equal_has_neither_value_absent(self):
        diff = PropertyDifference(
            propertyPath="/MemorySize",
            expectedValue=128,
            actualValue=256,
            differenceType=DifferenceType.NOT_EQUAL,
        )
        assert diff.expectedValue is not ABSENT
        assert diff.actualValue is not ABSENT
        assert (diff.expectedValue, diff.actualValue) == (128, 256)

    def test_none_value_is_not_confused_with_absent(self):
        # A genuine None value on one side is a present value, not absence.
        diff = PropertyDifference(
            propertyPath="/Description",
            expectedValue=None,
            actualValue="text",
            differenceType=DifferenceType.NOT_EQUAL,
        )
        assert diff.expectedValue is None
        assert diff.expectedValue is not ABSENT

    def test_exposes_property_identifier(self):
        diff = PropertyDifference("/A/B", 1, 2, DifferenceType.NOT_EQUAL)
        assert diff.propertyPath == "/A/B"

    def test_is_immutable(self):
        diff = PropertyDifference("/A", ABSENT, 1, DifferenceType.ADD)
        with pytest.raises(dataclasses.FrozenInstanceError):
            diff.actualValue = 2  # type: ignore[misc]

    def test_absent_survives_equality_and_copy(self):
        diff = PropertyDifference("/A", ABSENT, 1, DifferenceType.ADD)
        clone = copy.deepcopy(diff)
        assert clone == diff
        assert clone.expectedValue is ABSENT
        assert pickle.loads(pickle.dumps(diff)).expectedValue is ABSENT


class TestModelConstruction:
    def test_selection_criteria_variants(self):
        assert Names(("a", "b")).names == ("a", "b")
        assert Tags((TagFilter("env", "prod"),)).filters == (TagFilter("env", "prod"),)
        assert Region("us-east-1").region == "us-east-1"

    def test_resource_drift_defaults_and_differences(self):
        diff = PropertyDifference("/A", ABSENT, 1, DifferenceType.ADD)
        rd = ResourceDrift(
            logicalResourceId="Bucket",
            resourceType="AWS::S3::Bucket",
            resourceDriftStatus=ResourceDriftStatus.MODIFIED,
            propertyDifferences=(diff,),
        )
        assert rd.propertyDifferences == (diff,)
        assert rd.physicalResourceId is None
        empty = ResourceDrift("X", "AWS::SQS::Queue", ResourceDriftStatus.IN_SYNC)
        assert empty.propertyDifferences == ()

    def test_operation_summary_construction(self):
        items = (
            SummaryItem("s1", ItemOutcome.SUCCEEDED),
            SummaryItem("s2", ItemOutcome.FAILED, "boom"),
        )
        summary = OperationSummary(
            OperationKind.DETECTION, items, succeeded=1, failed=1, skipped=0
        )
        assert summary.succeeded + summary.failed + summary.skipped == len(items)
        assert items[0].reason is None
        assert items[1].reason == "boom"

    def test_drift_status_values(self):
        assert {s.value for s in DriftStatus} == {
            "DRIFTED",
            "IN_SYNC",
            "NOT_CHECKED",
            "UNKNOWN",
        }
        assert {d.value for d in DifferenceType} == {"ADD", "REMOVE", "NOT_EQUAL"}
