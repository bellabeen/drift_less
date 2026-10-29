"""Unit tests for the Drift_Manager orchestrator phase flow (task 10.1).

Requirements: 1.9, 1.10, 1.11, 2.7, 3.1, 4.4, 4.10, 5.5, 6.1, 6.2.
"""

from __future__ import annotations

import json

import pytest

from drift_manager.manager import DriftManager, Phase, PhaseError
from drift_manager.models import (
    DriftStatus,
    ItemOutcome,
    Names,
    ReconciliationAction,
    ResourceDriftStatus,
    Tags,
    TagFilter,
)
from drift_manager.reconciler import ConfirmedScope, RejectedEmpty, ScopeInvalidated
from drift_manager.selector import (
    Aborted,
    EmptySelection,
    FailedSelection,
    RejectedSelection,
    Resolved,
)
from drift_manager.summary import Produced
from tests.fakes import FakeCfnClient, FakeClock

ALIGN = ReconciliationAction.ALIGN_TO_TEMPLATE


def drift_record(logical: str) -> dict:
    return {
        "LogicalResourceId": logical,
        "PhysicalResourceId": f"{logical}-phys",
        "ResourceType": "AWS::S3::Bucket",
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
    cfn.add_stack("drifted")
    cfn.add_stack("clean")
    cfn.add_stack("busy", status="UPDATE_IN_PROGRESS")
    cfn.script_detection(
        "drifted",
        status_progression=["DETECTION_IN_PROGRESS", "DETECTION_COMPLETE"],
        stack_drift_status="DRIFTED",
        resource_drift_pages=[[drift_record("BucketA"), drift_record("BucketB")]],
    )
    cfn.script_detection("clean", stack_drift_status="IN_SYNC")
    return DriftManager(cfn, clock), cfn


def to_reported(manager: DriftManager, names=("drifted", "clean", "busy")):
    manager.select(Names(names=tuple(names)))
    manager.confirm_selection()
    manager.detect()
    return manager.report()


def test_happy_path_runs_every_phase(env):
    manager, cfn = env

    result = manager.select(Names(names=("drifted", "clean", "busy", "ghost")))
    assert isinstance(result, Resolved)
    assert manager.phase is Phase.RESOLVED
    assert cfn.call_count("detect_stack_drift") == 0  # 1.9: presented first

    confirmed = manager.confirm_selection()
    assert confirmed.unmatchedNames == ("ghost",)
    assert manager.phase is Phase.CONFIRMED

    results = manager.detect()
    assert manager.phase is Phase.DETECTED
    assert len(results.outcomes) == 3  # 2.7
    assert manager.is_operation_complete()
    snap = manager.progress()
    assert (snap.total, snap.completed, snap.remaining) == (3, 3, 0)

    summary = manager.detection_summary
    assert isinstance(summary, Produced)  # 6.1
    assert summary.summary.unmatchedNames == ("ghost",)
    assert {i.id: i.driftStatus for i in summary.summary.items} == {
        "drifted": DriftStatus.DRIFTED,
        "clean": DriftStatus.IN_SYNC,
        "busy": DriftStatus.NOT_CHECKED,
    }

    report = manager.report()  # 3.1
    assert manager.phase is Phase.REPORTED
    assert [e.stackName for e in report.entries] == ["drifted", "clean", "busy"]
    parsed = json.loads(manager.report_json())
    assert [s["driftStatus"] for s in parsed["stacks"]] == [
        "DRIFTED",
        "IN_SYNC",
        "NOT_CHECKED",
    ]

    drifted = manager.drifted_resources()
    assert [r.resourceId for r in drifted] == ["drifted/BucketA", "drifted/BucketB"]

    manager.select_scope(drifted[:1])
    assert manager.phase is Phase.SCOPE_RECORDED
    assert isinstance(manager.confirm_scope(), ConfirmedScope)
    assert manager.phase is Phase.SCOPE_CONFIRMED
    assert cfn.call_count("update_stack") == 0  # 4.10

    rec = manager.reconcile(ALIGN)  # 4.4
    assert manager.phase is Phase.RECONCILED
    assert (rec.total, rec.succeeded, rec.failed) == (1, 1, 0)
    assert [c.kwargs["StackName"] for c in cfn.calls_for("update_stack")] == ["drifted"]
    assert (
        manager.reconciler.resource_status(drifted[0].key)
        is ResourceDriftStatus.IN_SYNC
    )
    rsummary = manager.reconciliation_summary
    assert isinstance(rsummary, Produced)  # 6.2, 5.5
    assert [(i.id, i.outcome) for i in rsummary.summary.items] == [
        ("drifted/BucketA", ItemOutcome.SUCCEEDED)
    ]
    snap = manager.progress()
    assert (snap.total, snap.completed, snap.remaining) == (1, 1, 0)


def test_incremental_detection_exposes_in_flight_progress(env):
    manager, cfn = env
    manager.select(Names(names=("drifted", "clean")))
    manager.confirm_selection()
    manager.start_detection()
    assert manager.phase is Phase.DETECTING
    assert manager.progress().completed == 0

    while not manager.advance_detection():
        snap = manager.progress()
        assert snap.completed + snap.remaining == snap.total == 2
    assert manager.phase is Phase.DETECTED
    assert manager.progress().remaining == 0


def test_cancel_aborts_without_detection(env):
    manager, cfn = env
    manager.select(Names(names=("drifted", "clean")))

    assert isinstance(manager.cancel_selection(), Aborted)  # 1.11
    assert manager.phase is Phase.ABORTED
    with pytest.raises(PhaseError):
        manager.start_detection()
    with pytest.raises(PhaseError):
        manager.detect()
    assert cfn.call_count("detect_stack_drift") == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda m: m.confirm_selection(),
        lambda m: m.cancel_selection(),
        lambda m: m.start_detection(),
        lambda m: m.detect(),
        lambda m: m.report(),
        lambda m: m.drifted_resources(),
        lambda m: m.confirm_scope(),
        lambda m: m.reconcile(ALIGN),
    ],
)
def test_out_of_order_calls_from_idle_are_rejected(env, call):
    manager, cfn = env
    with pytest.raises(PhaseError):
        call(manager)
    assert manager.phase is Phase.IDLE
    assert cfn.call_count("detect_stack_drift") == 0


def test_detection_requires_confirmation(env):
    manager, cfn = env
    manager.select(Names(names=("drifted",)))
    with pytest.raises(PhaseError, match="start drift detection"):
        manager.start_detection()
    with pytest.raises(PhaseError):
        manager.detect()
    assert cfn.call_count("detect_stack_drift") == 0


def test_reconcile_requires_scope_confirmation(env):
    manager, cfn = env
    to_reported(manager)
    with pytest.raises(PhaseError):
        manager.reconcile(ALIGN)
    manager.select_scope(manager.drifted_resources())
    with pytest.raises(PhaseError):
        manager.reconcile(ALIGN)
    assert cfn.call_count("update_stack") == 0
    with pytest.raises(PhaseError):
        manager.select(Names(names=("clean",)))


@pytest.mark.parametrize(
    "criterion, expected",
    [
        (Names(names=("nope",)), EmptySelection),
        (Names(names=()), RejectedSelection),
        (Tags(filters=()), RejectedSelection),
        (Tags(filters=(TagFilter("env", "prod"),)), EmptySelection),
    ],
)
def test_unresolved_selection_stays_idle(env, criterion, expected):
    manager, cfn = env
    result = manager.select(criterion)
    assert isinstance(result, expected)
    assert manager.selection_result is result
    assert manager.phase is Phase.IDLE
    with pytest.raises(PhaseError):
        manager.confirm_selection()
    # A fresh, valid selection can still proceed.
    assert isinstance(manager.select(Names(names=("clean",))), Resolved)
    assert cfn.call_count("detect_stack_drift") == 0


def test_failed_region_selection_stays_idle():
    from drift_manager.models import Region

    class FailingCfn(FakeCfnClient):
        def list_stacks(self, **kwargs):
            raise RuntimeError("AccessDenied")

    manager = DriftManager(FailingCfn(FakeClock()), FakeClock())
    result = manager.select(Region(region="xx-nowhere-1"))
    assert isinstance(result, FailedSelection)
    assert "AccessDenied" in result.reason
    assert manager.phase is Phase.IDLE


def test_scope_invalidation_requires_reconfirmation(env):
    manager, cfn = env
    to_reported(manager)
    a, b = manager.drifted_resources()

    manager.select_scope([a])
    manager.change_selection([a, b])  # selection changes before confirmation

    invalidated = manager.confirm_scope()
    assert isinstance(invalidated, ScopeInvalidated)  # 4.3
    assert manager.phase is Phase.SCOPE_RECORDED
    with pytest.raises(PhaseError):
        manager.reconcile(ALIGN)
    assert cfn.call_count("update_stack") == 0

    confirmed = manager.confirm_scope()
    assert isinstance(confirmed, ConfirmedScope)
    assert confirmed.keys == {a.key, b.key}
    results = manager.reconcile(ALIGN)
    assert results.total == 2


def test_empty_scope_rejected_and_prior_scope_retained(env):
    manager, _ = env
    to_reported(manager)
    a, _b = manager.drifted_resources()

    manager.select_scope([])
    assert isinstance(manager.confirm_scope(), RejectedEmpty)
    assert manager.phase is Phase.REPORTED

    recorded = manager.select_scope([a])
    manager.select_scope([])
    rejected = manager.confirm_scope()
    assert isinstance(rejected, RejectedEmpty)
    assert rejected.retained == recorded
    assert manager.phase is Phase.SCOPE_RECORDED


def test_reselecting_after_confirmation_requires_new_confirmation(env):
    manager, cfn = env
    to_reported(manager)
    a, b = manager.drifted_resources()
    manager.select_scope([a])
    manager.confirm_scope()

    manager.select_scope([b])
    assert manager.phase is Phase.SCOPE_RECORDED
    with pytest.raises(PhaseError):
        manager.reconcile(ALIGN)
    assert cfn.call_count("update_stack") == 0


def test_scope_must_come_from_report(env):
    manager, _ = env
    to_reported(manager)
    from drift_manager.models import ResourceDrift
    from drift_manager.reconciler import ScopedResource

    stranger = ScopedResource(
        stackName="clean",
        resource=ResourceDrift(
            logicalResourceId="Other",
            resourceType="AWS::S3::Bucket",
            resourceDriftStatus=ResourceDriftStatus.MODIFIED,
        ),
    )
    with pytest.raises(ValueError):
        manager.select_scope([stranger])
    assert manager.phase is Phase.REPORTED
