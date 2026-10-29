"""Property test for reconciliation confirmation gating and exactly-once coverage.

Feature: multi-stack-drift-manager, Property 18: Reconciliation is gated on confirmation and covers every scoped resource

**Validates: Requirements 4.4, 4.10**
"""

from __future__ import annotations

from collections import Counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import (
    ReconciliationAction,
    ResourceDrift,
    ResourceDriftStatus,
)
from drift_manager.reconciler import (
    IMPORT_CHANGE_SET_TYPE,
    ConfirmedScope,
    Reconciler,
    ScopedResource,
)
from tests.fakes import FakeCfnClient, FakeClock

RECONCILIATION_METHODS = ("update_stack", "create_change_set", "execute_change_set")

_ident = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
    min_size=1,
    max_size=8,
)


@st.composite
def scopes(draw) -> list[ScopedResource]:
    """Scopes spanning 1-4 stacks with 1-5 unique drifted resources each."""
    stack_names = draw(st.lists(_ident, min_size=1, max_size=4, unique=True))
    resources: list[ScopedResource] = []
    for stack in stack_names:
        logical_ids = draw(st.lists(_ident, min_size=1, max_size=5, unique=True))
        for logical_id in logical_ids:
            resources.append(
                ScopedResource(
                    stackName=f"stack-{stack}",
                    resource=ResourceDrift(
                        logicalResourceId=f"Res{logical_id}",
                        resourceType="AWS::S3::Bucket",
                        resourceDriftStatus=draw(
                            st.sampled_from(
                                [ResourceDriftStatus.MODIFIED, ResourceDriftStatus.DELETED]
                            )
                        ),
                        physicalResourceId=draw(st.none() | _ident),
                    ),
                )
            )
    return draw(st.permutations(resources))


def _reconciliation_calls(cfn: FakeCfnClient) -> int:
    return sum(cfn.call_count(m) for m in RECONCILIATION_METHODS)


# Feature: multi-stack-drift-manager, Property 18: Reconciliation is gated on confirmation and covers every scoped resource
@settings(max_examples=100)
@given(resources=scopes(), action=st.sampled_from(list(ReconciliationAction)))
def test_reconciliation_gated_and_exactly_once(resources, action):
    clock = FakeClock()
    cfn = FakeCfnClient(clock)
    for stack in {r.stackName for r in resources}:
        cfn.add_stack(stack)
    reconciler = Reconciler(cfn, clock)

    # --- Before confirmation: nothing may be applied. ----------------------
    scope = reconciler.set_scope(resources)
    empty = reconciler.confirm_scope(scope, [])  # RejectedEmpty
    invalidated = reconciler.confirm_scope(scope, resources[:-1] or [])
    unconfirmed = [scope, empty, invalidated, None, object(), list(resources)]
    if hasattr(invalidated, "updated"):
        unconfirmed.append(invalidated.updated)
    for candidate in unconfirmed:
        with pytest.raises((TypeError, ValueError)):
            reconciler.reconcile(candidate, action)

    # A ConfirmedScope cannot be forged.
    with pytest.raises(ValueError):
        ConfirmedScope(resources=tuple(resources))

    # A confirmation made stale by a later set_scope is rejected too.
    current = reconciler.set_scope(resources)
    stale = reconciler.confirm_scope(current, resources)
    assert isinstance(stale, ConfirmedScope)
    current = reconciler.set_scope(resources)
    with pytest.raises(ValueError):
        reconciler.reconcile(stale, action)

    assert _reconciliation_calls(cfn) == 0

    # --- Upon confirmation: exactly once per scoped resource. --------------
    confirmed = reconciler.confirm_scope(current, resources)
    assert isinstance(confirmed, ConfirmedScope)
    results = reconciler.reconcile(confirmed, action)

    expected_ids = sorted(r.resourceId for r in resources)
    assert sorted(o.resourceId for o in results.outcomes) == expected_ids
    assert results.total == len(resources)
    assert all(o.action is action for o in results.outcomes)

    per_stack = Counter(r.stackName for r in resources)
    if action is ReconciliationAction.ALIGN_TO_TEMPLATE:
        updates = cfn.calls_for("update_stack")
        assert Counter(c.kwargs["StackName"] for c in updates) == per_stack
        assert all(c.kwargs.get("UsePreviousTemplate") is True for c in updates)
        assert cfn.call_count("create_change_set") == 0
        assert cfn.call_count("execute_change_set") == 0
    else:
        assert cfn.call_count("update_stack") == 0
        creates = cfn.calls_for("create_change_set")
        imported = Counter()
        for call in creates:
            assert call.kwargs["ChangeSetType"] == IMPORT_CHANGE_SET_TYPE
            for entry in call.kwargs["ResourcesToImport"]:
                imported[f"{call.kwargs['StackName']}/{entry['LogicalResourceId']}"] += 1
        assert imported == Counter(expected_ids)

        # Each created change set is executed exactly once, for its own stack.
        created_ids = [
            f"arn:aws:cloudformation:::changeSet/{c.kwargs['ChangeSetName']}/{i:04d}"
            for i, c in enumerate(creates, start=1)
        ]
        executed = Counter(
            (c.kwargs["ChangeSetName"], c.kwargs.get("StackName"))
            for c in cfn.calls_for("execute_change_set")
        )
        assert executed == Counter(
            zip(created_ids, (c.kwargs["StackName"] for c in creates))
        )
