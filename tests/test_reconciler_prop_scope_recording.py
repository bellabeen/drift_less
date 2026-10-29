"""Property test for reconciliation scope recording (Property 16).

Feature: multi-stack-drift-manager, Property 16: Selected resources are recorded as the reconciliation scope

**Validates: Requirements 4.1**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import ResourceDrift, ResourceDriftStatus
from drift_manager.reconciler import (
    MAX_SCOPE_RESOURCES,
    Reconciler,
    ResourceKey,
    Scope,
    ScopedResource,
)

_DRIFTED = (ResourceDriftStatus.MODIFIED, ResourceDriftStatus.DELETED)
_TYPES = ("AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::Lambda::Function")


@st.composite
def selections(draw):
    """Draw (selection_with_duplicates, expected_unique_selection).

    Builds 1-500 distinct-keyed resources spread over several stacks (keys are
    derived from indices so generation stays cheap at large sizes), shuffles
    them, then injects duplicates (same key, different payload) *after* the
    first occurrence so the expected result is the de-duplicated original.
    """
    size = draw(st.integers(min_value=1, max_value=MAX_SCOPE_RESOURCES))
    num_stacks = draw(st.integers(min_value=1, max_value=8))
    rnd = draw(st.randoms(use_true_random=False))

    unique = []
    for i in range(size):
        stack = f"stack-{rnd.randrange(num_stacks)}"
        unique.append(
            ScopedResource(
                stackName=stack,
                resource=ResourceDrift(
                    logicalResourceId=f"Res{i}",
                    resourceType=rnd.choice(_TYPES),
                    resourceDriftStatus=rnd.choice(_DRIFTED),
                    physicalResourceId=f"phys-{i}",
                ),
            )
        )
    rnd.shuffle(unique)

    selection = list(unique)
    num_dups = draw(st.integers(min_value=0, max_value=min(20, size)))
    for _ in range(num_dups):
        src_pos = rnd.randrange(size)
        original = unique[src_pos]
        dup = ScopedResource(
            stackName=original.stackName,
            resource=ResourceDrift(
                logicalResourceId=original.resource.logicalResourceId,
                resourceType="AWS::Duplicate::Marker",
                resourceDriftStatus=ResourceDriftStatus.MODIFIED,
                physicalResourceId="duplicate",
            ),
        )
        # Insert strictly after the first occurrence of the original.
        first = next(
            idx for idx, item in enumerate(selection) if item.key == original.key
        )
        selection.insert(rnd.randint(first + 1, len(selection)), dup)

    return selection, unique


# Feature: multi-stack-drift-manager, Property 16: Selected resources are recorded as the reconciliation scope
@settings(max_examples=100, deadline=None)
@given(selections())
def test_selected_resources_are_recorded_as_scope(data):
    selection, expected = data
    rc = Reconciler()

    scope = rc.set_scope(selection)

    assert isinstance(scope, Scope)
    # Exactly the selected resources: same items, same order, duplicates collapsed
    # keeping the first occurrence.
    assert scope.resources == tuple(expected)
    assert len(scope) == len(expected)
    assert scope.keys == frozenset(
        ResourceKey(r.stackName, r.resource.logicalResourceId) for r in selection
    )
    # The recorded scope is the one returned.
    assert rc.scope is scope
