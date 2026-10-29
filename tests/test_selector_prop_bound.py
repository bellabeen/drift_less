"""Property test for the Stack_Selector 500-stack resolution bound (task 2.5).

Feature: multi-stack-drift-manager, Property 4: Resolution never exceeds 500 stacks

**Validates: Requirements 1.4**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import Names, Region, Tags, TagFilter
from drift_manager.selector import MAX_RESOLVED_STACKS, Resolved, StackSelector
from tests.fakes import FakeCfnClient

REGIONS = ("us-east-1", "us-west-2", "eu-west-1")
TAG = ("team", "core")


@st.composite
def inventory_and_criterion(draw):
    """Build an inventory (often >500 matching stacks) plus a criterion over it.

    Each stack is ``(region, tagged)``; indices name the stacks so generation
    stays cheap even for inventories well above the 500 bound.
    """
    size = draw(st.one_of(st.integers(0, 50), st.integers(450, 900)))
    # Bias heavily toward a single region / tagged so many stacks match.
    region_choice = st.sampled_from(REGIONS[:1] * 4 + REGIONS)
    stacks = draw(
        st.lists(
            st.tuples(region_choice, st.booleans() | st.just(True)),
            min_size=size,
            max_size=size,
        )
    )

    kind = draw(st.sampled_from(["names", "tags", "region"]))
    if kind == "names":
        # 1-500 names per the criterion model, mixing existing and unknown names.
        count = draw(st.integers(1, 500))
        indices = draw(
            st.lists(st.integers(0, max(size, 1) + 100), min_size=count, max_size=count)
        )
        criterion = Names(names=tuple(f"stack-{i}" for i in indices))
    elif kind == "tags":
        criterion = Tags(filters=(TagFilter(key=TAG[0], value=TAG[1]),))
    else:
        criterion = Region(region=draw(st.sampled_from(REGIONS)))
    return stacks, criterion


# Feature: multi-stack-drift-manager, Property 4: Resolution never exceeds 500 stacks
@settings(max_examples=100, deadline=None)
@given(inventory_and_criterion())
def test_resolution_never_exceeds_500_stacks(case):
    stacks, criterion = case
    cfn = FakeCfnClient()
    for i, (region, tagged) in enumerate(stacks):
        cfn.add_stack(
            f"stack-{i}", region=region, tags=dict([TAG]) if tagged else {}
        )

    result = StackSelector(cfn).resolve(criterion)

    if isinstance(result, Resolved):
        assert 1 <= len(result.stacks) <= MAX_RESOLVED_STACKS


def test_region_with_600_matching_stacks_is_bounded_to_500():
    cfn = FakeCfnClient()
    for i in range(600):
        cfn.add_stack(f"stack-{i}", region="us-east-1")

    result = StackSelector(cfn).resolve(Region(region="us-east-1"))

    assert isinstance(result, Resolved)
    assert len(result.stacks) == MAX_RESOLVED_STACKS
