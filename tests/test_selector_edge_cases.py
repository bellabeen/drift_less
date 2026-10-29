"""Unit tests for Stack_Selector edge cases (task 2.7).

Requirements: 1.6, 1.7, 1.8, 1.10.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from drift_manager.detector import BulkDetection
from drift_manager.models import Detected, Names, Region, TagFilter, Tags
from drift_manager.selector import (
    ConfirmedSet,
    EmptySelection,
    FailedSelection,
    RejectedSelection,
    Resolved,
    StackSelector,
)

from tests.fakes import FakeCfnClient, FakeClock


class RegionErrorCfnClient(FakeCfnClient):
    """Fake client that rejects one region, as boto3 would for a bad/denied region."""

    def __init__(self, bad_region: str, clock: Optional[FakeClock] = None) -> None:
        super().__init__(clock=clock)
        self._bad_region = bad_region

    def _check(self, kwargs: dict[str, Any]) -> None:
        if kwargs.get("Region") == self._bad_region:
            raise RuntimeError(f"could not connect to region {self._bad_region}")

    def list_stacks(self, **kwargs: Any) -> dict[str, Any]:
        self._check(kwargs)
        return super().list_stacks(**kwargs)

    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        self._check(kwargs)
        return super().describe_stacks(**kwargs)


# -- 1.6: zero names and zero filters are rejected ---------------------------


@pytest.mark.parametrize("criterion", [Names(names=()), Tags(filters=())])
def test_empty_criterion_is_rejected_with_message(
    cfn: FakeCfnClient, criterion
) -> None:
    cfn.add_stack("s1", tags={"env": "prod"})

    result = StackSelector(cfn).resolve(criterion)

    assert isinstance(result, RejectedSelection)
    assert result.message
    # Validation precedes resolution: no inventory lookups happen.
    assert cfn.call_count() == 0


# -- 1.7: invalid / inaccessible region fails with a reason ------------------


def test_invalid_region_returns_failed_selection_with_reason(clock: FakeClock) -> None:
    cfn = RegionErrorCfnClient("xx-nowhere-1", clock=clock)
    cfn.add_stack("s1", region="us-east-1")

    result = StackSelector(cfn).resolve(Region(region="xx-nowhere-1"))

    assert isinstance(result, FailedSelection)
    assert "xx-nowhere-1" in result.reason
    assert "could not connect" in result.reason


# -- 1.8: zero matches yield EmptySelection ----------------------------------


@pytest.mark.parametrize(
    "criterion",
    [
        Names(names=("missing",)),
        Tags(filters=(TagFilter(key="env", value="Prod"),)),  # case-sensitive
        Region(region="eu-west-1"),
    ],
)
def test_zero_matches_returns_empty_selection(cfn: FakeCfnClient, criterion) -> None:
    cfn.add_stack("s1", region="us-east-1", tags={"env": "prod"})

    result = StackSelector(cfn).resolve(criterion)

    assert isinstance(result, EmptySelection)
    assert result.message


# -- 1.10: confirming allows detection on exactly the resolved set ------------


def test_confirm_allows_detection_on_resolved_set(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    cfn.add_stack("a", tags={"team": "x"})
    cfn.add_stack("b", tags={"team": "x"})
    cfn.add_stack("c", tags={"team": "y"})
    selector = StackSelector(cfn)

    resolved = selector.resolve(Tags(filters=(TagFilter(key="team", value="x"),)))
    assert isinstance(resolved, Resolved)
    # Resolution alone initiates no detection.
    assert cfn.call_count("detect_stack_drift") == 0

    confirmed = selector.confirm(resolved)
    assert isinstance(confirmed, ConfirmedSet)
    assert confirmed.stacks == resolved.stacks

    results = BulkDetection(cfn, clock, confirmed).run()

    detected = sorted(c.kwargs["StackName"] for c in cfn.calls_for("detect_stack_drift"))
    assert detected == ["a", "b"]
    assert sorted(o.stackName for o in results.outcomes) == ["a", "b"]
    assert all(isinstance(o.outcome, Detected) for o in results.outcomes)


def test_detection_refuses_unconfirmed_resolved_set(
    cfn: FakeCfnClient, clock: FakeClock
) -> None:
    cfn.add_stack("a")
    resolved = StackSelector(cfn).resolve(Names(names=("a",)))
    assert isinstance(resolved, Resolved)

    with pytest.raises(TypeError):
        BulkDetection(cfn, clock, resolved)  # type: ignore[arg-type]
    assert cfn.call_count("detect_stack_drift") == 0
