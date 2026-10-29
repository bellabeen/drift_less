"""Integration test: align-to-template (update) reconciliation (task 11.3).

Drives the full Drift_Manager pipeline (select -> confirm -> detect -> report
-> scope -> confirm scope -> reconcile) against the FakeCfnClient / FakeClock
boundary and asserts that ``AlignToTemplate`` re-asserts each stack's template
via ``update_stack(UsePreviousTemplate=True)`` and leaves the reconciled
resources in-sync.

Requirements: 4.5
"""

from __future__ import annotations

import pytest

from drift_manager.manager import DriftManager, Phase
from drift_manager.models import (
    DriftStatus,
    ItemOutcome,
    Names,
    ReconciliationAction,
    ResourceDriftStatus,
    Succeeded,
)
from drift_manager.reconciler import ConfirmedScope
from drift_manager.summary import Produced
from tests.fakes import FakeCfnClient, FakeClock, throttling_error

ALIGN = ReconciliationAction.ALIGN_TO_TEMPLATE


def drift_record(logical: str, resource_type: str) -> dict:
    return {
        "LogicalResourceId": logical,
        "PhysicalResourceId": f"{logical}-phys",
        "ResourceType": resource_type,
        "StackResourceDriftStatus": "MODIFIED",
        "PropertyDifferences": [
            {
                "PropertyPath": "/Versioning",
                "ExpectedValue": "Enabled",
                "ActualValue": "Suspended",
                "DifferenceType": "NOT_EQUAL",
            }
        ],
    }


@pytest.fixture
def env():
    clock = FakeClock()
    cfn = FakeCfnClient(clock)
    cfn.add_stack("alpha")
    cfn.add_stack("beta")
    cfn.add_stack("clean")
    cfn.script_detection(
        "alpha",
        status_progression=["DETECTION_IN_PROGRESS", "DETECTION_COMPLETE"],
        stack_drift_status="DRIFTED",
        resource_drift_pages=[
            [
                drift_record("BucketA", "AWS::S3::Bucket"),
                drift_record("BucketB", "AWS::S3::Bucket"),
            ]
        ],
    )
    cfn.script_detection(
        "beta",
        stack_drift_status="DRIFTED",
        resource_drift_pages=[[drift_record("Queue", "AWS::SQS::Queue")]],
    )
    cfn.script_detection("clean", stack_drift_status="IN_SYNC")
    return DriftManager(cfn, clock), cfn


def run_to_confirmed_scope(manager: DriftManager, cfn: FakeCfnClient):
    manager.select(Names(names=("alpha", "beta", "clean")))
    manager.confirm_selection()
    manager.detect()
    report = manager.report()
    assert {e.stackName: e.status for e in report.entries} == {
        "alpha": DriftStatus.DRIFTED,
        "beta": DriftStatus.DRIFTED,
        "clean": DriftStatus.IN_SYNC,
    }

    drifted = manager.drifted_resources()
    assert [r.resourceId for r in drifted] == [
        "alpha/BucketA",
        "alpha/BucketB",
        "beta/Queue",
    ]
    assert all(r.resource.propertyDifferences for r in drifted)

    manager.select_scope(drifted)
    assert cfn.call_count("update_stack") == 0  # 4.10: nothing before confirm
    confirmed = manager.confirm_scope()
    assert isinstance(confirmed, ConfirmedScope)
    assert manager.phase is Phase.SCOPE_CONFIRMED
    assert cfn.call_count("update_stack") == 0
    return drifted, len(cfn.calls)


def assert_aligned(manager: DriftManager, cfn: FakeCfnClient, drifted, results):
    assert manager.phase is Phase.RECONCILED
    assert (results.total, results.succeeded, results.failed) == (3, 3, 0)
    assert all(o.action is ALIGN for o in results.outcomes)
    assert all(isinstance(o.result, Succeeded) for o in results.outcomes)

    # Template-reassert path only; no import change sets.
    assert cfn.call_count("create_change_set") == 0
    assert cfn.call_count("execute_change_set") == 0

    # 4.5: each reconciled resource is in-sync with its differences cleared.
    for item in drifted:
        assert manager.reconciler.resource_status(item.key) is ResourceDriftStatus.IN_SYNC
        assert manager.reconciler.resources[item.key].propertyDifferences == ()

    summary = manager.reconciliation_summary
    assert isinstance(summary, Produced)
    assert [(i.id, i.outcome) for i in summary.summary.items] == [
        ("alpha/BucketA", ItemOutcome.SUCCEEDED),
        ("alpha/BucketB", ItemOutcome.SUCCEEDED),
        ("beta/Queue", ItemOutcome.SUCCEEDED),
    ]
    assert (summary.summary.succeeded, summary.summary.failed) == (3, 0)

    assert manager.is_operation_complete()
    snap = manager.progress()
    assert (snap.total, snap.completed, snap.remaining) == (3, 3, 0)
    assert snap.failures == ()


def test_align_to_template_updates_each_scoped_resource(env):
    manager, cfn = env
    drifted, calls_before_reconcile = run_to_confirmed_scope(manager, cfn)

    results = manager.reconcile(ALIGN)

    updates = cfn.calls_for("update_stack")
    assert [c.kwargs for c in updates] == [
        {"StackName": "alpha", "UsePreviousTemplate": True},
        {"StackName": "alpha", "UsePreviousTemplate": True},
        {"StackName": "beta", "UsePreviousTemplate": True},
    ]
    # Every update happens after scope confirmation.
    assert all(c.seq >= calls_before_reconcile for c in updates)
    assert_aligned(manager, cfn, drifted, results)


def test_align_to_template_retries_transient_throttle(env):
    manager, cfn = env
    cfn.script_errors("beta", "update_stack", [throttling_error()])
    drifted, calls_before_reconcile = run_to_confirmed_scope(manager, cfn)

    results = manager.reconcile(ALIGN)

    updates = cfn.calls_for("update_stack")
    # beta's first attempt is throttled, then retried and succeeds.
    assert [c.kwargs["StackName"] for c in updates] == ["alpha", "alpha", "beta", "beta"]
    assert all(c.kwargs["UsePreviousTemplate"] is True for c in updates)
    assert all(c.seq >= calls_before_reconcile for c in updates)
    assert updates[3].time > updates[2].time  # retry waited on backoff
    assert_aligned(manager, cfn, drifted, results)
