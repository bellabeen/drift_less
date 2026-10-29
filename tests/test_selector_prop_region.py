"""Property test for Stack_Selector region resolution (task 2.4).

Feature: multi-stack-drift-manager, Property 3: Region criterion resolves to all stacks in the region

**Validates: Requirements 1.3**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import Region
from drift_manager.selector import EmptySelection, Resolved, StackSelector
from tests.fakes import FakeCfnClient

REGIONS = [
    "us-east-1",
    "us-west-2",
    "eu-west-1",
    "eu-central-1",
    "ap-southeast-2",
]

stack_names = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-"),
    min_size=1,
    max_size=20,
)

# Inventory: unique stack names, each placed in one of several regions.
# Kept well under the 500 bound so the bound never truncates the expected set.
inventories = st.dictionaries(
    keys=stack_names,
    values=st.sampled_from(REGIONS),
    max_size=40,
)


# Feature: multi-stack-drift-manager, Property 3: Region criterion resolves to all stacks in the region
@settings(max_examples=100)
@given(inventory=inventories, target=st.sampled_from(REGIONS))
def test_region_criterion_resolves_to_all_stacks_in_region(
    inventory: dict[str, str], target: str
) -> None:
    cfn = FakeCfnClient()
    for name, region in inventory.items():
        cfn.add_stack(name, region=region)

    result = StackSelector(cfn).resolve(Region(region=target))

    expected = {name for name, region in inventory.items() if region == target}

    if not expected:
        assert isinstance(result, EmptySelection)
        return

    assert isinstance(result, Resolved)
    resolved_names = [s.name for s in result.stacks]
    # No duplicates, and exactly the inventory stacks located in the region.
    assert len(resolved_names) == len(set(resolved_names))
    assert set(resolved_names) == expected
    assert all(s.region == target for s in result.stacks)
    assert result.unmatchedNames == ()
