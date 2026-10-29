"""Integration test: adopt-actual (resource import) reconciliation (task 11.2).

Drives the full Drift_Manager pipeline against the FakeCfnClient / FakeClock:
select -> confirm -> detect -> report -> drifted_resources -> select_scope ->
confirm_scope -> reconcile(AdoptActual), and asserts the
``create_change_set(ChangeSetType="IMPORT")`` + ``execute_change_set`` path
leaves every scoped resource in sync.

**Validates: Requirements 4.6**
"""

from __future__ import annotations

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
from tests.fakes import FakeCfnClient, FakeClock

ADOPT = ReconciliationAction.ADOPT_ACTUAL


def drift_record(logical: str, resource_type: str) -> dict:
    return {
        "LogicalResourceId": logical,
        "PhysicalResourceId": f"{logical}-phys",
        "ResourceType": resource_type,
        "StackResourceDriftStatus": "MODIFIED",
        "PropertyDifferences": [
            {
                "PropertyPath": "/Tags",
                "ExpectedValue": "a",
                "ActualValue": "b",
                "DifferenceType": "NOT_EQUAL",
            }
        ],
    }


def test_adopt_actual_imports_resources_and_marks_them_in_sync():
    clock = FakeClock()
    cfn = FakeCfnClient(clock)
    cfn.add_stack("app")
    cfn.add_stack("data")
    cfn.add_stack("clean")
    cfn.script_detection(
        "app",
        status_progression=["DETECTION_IN_PROGRESS", "DETECTION_COMPLETE"],
        stack_drift_status="DRIFTED",
        resource_drift_pages=[
            [
                drift_record("Bucket", "AWS::S3::Bucket"),
                drift_record("Queue", "AWS::SQS::Queue"),
            ]
        ],
    )
    cfn.script_detection(
        "data",
        stack_drift_status="DRIFTED",
        resource_drift_pages=[[drift_record("Table", "AWS::DynamoDB::Table")]],
    )
    cfn.script_detection("clean", stack_drift_status="IN_SYNC")
    manager = DriftManager(cfn, clock)

    # Selection and detection.
    manager.select(Names(names=("app", "data", "clean")))
    manager.confirm_selection()
    manager.detect()
    report = manager.report()
    assert report.counts.DRIFTED == 2
    assert report.counts.IN_SYNC == 1

    # Scope: every drifted resource, spanning two stacks.
    drifted = manager.drifted_resources()
    assert [r.resourceId for r in drifted] == ["app/Bucket", "app/Queue", "data/Table"]
    manager.select_scope(drifted)
    assert isinstance(manager.confirm_scope(), ConfirmedScope)
    assert manager.phase is Phase.SCOPE_CONFIRMED
    assert cfn.call_count("create_change_set") == 0  # gated until reconcile

    results = manager.reconcile(ADOPT)
    assert manager.phase is Phase.RECONCILED

    # Import path only: no template-alignment updates.
    assert cfn.call_count("update_stack") == 0
    creates = cfn.calls_for("create_change_set")
    executes = cfn.calls_for("execute_change_set")
    assert len(creates) == len(drifted) == 3
    assert len(executes) == 3

    # One IMPORT change set per scoped resource, each executed after creation.
    returned_ids = []
    for item, create, execute in zip(drifted, creates, executes):
        res = item.resource
        assert create.kwargs["ChangeSetType"] == "IMPORT"
        assert create.kwargs["StackName"] == item.stackName
        assert create.kwargs["ResourcesToImport"] == [
            {
                "ResourceType": res.resourceType,
                "LogicalResourceId": res.logicalResourceId,
                "ResourceIdentifier": {
                    "PhysicalResourceId": f"{res.logicalResourceId}-phys"
                },
            }
        ]
        assert execute.seq > create.seq
        assert execute.kwargs["StackName"] == item.stackName
        # The fake's returned Id is "arn:...:changeSet/<name>/<counter>".
        expected_id = (
            f"arn:aws:cloudformation:::changeSet/{create.kwargs['ChangeSetName']}/"
            f"{len(returned_ids) + 1:04d}"
        )
        assert execute.kwargs["ChangeSetName"] == expected_id
        returned_ids.append(expected_id)
    # Each execute immediately follows its own create (create, execute, create, ...).
    for i in range(len(creates) - 1):
        assert executes[i].seq < creates[i + 1].seq
    assert len(set(returned_ids)) == 3

    # Resulting resource status is IN_SYNC for every scoped resource.
    for item in drifted:
        assert manager.reconciler.resource_status(item.key) is ResourceDriftStatus.IN_SYNC
        assert manager.reconciler.resources[item.key].propertyDifferences == ()

    # Results and summary.
    assert (results.total, results.succeeded, results.failed) == (3, 3, 0)
    assert all(o.action is ADOPT for o in results.outcomes)
    assert all(isinstance(o.result, Succeeded) for o in results.outcomes)
    summary = manager.reconciliation_summary
    assert isinstance(summary, Produced)
    assert summary.summary.succeeded == 3
    assert summary.summary.failed == 0
    assert [i.id for i in summary.summary.items] == [r.resourceId for r in drifted]
    assert all(i.outcome is ItemOutcome.SUCCEEDED for i in summary.summary.items)
    assert all(i.action is ADOPT for i in summary.summary.items)

    # Progress for the reconciliation operation is complete.
    assert manager.is_operation_complete()
    snapshot = manager.progress()
    assert (snapshot.total, snapshot.completed, snapshot.remaining) == (3, 3, 0)
    assert snapshot.failures == ()

    # The detection report itself is unchanged by reconciliation.
    assert manager.report().counts.DRIFTED == 2
    assert {e.stackName for e in report.entries if e.status is DriftStatus.DRIFTED} == {
        "app",
        "data",
    }
