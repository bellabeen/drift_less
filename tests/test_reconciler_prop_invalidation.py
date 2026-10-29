"""Property test for scope invalidation on selection change (task 8.4).

Feature: multi-stack-drift-manager, Property 17: Changing the selection invalidates the recorded scope

For any recorded reconciliation scope, if the set of selected drifted resources
changes before confirmation, the scope is invalidated and no reconciliation
action is applied until the updated scope is re-confirmed.

**Validates: Requirements 4.3**
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import (
    ReconciliationAction,
    ResourceDrift,
    ResourceDriftStatus,
    Succeeded,
)
from drift_manager.reconciler import (
    ConfirmedScope,
    Reconciler,
    ResourceKey,
    Scope,
    ScopedResource,
    ScopeInvalidated,
)
from tests.fakes import FakeCfnClient, FakeClock

#: CloudFormation methods that apply a reconciliation action.
RECON_METHODS = ("update_stack", "create_change_set", "execute_change_set")

# ---------------------------------------------------------------------------
# Local strategies
# ---------------------------------------------------------------------------

_stack_names = st.sampled_from(["stack-a", "stack-b", "stack-c"])
_logical_ids = st.from_regex(r"[A-Z][A-Za-z0-9]{0,7}", fullmatch=True)
_keys = st.tuples(_stack_names, _logical_ids)
_drifted_status = st.sampled_from(
    [ResourceDriftStatus.MODIFIED, ResourceDriftStatus.DELETED]
)


@st.composite
def _selection_change(draw):
    """Draw (initial, changed) non-empty key sets whose key sets differ."""
    initial = draw(st.sets(_keys, min_size=1, max_size=8))
    mode = draw(st.sampled_from(["add", "remove", "replace"]))
    if mode == "remove" and len(initial) < 2:
        mode = draw(st.sampled_from(["add", "replace"]))

    ordered = sorted(initial)
    if mode == "add":
        extra = draw(
            st.sets(_keys.filter(lambda k: k not in initial), min_size=1, max_size=4)
        )
        changed = set(initial) | extra
    elif mode == "remove":
        removed = draw(
            st.sets(st.sampled_from(ordered), min_size=1, max_size=len(ordered) - 1)
        )
        changed = set(initial) - removed
    else:  # replace: drop at least one, add at least one new
        removed = draw(
            st.sets(st.sampled_from(ordered), min_size=1, max_size=len(ordered))
        )
        extra = draw(
            st.sets(_keys.filter(lambda k: k not in initial), min_size=1, max_size=4)
        )
        changed = (set(initial) - removed) | extra

    assert changed and changed != initial
    return mode, initial, changed


def _scoped(keys, status_for):
    return [
        ScopedResource(
            stackName=stack,
            resource=ResourceDrift(
                logicalResourceId=logical,
                resourceType="AWS::S3::Bucket",
                resourceDriftStatus=status_for(stack, logical),
                physicalResourceId=f"{stack}-{logical}".lower(),
            ),
        )
        for stack, logical in sorted(keys)
    ]


def _recon_call_count(cfn: FakeCfnClient) -> int:
    return sum(cfn.call_count(m) for m in RECON_METHODS)


# ---------------------------------------------------------------------------
# Property 17
# ---------------------------------------------------------------------------


# Feature: multi-stack-drift-manager, Property 17: Changing the selection invalidates the recorded scope
@settings(max_examples=100, deadline=None)
@given(
    change=_selection_change(),
    action=st.sampled_from(list(ReconciliationAction)),
    statuses=st.dictionaries(_keys, _drifted_status),
    confirm_first=st.booleans(),
)
def test_changing_selection_invalidates_recorded_scope(
    change, action, statuses, confirm_first
):
    _mode, initial_keys, changed_keys = change
    status_for = lambda s, l: statuses.get((s, l), ResourceDriftStatus.MODIFIED)
    initial_sel = _scoped(initial_keys, status_for)
    changed_sel = _scoped(changed_keys, status_for)

    clock = FakeClock()
    cfn = FakeCfnClient(clock)
    for stack in {k[0] for k in initial_keys | changed_keys}:
        cfn.add_stack(stack)
    reconciler = Reconciler(cfn, clock)

    scope = reconciler.set_scope(initial_sel)
    assert scope.keys == {ResourceKey(*k) for k in initial_keys}

    # Optionally hold an earlier confirmation of the original scope; the change
    # must make it stale too.
    earlier = None
    if confirm_first:
        earlier = reconciler.confirm_scope(scope, initial_sel)
        assert isinstance(earlier, ConfirmedScope)

    # The selection changes before (re-)confirmation -> invalidated.
    result = reconciler.confirm_scope(scope, changed_sel)
    assert isinstance(result, ScopeInvalidated)
    assert result.previous is scope
    assert isinstance(result.updated, Scope)
    assert result.updated.keys == {ResourceKey(*k) for k in changed_keys}
    assert reconciler.scope == result.updated

    # No reconciliation action can be applied with the stale scope, the
    # invalidated result, the unconfirmed updated scope, or an earlier token.
    for candidate in (scope, result, result.updated):
        with pytest.raises(TypeError):
            reconciler.reconcile(candidate, action)
    if earlier is not None:
        with pytest.raises(ValueError):
            reconciler.reconcile(earlier, action)
    assert _recon_call_count(cfn) == 0
    assert reconciler.resources == {}

    # Re-confirm the updated scope against the changed selection.
    confirmed = reconciler.confirm_scope(result.updated, changed_sel)
    assert isinstance(confirmed, ConfirmedScope)
    assert confirmed.keys == result.updated.keys
    # The pre-change confirmation remains stale after re-confirmation.
    if earlier is not None:
        with pytest.raises(ValueError):
            reconciler.require_confirmed(earlier)
    assert _recon_call_count(cfn) == 0

    results = reconciler.reconcile(confirmed, action)
    n = len(changed_keys)
    assert results.total == n
    assert results.succeeded == n
    assert results.failed == 0
    assert all(isinstance(o.result, Succeeded) for o in results.outcomes)
    assert {o.resourceId for o in results.outcomes} == {
        f"{s}/{l}" for s, l in changed_keys
    }
    if action is ReconciliationAction.ALIGN_TO_TEMPLATE:
        assert cfn.call_count("update_stack") == n
        assert cfn.call_count("create_change_set") == 0
    else:
        assert cfn.call_count("create_change_set") == n
        assert cfn.call_count("execute_change_set") == n
        assert cfn.call_count("update_stack") == 0
