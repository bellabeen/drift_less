"""End-to-end gate enforcement for the Drift_Manager (task 10.2).

Uses the FakeCfnClient call records (``calls``, ``call_count``, ``calls_for``,
``CallRecord.seq``) to show that:

- no ``detect_stack_drift`` call is made before the resolved stack set is
  confirmed, and every such call is ordered after the confirmation (1.9);
- no reconciliation call (``update_stack`` / ``create_change_set`` /
  ``execute_change_set``) is made before the reconciliation scope is
  confirmed, and every such call is ordered after that confirmation (4.10);
- attempts to bypass the orchestrator (driving ``BulkDetection`` or the
  ``Reconciler`` directly with unconfirmed, invalidated, rejected, stale, or
  forged tokens) produce no CloudFormation detection/reconciliation calls.

Requirements: 1.9, 4.10.
"""

from __future__ import annotations

from typing import Callable

import pytest

from drift_manager.detector import BulkDetection
from drift_manager.manager import DriftManager, Phase, PhaseError
from drift_manager.models import Names, ReconciliationAction
from drift_manager.reconciler import (
    ConfirmedScope,
    RejectedEmpty,
    ScopeInvalidated,
)
from drift_manager.selector import ConfirmedSet
from tests.fakes import FakeCfnClient, FakeClock

ALIGN = ReconciliationAction.ALIGN_TO_TEMPLATE
ADOPT = ReconciliationAction.ADOPT_ACTUAL
ACTIONS = (ALIGN, ADOPT)

DETECT = "detect_stack_drift"
RECONCILE_METHODS = ("update_stack", "create_change_set", "execute_change_set")
STACKS = ("drifted", "clean", "other")


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
    for name in STACKS:
        cfn.add_stack(name)
    cfn.script_detection(
        "drifted",
        status_progression=["DETECTION_IN_PROGRESS", "DETECTION_COMPLETE"],
        stack_drift_status="DRIFTED",
        resource_drift_pages=[[drift_record("BucketA"), drift_record("BucketB")]],
    )
    cfn.script_detection(
        "other",
        status_progression=["DETECTION_IN_PROGRESS", "DETECTION_COMPLETE"],
        stack_drift_status="DRIFTED",
        resource_drift_pages=[[drift_record("Queue")]],
    )
    cfn.script_detection("clean", stack_drift_status="IN_SYNC")
    return DriftManager(cfn, clock), cfn, clock


def reconciliation_calls(cfn: FakeCfnClient) -> list:
    return [c for c in cfn.calls if c.method in RECONCILE_METHODS]


def assert_no_reconciliation_calls(cfn: FakeCfnClient) -> None:
    for method in RECONCILE_METHODS:
        assert cfn.call_count(method) == 0, method


def next_seq(cfn: FakeCfnClient) -> int:
    """The seq the next recorded CloudFormation call will receive."""
    return len(cfn.calls)


def to_reported(manager: DriftManager) -> None:
    manager.select(Names(names=STACKS))
    manager.confirm_selection()
    manager.detect()
    manager.report()


# ---------------------------------------------------------------------------
# Detection gate (1.9)
# ---------------------------------------------------------------------------


DETECTION_ATTEMPTS: dict[str, Callable[[DriftManager], object]] = {
    "start_detection": lambda m: m.start_detection(),
    "advance_detection": lambda m: m.advance_detection(),
    "detect": lambda m: m.detect(),
}

#: Setups reaching every phase that precedes the detection gate.
PRE_DETECTION_STATES: dict[str, Callable[[DriftManager], Phase]] = {
    "idle": lambda m: Phase.IDLE,
    "empty-selection": lambda m: (m.select(Names(names=("ghost",))), Phase.IDLE)[1],
    "rejected-selection": lambda m: (m.select(Names(names=())), Phase.IDLE)[1],
    "resolved": lambda m: (m.select(Names(names=STACKS)), Phase.RESOLVED)[1],
    "re-resolved": lambda m: (
        m.select(Names(names=STACKS)),
        m.select(Names(names=("clean",))),
        Phase.RESOLVED,
    )[2],
    "aborted": lambda m: (
        m.select(Names(names=STACKS)),
        m.cancel_selection(),
        Phase.ABORTED,
    )[2],
}


@pytest.mark.parametrize("attempt", DETECTION_ATTEMPTS.values(), ids=DETECTION_ATTEMPTS)
@pytest.mark.parametrize("setup", PRE_DETECTION_STATES.values(), ids=PRE_DETECTION_STATES)
def test_detection_never_starts_before_confirmation(env, setup, attempt):
    manager, cfn, _ = env
    phase = setup(manager)

    with pytest.raises(PhaseError):
        attempt(manager)

    assert manager.phase is phase
    assert cfn.call_count(DETECT) == 0
    assert cfn.call_count("describe_stack_drift_detection_status") == 0
    assert manager.detection_results is None


def test_every_detect_call_is_ordered_after_confirmation(env):
    manager, cfn, _ = env
    manager.select(Names(names=STACKS))
    assert cfn.call_count() > 0  # selection itself does consult the inventory
    assert cfn.call_count(DETECT) == 0

    manager.confirm_selection()
    confirmed_at = next_seq(cfn)
    assert cfn.call_count(DETECT) == 0

    manager.start_detection()
    assert cfn.call_count(DETECT) == 0  # starting does not call CFN yet

    manager.detect()
    detect_calls = cfn.calls_for(DETECT)
    assert sorted(c.kwargs["StackName"] for c in detect_calls) == sorted(STACKS)
    assert all(c.seq >= confirmed_at for c in detect_calls)
    # Every pre-confirmation call was an inventory lookup, never detection.
    assert {c.method for c in cfn.calls if c.seq < confirmed_at} <= {
        "list_stacks",
        "describe_stacks",
    }


def test_cancelled_then_reselected_detects_only_after_new_confirmation(env):
    manager, cfn, _ = env
    manager.select(Names(names=STACKS))
    manager.cancel_selection()
    manager.select(Names(names=("clean",)))
    assert cfn.call_count(DETECT) == 0

    manager.confirm_selection()
    confirmed_at = next_seq(cfn)
    manager.detect()

    detect_calls = cfn.calls_for(DETECT)
    assert [c.kwargs["StackName"] for c in detect_calls] == ["clean"]
    assert all(c.seq >= confirmed_at for c in detect_calls)


def test_bulk_detection_bypass_with_unconfirmed_tokens_makes_no_calls(env):
    manager, cfn, clock = env
    resolved = manager.select(Names(names=STACKS))

    # Driving the detector directly with the unconfirmed Resolved set.
    with pytest.raises(TypeError):
        BulkDetection(cfn, clock, resolved)

    # Forging the gate token is rejected.
    with pytest.raises(ValueError):
        ConfirmedSet(stacks=resolved.stacks)

    aborted = manager.cancel_selection()
    with pytest.raises(TypeError):
        BulkDetection(cfn, clock, aborted)
    with pytest.raises(TypeError):
        BulkDetection(cfn, clock, None)

    assert cfn.call_count(DETECT) == 0
    assert cfn.call_count("describe_stack_drift_detection_status") == 0


# ---------------------------------------------------------------------------
# Reconciliation gate (4.10)
# ---------------------------------------------------------------------------


def _mid_detection(m: DriftManager) -> Phase:
    m.select(Names(names=STACKS))
    m.confirm_selection()
    m.start_detection()
    m.advance_detection()
    assert m.phase is Phase.DETECTING
    return Phase.DETECTING


def _scope_recorded(m: DriftManager) -> Phase:
    to_reported(m)
    m.select_scope(m.drifted_resources())
    return Phase.SCOPE_RECORDED


def _invalidated(m: DriftManager) -> Phase:
    to_reported(m)
    a, b, _q = m.drifted_resources()
    m.select_scope([a])
    m.change_selection([a, b])
    assert isinstance(m.confirm_scope(), ScopeInvalidated)
    return Phase.SCOPE_RECORDED


def _rejected_empty(m: DriftManager) -> Phase:
    to_reported(m)
    m.select_scope([])
    assert isinstance(m.confirm_scope(), RejectedEmpty)
    return Phase.REPORTED


def _rejected_empty_after_recording(m: DriftManager) -> Phase:
    to_reported(m)
    m.select_scope(m.drifted_resources())
    m.select_scope([])
    assert isinstance(m.confirm_scope(), RejectedEmpty)
    return Phase.SCOPE_RECORDED


def _reselected_after_confirmation(m: DriftManager) -> Phase:
    to_reported(m)
    a, b, _q = m.drifted_resources()
    m.select_scope([a])
    assert isinstance(m.confirm_scope(), ConfirmedScope)
    m.select_scope([b])
    return Phase.SCOPE_RECORDED


def _emptied_after_confirmation(m: DriftManager) -> Phase:
    to_reported(m)
    m.select_scope(m.drifted_resources())
    assert isinstance(m.confirm_scope(), ConfirmedScope)
    m.select_scope([])
    return Phase.SCOPE_RECORDED


def _confirmed_then_reported(m: DriftManager) -> Phase:
    # Reading the report again must not count as a scope confirmation.
    to_reported(m)
    m.report()
    return Phase.REPORTED


#: Setups reaching every phase that precedes the reconciliation gate.
PRE_RECONCILIATION_STATES: dict[str, Callable[[DriftManager], Phase]] = {
    "idle": lambda m: Phase.IDLE,
    "resolved": lambda m: (m.select(Names(names=STACKS)), Phase.RESOLVED)[1],
    "aborted": lambda m: (
        m.select(Names(names=STACKS)),
        m.cancel_selection(),
        Phase.ABORTED,
    )[2],
    "confirmed": lambda m: (
        m.select(Names(names=STACKS)),
        m.confirm_selection(),
        Phase.CONFIRMED,
    )[2],
    "detecting": _mid_detection,
    "detected": lambda m: (
        m.select(Names(names=STACKS)),
        m.confirm_selection(),
        m.detect(),
        Phase.DETECTED,
    )[3],
    "reported": _confirmed_then_reported,
    "scope-recorded": _scope_recorded,
    "scope-invalidated": _invalidated,
    "rejected-empty": _rejected_empty,
    "rejected-empty-retained": _rejected_empty_after_recording,
    "reselected-after-confirmation": _reselected_after_confirmation,
    "emptied-after-confirmation": _emptied_after_confirmation,
}


@pytest.mark.parametrize("action", ACTIONS, ids=[a.value for a in ACTIONS])
@pytest.mark.parametrize(
    "setup", PRE_RECONCILIATION_STATES.values(), ids=PRE_RECONCILIATION_STATES
)
def test_reconciliation_never_starts_before_scope_confirmation(env, setup, action):
    manager, cfn, _ = env
    phase = setup(manager)

    with pytest.raises(PhaseError):
        manager.reconcile(action)

    assert manager.phase is phase
    assert_no_reconciliation_calls(cfn)
    assert manager.reconciliation_results is None


@pytest.mark.parametrize("action", ACTIONS, ids=[a.value for a in ACTIONS])
def test_every_reconciliation_call_is_ordered_after_scope_confirmation(env, action):
    manager, cfn, _ = env
    manager.select(Names(names=STACKS))
    manager.confirm_selection()
    detection_confirmed_at = next_seq(cfn)
    manager.detect()
    manager.report()
    a, b, q = manager.drifted_resources()

    # Invalidate once, then re-confirm: only the final confirmation opens the gate.
    manager.select_scope([a])
    manager.change_selection([a, b, q])
    assert isinstance(manager.confirm_scope(), ScopeInvalidated)
    assert_no_reconciliation_calls(cfn)

    confirmed = manager.confirm_scope()
    assert isinstance(confirmed, ConfirmedScope)
    scope_confirmed_at = next_seq(cfn)
    assert_no_reconciliation_calls(cfn)

    results = manager.reconcile(action)
    assert results.total == 3

    calls = reconciliation_calls(cfn)
    assert calls, "reconciliation should have called CloudFormation"
    assert all(c.seq >= scope_confirmed_at for c in calls)
    assert {c.kwargs["StackName"] for c in calls} == {"drifted", "other"}
    # Detection itself is entirely after the stack confirmation and before
    # the scope confirmation.
    assert all(
        detection_confirmed_at <= c.seq < scope_confirmed_at
        for c in cfn.calls_for(DETECT)
    )
    if action is ALIGN:
        assert cfn.call_count("update_stack") == 3
        assert cfn.call_count("create_change_set") == 0
    else:
        assert cfn.call_count("update_stack") == 0
        assert cfn.call_count("create_change_set") == 3
        assert cfn.call_count("execute_change_set") == 3

    # The gate does not reopen: a second reconcile is rejected with no calls.
    before = len(cfn.calls)
    with pytest.raises(PhaseError):
        manager.reconcile(action)
    assert len(cfn.calls) == before


@pytest.mark.parametrize("action", ACTIONS, ids=[a.value for a in ACTIONS])
def test_reconciler_bypass_with_non_confirmed_tokens_makes_no_calls(env, action):
    manager, cfn, _ = env
    to_reported(manager)
    a, b, _q = manager.drifted_resources()
    reconciler = manager.reconciler

    # An unconfirmed (recorded) scope.
    scope = manager.select_scope([a])
    with pytest.raises(TypeError):
        reconciler.reconcile(scope, action)

    # A ScopeInvalidated result and its updated (still unconfirmed) scope.
    manager.change_selection([a, b])
    invalidated = manager.confirm_scope()
    assert isinstance(invalidated, ScopeInvalidated)
    with pytest.raises(TypeError):
        reconciler.reconcile(invalidated, action)
    with pytest.raises(TypeError):
        reconciler.reconcile(invalidated.updated, action)

    # A RejectedEmpty result.
    manager.select_scope([])
    rejected = manager.confirm_scope()
    assert isinstance(rejected, RejectedEmpty)
    with pytest.raises(TypeError):
        reconciler.reconcile(rejected, action)

    # A forged ConfirmedScope cannot even be constructed.
    with pytest.raises(ValueError):
        ConfirmedScope(resources=(a,))

    assert_no_reconciliation_calls(cfn)
    assert manager.phase is not Phase.SCOPE_CONFIRMED


@pytest.mark.parametrize("action", ACTIONS, ids=[a.value for a in ACTIONS])
def test_stale_confirmation_bypass_makes_no_calls(env, action):
    manager, cfn, _ = env
    to_reported(manager)
    a, b, _q = manager.drifted_resources()

    manager.select_scope([a])
    stale = manager.confirm_scope()
    assert isinstance(stale, ConfirmedScope)

    # Recording a new scope invalidates the earlier confirmation.
    manager.select_scope([b])
    with pytest.raises(ValueError, match="stale"):
        manager.reconciler.reconcile(stale, action)
    with pytest.raises(PhaseError):
        manager.reconcile(action)

    # An empty re-selection after a fresh confirmation also requires re-confirming.
    fresh = manager.confirm_scope()
    assert isinstance(fresh, ConfirmedScope)
    manager.select_scope([])
    assert manager.phase is Phase.SCOPE_RECORDED
    with pytest.raises(PhaseError):
        manager.reconcile(action)

    assert_no_reconciliation_calls(cfn)


@pytest.mark.parametrize("action", ACTIONS, ids=[a.value for a in ACTIONS])
def test_confirmation_from_another_manager_is_rejected(env, action):
    manager, cfn, clock = env
    to_reported(manager)

    other = DriftManager(cfn, clock)
    to_reported(other)
    other.select_scope(other.drifted_resources())
    foreign = other.confirm_scope()
    assert isinstance(foreign, ConfirmedScope)

    manager.select_scope(manager.drifted_resources())
    with pytest.raises(ValueError):
        manager.reconciler.reconcile(foreign, action)

    assert_no_reconciliation_calls(cfn)
