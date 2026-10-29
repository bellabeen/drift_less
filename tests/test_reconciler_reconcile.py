"""Unit tests for Reconciler.reconcile action translation (task 8.2).

Requirements: 4.5, 4.6, 4.7, 4.8, 4.9.
"""

from __future__ import annotations

import pytest

from drift_manager.models import (
    Failed,
    ReconciliationAction,
    ResourceDrift,
    ResourceDriftStatus,
    Succeeded,
)
from drift_manager.progress import ProgressTracker
from drift_manager.reconciler import (
    IMPORT_CHANGE_SET_TYPE,
    ConfirmedScope,
    Reconciler,
    ResourceKey,
    ScopedResource,
)
from tests.fakes import FakeClientError, FakeClock, FakeCfnClient, throttling_error

ALIGN = ReconciliationAction.ALIGN_TO_TEMPLATE
ADOPT = ReconciliationAction.ADOPT_ACTUAL


def res(stack: str, logical: str) -> ScopedResource:
    return ScopedResource(
        stackName=stack,
        resource=ResourceDrift(
            logicalResourceId=logical,
            resourceType="AWS::S3::Bucket",
            resourceDriftStatus=ResourceDriftStatus.MODIFIED,
            physicalResourceId=f"{stack}-{logical}-phys",
        ),
    )


def setup(*resources: ScopedResource, stacks=("a", "b", "c")):
    clock = FakeClock()
    cfn = FakeCfnClient(clock)
    for name in stacks:
        cfn.add_stack(name)
    tracker = ProgressTracker(clock)
    rc = Reconciler(cfn, clock, progress=tracker)
    scope = rc.set_scope(resources)
    confirmed = rc.confirm_scope(scope, resources)
    assert isinstance(confirmed, ConfirmedScope)
    return rc, cfn, tracker, confirmed


def test_align_to_template_calls_update_stack_once_per_resource():
    items = (res("a", "Bucket"), res("b", "Bucket"))
    rc, cfn, _, confirmed = setup(*items)

    results = rc.reconcile(confirmed, ALIGN)

    updates = cfn.calls_for("update_stack")
    assert [c.kwargs["StackName"] for c in updates] == ["a", "b"]
    assert cfn.call_count("create_change_set") == 0
    assert (results.total, results.succeeded, results.failed) == (2, 2, 0)
    assert all(isinstance(o.result, Succeeded) for o in results.outcomes)
    assert [o.resourceId for o in results.outcomes] == ["a/Bucket", "b/Bucket"]
    for item in items:
        assert rc.resource_status(item.key) is ResourceDriftStatus.IN_SYNC


def test_adopt_actual_creates_and_executes_import_change_set():
    item = res("a", "Bucket")
    rc, cfn, _, confirmed = setup(item)

    results = rc.reconcile(confirmed, ADOPT)

    (create,) = cfn.calls_for("create_change_set")
    assert create.kwargs["ChangeSetType"] == IMPORT_CHANGE_SET_TYPE
    assert create.kwargs["StackName"] == "a"
    imported = create.kwargs["ResourcesToImport"][0]
    assert imported["LogicalResourceId"] == "Bucket"
    assert imported["ResourceType"] == "AWS::S3::Bucket"
    (execute,) = cfn.calls_for("execute_change_set")
    assert execute.kwargs["StackName"] == "a"
    assert execute.seq > create.seq
    assert cfn.call_count("update_stack") == 0
    assert results.succeeded == 1
    assert rc.resources[ResourceKey("a", "Bucket")].resourceDriftStatus is (
        ResourceDriftStatus.IN_SYNC
    )


@pytest.mark.parametrize(
    "action, script",
    [
        (ALIGN, {"update_stack_error": FakeClientError("ValidationError", "boom")}),
        (ADOPT, {"create_change_set_error": FakeClientError("ValidationError", "boom")}),
        (ADOPT, {"execute_change_set_error": FakeClientError("ValidationError", "boom")}),
    ],
)
def test_failure_is_isolated_and_processing_continues(action, script):
    items = (res("a", "Bucket"), res("b", "Bucket"), res("c", "Bucket"))
    rc, cfn, tracker, confirmed = setup(*items)
    cfn.script_reconciliation("b", **script)

    results = rc.reconcile(confirmed, action)

    assert (results.total, results.succeeded, results.failed) == (3, 2, 1)
    by_id = {o.resourceId: o.result for o in results.outcomes}
    assert isinstance(by_id["a/Bucket"], Succeeded)
    assert isinstance(by_id["c/Bucket"], Succeeded)
    assert isinstance(by_id["b/Bucket"], Failed)
    assert "boom" in by_id["b/Bucket"].reason
    # Prior drift status retained for the failed resource only.
    assert rc.resource_status(ResourceKey("b", "Bucket")) is ResourceDriftStatus.MODIFIED
    assert rc.resource_status(ResourceKey("c", "Bucket")) is ResourceDriftStatus.IN_SYNC
    # Failure surfaced to the Progress_Tracker; operation completed.
    snap = tracker.observe(rc.operation)
    assert (snap.total, snap.completed, snap.remaining) == (3, 3, 0)
    assert [f.id for f in snap.failures] == ["b/Bucket"]
    summary = tracker.summary(rc.operation)
    assert (summary.succeeded, summary.failed) == (2, 1)


def test_transient_error_is_retried_then_succeeds():
    item = res("a", "Bucket")
    rc, cfn, _, confirmed = setup(item)
    cfn.script_errors("a", "update_stack", [throttling_error(), throttling_error()])

    results = rc.reconcile(confirmed, ALIGN)

    assert results.succeeded == 1
    assert cfn.call_count("update_stack") == 3
    assert cfn.clock.time() > 0  # backoff slept on the injected clock


def test_retry_exhaustion_records_failed():
    item = res("a", "Bucket")
    rc, cfn, _, confirmed = setup(item)
    cfn.script_errors("a", "execute_change_set", [throttling_error()] * 10)

    results = rc.reconcile(confirmed, ADOPT)

    (outcome,) = results.outcomes
    assert isinstance(outcome.result, Failed)
    assert "Throttling" in outcome.result.reason
    # Change set created once; only the execute call was retried.
    assert cfn.call_count("create_change_set") == 1
    assert cfn.call_count("execute_change_set") == 5
    assert rc.resource_status(item.key) is ResourceDriftStatus.MODIFIED


def test_reconcile_requires_confirmed_scope():
    item = res("a", "Bucket")
    rc, cfn, _, _ = setup(item)
    unconfirmed = rc.set_scope([item])
    with pytest.raises(TypeError):
        rc.reconcile(unconfirmed, ALIGN)
    assert cfn.call_count("update_stack") == 0
