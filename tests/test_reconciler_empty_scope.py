"""Unit tests for empty-scope confirmation rejection (task 8.8).

Requirement 4.2: confirming a reconciliation scope with zero Drifted Resources
is rejected, the current scope is retained unchanged, and the operator is told
that at least one Drifted Resource must be selected.

Complements ``tests/test_reconciler_scope.py`` (which covers the basic
"prior scope retained" cases) with the remaining edge cases.

Documented semantics (from ``Reconciler``): an empty-scope rejection is a pure
no-op on reconciler state. ``set_scope([])`` does not replace the recorded
scope, and ``confirm_scope`` returning :class:`RejectedEmpty` touches neither
the recorded scope nor the confirmed version. Therefore a confirmation made
*before* the rejected attempt stays valid and can still be reconciled.
"""

from __future__ import annotations

import pytest

from drift_manager.models import (
    ReconciliationAction,
    ResourceDrift,
    ResourceDriftStatus,
    Succeeded,
)
from drift_manager.reconciler import (
    EMPTY_SCOPE_MESSAGE,
    ConfirmedScope,
    Reconciler,
    RejectedEmpty,
    ScopedResource,
)

from .fakes import FakeCfnClient, FakeClock


def res(stack: str, logical: str) -> ScopedResource:
    return ScopedResource(
        stackName=stack,
        resource=ResourceDrift(
            logicalResourceId=logical,
            resourceType="AWS::S3::Bucket",
            resourceDriftStatus=ResourceDriftStatus.MODIFIED,
            physicalResourceId=f"{stack}-{logical.lower()}",
        ),
    )


def make_reconciler() -> tuple[Reconciler, FakeCfnClient]:
    clock = FakeClock()
    cfn = FakeCfnClient(clock)
    for name in ("a", "b"):
        cfn.add_stack(name)
    return Reconciler(cfn, clock), cfn


def test_empty_confirm_with_no_prior_scope_retains_none():
    rc, cfn = make_reconciler()
    empty = rc.set_scope([])
    assert len(empty) == 0
    assert rc.scope is None

    result = rc.confirm_scope(empty, [])
    assert isinstance(result, RejectedEmpty)
    assert result.retained is None
    assert rc.scope is None
    assert cfn.call_count() == 0


def test_rejection_message_requires_at_least_one_drifted_resource():
    rc, _ = make_reconciler()
    result = rc.confirm_scope(rc.set_scope([]), [])
    assert isinstance(result, RejectedEmpty)
    assert result.message == EMPTY_SCOPE_MESSAGE
    assert "at least one Drifted Resource must be selected" in result.message


def test_empty_scope_rejected_even_with_nonempty_current_selection():
    # An empty scope cannot be confirmed by pairing it with a non-empty selection;
    # it must not silently record the selection as a new scope.
    rc, cfn = make_reconciler()
    prior = rc.set_scope([res("a", "X")])
    result = rc.confirm_scope(rc.set_scope([]), [res("b", "Y")])
    assert isinstance(result, RejectedEmpty)
    assert result.retained == prior
    assert rc.scope is prior
    assert cfn.call_count() == 0


def test_rejection_is_not_a_reconcile_gate_token():
    rc, cfn = make_reconciler()
    rc.set_scope([res("a", "X")])
    rejected = rc.confirm_scope(rc.set_scope([]), [])
    with pytest.raises(TypeError):
        rc.reconcile(rejected, ReconciliationAction.ALIGN_TO_TEMPLATE)
    assert cfn.call_count() == 0


def test_retained_scope_can_still_be_confirmed_and_reconciled():
    rc, cfn = make_reconciler()
    selection = [res("a", "X"), res("b", "Y")]
    prior = rc.set_scope(selection)

    rejected = rc.confirm_scope(rc.set_scope([]), [])
    assert isinstance(rejected, RejectedEmpty)
    assert cfn.call_count() == 0  # the rejection itself makes no CFN calls

    confirmed = rc.confirm_scope(rejected.retained, selection)
    assert isinstance(confirmed, ConfirmedScope)
    assert confirmed.version == prior.version

    results = rc.reconcile(confirmed, ReconciliationAction.ALIGN_TO_TEMPLATE)
    assert (results.total, results.succeeded, results.failed) == (2, 2, 0)
    assert all(isinstance(o.result, Succeeded) for o in results.outcomes)
    assert [c.kwargs["StackName"] for c in cfn.calls_for("update_stack")] == [
        "a",
        "b",
    ]


def test_empty_rejection_does_not_revoke_prior_confirmation():
    rc, cfn = make_reconciler()
    selection = [res("a", "X")]
    scope = rc.set_scope(selection)
    confirmed = rc.confirm_scope(scope, selection)
    assert isinstance(confirmed, ConfirmedScope)

    # Both empty-confirmation paths: an empty recorded scope, and deselecting all.
    assert isinstance(rc.confirm_scope(rc.set_scope([]), []), RejectedEmpty)
    assert isinstance(rc.confirm_scope(scope, []), RejectedEmpty)
    assert rc.scope is scope
    assert cfn.call_count() == 0

    # The earlier confirmation is unaltered and still passes the gate.
    assert rc.require_confirmed(confirmed) is confirmed
    results = rc.reconcile(confirmed, ReconciliationAction.ALIGN_TO_TEMPLATE)
    assert (results.total, results.succeeded, results.failed) == (1, 1, 0)
    assert cfn.call_count("update_stack") == 1
