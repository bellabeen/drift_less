"""Unit tests for Reconciler scope recording, invalidation, and gating (task 8.1).

Requirements: 4.1, 4.2, 4.3, 4.4, 4.10.
"""

from __future__ import annotations

import pytest

from drift_manager.models import ResourceDrift, ResourceDriftStatus
from drift_manager.reconciler import (
    MAX_SCOPE_RESOURCES,
    ConfirmedScope,
    Reconciler,
    RejectedEmpty,
    ResourceKey,
    Scope,
    ScopedResource,
    ScopeInvalidated,
    require_confirmed_scope,
)


def res(stack: str, logical: str) -> ScopedResource:
    return ScopedResource(
        stackName=stack,
        resource=ResourceDrift(
            logicalResourceId=logical,
            resourceType="AWS::S3::Bucket",
            resourceDriftStatus=ResourceDriftStatus.MODIFIED,
        ),
    )


def test_set_scope_records_selected_resources():
    rc = Reconciler()
    selection = [res("a", "Bucket"), res("b", "Bucket")]
    scope = rc.set_scope(selection)
    assert scope.resources == tuple(selection)
    assert rc.scope == scope


def test_identity_includes_owning_stack():
    a, b = res("a", "Bucket"), res("b", "Bucket")
    assert a.key == ResourceKey("a", "Bucket")
    assert a.key != b.key
    assert a.resourceId == "a/Bucket"
    # Same logical id in different stacks are distinct; true duplicates collapse.
    scope = Reconciler().set_scope([a, b, res("a", "Bucket")])
    assert len(scope) == 2


def test_set_scope_rejects_more_than_500():
    rc = Reconciler()
    too_many = [res("s", f"R{i}") for i in range(MAX_SCOPE_RESOURCES + 1)]
    with pytest.raises(ValueError):
        rc.set_scope(too_many)
    assert rc.scope is None
    assert len(rc.set_scope(too_many[:MAX_SCOPE_RESOURCES])) == MAX_SCOPE_RESOURCES


def test_confirm_unchanged_selection_yields_confirmed_scope():
    rc = Reconciler()
    selection = [res("a", "X"), res("b", "Y")]
    scope = rc.set_scope(selection)
    # Order of the current selection does not matter.
    result = rc.confirm_scope(scope, list(reversed(selection)))
    assert isinstance(result, ConfirmedScope)
    assert result.resources == scope.resources
    assert rc.require_confirmed(result) is result


def test_empty_scope_confirmation_rejected_and_prior_scope_retained():
    rc = Reconciler()
    prior = rc.set_scope([res("a", "X")])
    empty = rc.set_scope([])
    result = rc.confirm_scope(empty, [])
    assert isinstance(result, RejectedEmpty)
    assert "at least one" in result.message
    assert result.retained == prior
    assert rc.scope == prior


def test_deselecting_everything_rejects_and_retains_scope():
    rc = Reconciler()
    prior = rc.set_scope([res("a", "X")])
    result = rc.confirm_scope(prior, [])
    assert isinstance(result, RejectedEmpty)
    assert rc.scope == prior


def test_changed_selection_invalidates_and_requires_reconfirmation():
    rc = Reconciler()
    scope = rc.set_scope([res("a", "X")])
    changed = [res("a", "X"), res("b", "Y")]
    result = rc.confirm_scope(scope, changed)
    assert isinstance(result, ScopeInvalidated)
    assert result.previous == scope
    assert result.updated.keys == {ResourceKey("a", "X"), ResourceKey("b", "Y")}
    assert rc.scope == result.updated

    # The old scope can no longer be confirmed, even with its original selection.
    stale = rc.confirm_scope(scope, [res("a", "X")])
    assert isinstance(stale, ScopeInvalidated)

    # Re-confirming the current scope against the current selection succeeds.
    current = rc.scope
    confirmed = rc.confirm_scope(current, current.resources)
    assert isinstance(confirmed, ConfirmedScope)


def test_actions_forbidden_without_confirmation():
    rc = Reconciler()
    scope = rc.set_scope([res("a", "X")])
    for candidate in (scope, RejectedEmpty(retained=scope), None):
        with pytest.raises(TypeError):
            rc.require_confirmed(candidate)
        with pytest.raises(TypeError):
            require_confirmed_scope(candidate)
    with pytest.raises(ValueError):
        ConfirmedScope(resources=scope.resources)


def test_new_scope_after_confirmation_makes_confirmation_stale():
    rc = Reconciler()
    scope = rc.set_scope([res("a", "X")])
    confirmed = rc.confirm_scope(scope, scope.resources)
    assert isinstance(confirmed, ConfirmedScope)
    rc.set_scope([res("b", "Y")])
    with pytest.raises(ValueError):
        rc.require_confirmed(confirmed)


def test_unrecorded_scope_cannot_be_confirmed():
    rc = Reconciler()
    foreign = Reconciler().set_scope([res("a", "X")])
    result = rc.confirm_scope(foreign, foreign.resources)
    assert isinstance(result, ScopeInvalidated)
    assert isinstance(rc.scope, Scope)
