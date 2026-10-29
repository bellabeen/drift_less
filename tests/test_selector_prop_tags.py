"""Property-based test for Stack_Selector tag-filter resolution (task 2.3).

Feature: multi-stack-drift-manager, Property 2: Tag filters match by exact case-sensitive AND semantics

**Validates: Requirements 1.2**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import TagFilter, Tags
from drift_manager.selector import EmptySelection, Resolved, StackSelector
from tests.fakes import FakeCfnClient

# Small alphabets with case variants so exact/case-sensitive matching is
# exercised (e.g. "Env" vs "env" vs "ENV") and matches actually occur.
KEYS = ["Env", "env", "ENV", "Team", "team", "app", "App"]
VALUES = ["prod", "Prod", "PROD", "dev", "Dev", "x", ""]

tag_maps = st.dictionaries(st.sampled_from(KEYS), st.sampled_from(VALUES), max_size=5)
stack_tag_lists = st.lists(tag_maps, min_size=0, max_size=30)
random_filter = st.builds(TagFilter, key=st.sampled_from(KEYS), value=st.sampled_from(VALUES))


@st.composite
def inventory_and_filters(draw):
    """Draw an inventory plus 1-500 filters, sometimes derived from a stack's tags."""
    tags_per_stack = draw(stack_tag_lists)
    pool: list[TagFilter] = []
    # Bias toward matches: optionally seed filters from an existing stack's tags.
    seeded = [m for m in tags_per_stack if m]
    if seeded and draw(st.booleans()):
        source = draw(st.sampled_from(seeded))
        pool = [TagFilter(key=k, value=v) for k, v in source.items()]
        filters = draw(
            st.lists(st.sampled_from(pool), min_size=1, max_size=500)
            | st.lists(random_filter, min_size=0, max_size=3).map(lambda extra: pool + extra)
        )
    else:
        filters = draw(st.lists(random_filter, min_size=1, max_size=500))
    return tags_per_stack, filters[:500]


def _expected_match(tags: dict[str, str], filters: list[TagFilter]) -> bool:
    return all(any(k == f.key and v == f.value for k, v in tags.items()) for f in filters)


# Feature: multi-stack-drift-manager, Property 2: Tag filters match by exact case-sensitive AND semantics
@settings(max_examples=100)
@given(data=inventory_and_filters())
def test_tag_filters_exact_case_sensitive_and_semantics(data):
    tags_per_stack, filters = data
    assert 1 <= len(filters) <= 500

    cfn = FakeCfnClient()
    for i, tags in enumerate(tags_per_stack):
        cfn.add_stack(f"stack-{i}", tags=tags)

    result = StackSelector(cfn).resolve(Tags(filters=tuple(filters)))

    expected = {
        f"stack-{i}" for i, tags in enumerate(tags_per_stack) if _expected_match(tags, filters)
    }

    if expected:
        assert isinstance(result, Resolved)
        assert {s.name for s in result.stacks} == expected
        assert result.unmatchedNames == ()
    else:
        assert isinstance(result, EmptySelection)
