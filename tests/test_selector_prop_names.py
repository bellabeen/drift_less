"""Property test for explicit-name resolution in the Stack_Selector.

Feature: multi-stack-drift-manager, Property 1: Explicit names resolve to exact matches

**Validates: Requirements 1.1, 1.5**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import Names
from drift_manager.selector import EmptySelection, Resolved, StackSelector

from .fakes import FakeCfnClient

# A small alphabet keeps collisions between submitted names and inventory
# names frequent, and mixed case exercises exact (case-sensitive) matching.
_name = st.text(alphabet="abAB-1", min_size=1, max_size=4)


@st.composite
def inventory_and_names(draw):
    inventory = draw(st.lists(_name, max_size=30, unique=True))
    # Submitted names mix inventory names with arbitrary (likely unmatched) ones.
    candidate = st.sampled_from(inventory) | _name if inventory else _name
    submitted = draw(st.lists(candidate, min_size=1, max_size=500))
    return inventory, submitted


# Feature: multi-stack-drift-manager, Property 1: Explicit names resolve to exact matches
@settings(max_examples=100)
@given(data=inventory_and_names())
def test_explicit_names_resolve_to_exact_matches(data):
    inventory, submitted = data
    cfn = FakeCfnClient()
    for name in inventory:
        cfn.add_stack(name)

    result = StackSelector(cfn).resolve(Names(names=tuple(submitted)))

    expected = set(inventory) & set(submitted)
    expected_unmatched = set(submitted) - set(inventory)

    if not expected:
        # Zero matches yields an empty selection (req 1.8).
        assert isinstance(result, EmptySelection)
        return

    assert isinstance(result, Resolved)
    resolved_names = [s.name for s in result.stacks]
    # Exactly the inventory stacks whose name matches a submitted name, no dups.
    assert len(resolved_names) == len(set(resolved_names))
    assert set(resolved_names) == expected
    # Unmatched names are excluded and recorded (req 1.5), each once.
    assert len(result.unmatchedNames) == len(set(result.unmatchedNames))
    assert set(result.unmatchedNames) == expected_unmatched
