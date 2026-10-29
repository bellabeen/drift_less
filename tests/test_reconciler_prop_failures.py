"""Property test: reconciliation failures are isolated (task 8.7).

Feature: multi-stack-drift-manager, Property 20: Failures are isolated and processing continues

For any confirmed reconciliation scope with arbitrary failures injected, each
failed resource records a failed outcome with a reason and retains its prior
drift status, every other resource in the scope is still processed, and each
recorded failure is reflected in the reported progress.

**Validates: Requirements 4.7, 4.8, 5.4**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import (
    Failed,
    ItemOutcome,
    ReconciliationAction,
    ResourceDrift,
    ResourceDriftStatus,
    Succeeded,
)
from drift_manager.progress import ProgressTracker
from drift_manager.reconciler import ConfirmedScope, Reconciler, ScopedResource
from drift_manager.retry import DEFAULT_RETRY_POLICY
from tests.fakes import FakeClientError, FakeClock, FakeCfnClient, throttling_error

RESOURCE_TYPES = ("AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::DynamoDB::Table")
PRIOR_STATUSES = (
    ResourceDriftStatus.MODIFIED,
    ResourceDriftStatus.DELETED,
    ResourceDriftStatus.UNKNOWN,
)
NON_TRANSIENT_CODES = ("ValidationError", "AccessDenied", "InsufficientCapabilities")

# Per-resource injection modes. One resource per stack, so per-stack injection
# maps deterministically onto exactly one resource.
OK = "ok"  # no errors
RECOVERED = "recovered"  # transient throttles that retry absorbs -> success
HARD = "hard"  # non-transient error -> immediate failure
EXHAUSTED = "exhausted"  # transient throttles past the retry budget -> failure
FAILING_MODES = (HARD, EXHAUSTED)

# ---------------------------------------------------------------------------
# Strategies (local to this file)
# ---------------------------------------------------------------------------

_ident = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij0123456789", min_size=1, max_size=8)


@st.composite
def injected_case(draw, index: int):
    """One scoped resource on its own stack plus the failure to inject."""
    stack = f"stack-{index:03d}"
    resource = ScopedResource(
        stackName=stack,
        resource=ResourceDrift(
            logicalResourceId="R" + draw(_ident),
            resourceType=draw(st.sampled_from(RESOURCE_TYPES)),
            resourceDriftStatus=draw(st.sampled_from(PRIOR_STATUSES)),
            physicalResourceId=draw(st.one_of(st.none(), _ident.map(lambda s: f"phys-{s}"))),
        ),
    )
    mode = draw(st.sampled_from((OK, RECOVERED, HARD, EXHAUSTED)))
    # Which AdoptActual primitive fails (AlignToTemplate always uses update_stack).
    adopt_method = draw(st.sampled_from(("create_change_set", "execute_change_set")))
    code = draw(st.sampled_from(NON_TRANSIENT_CODES))
    throttles = draw(st.integers(min_value=1, max_value=DEFAULT_RETRY_POLICY.max_attempts - 1))
    return resource, mode, adopt_method, code, throttles


@st.composite
def cases(draw):
    n = draw(st.integers(min_value=1, max_value=12))
    return [draw(injected_case(i)) for i in range(n)]


def _inject(cfn: FakeCfnClient, action, resource, mode, adopt_method, code, throttles):
    stack = resource.stackName
    method = (
        "update_stack" if action is ReconciliationAction.ALIGN_TO_TEMPLATE else adopt_method
    )
    if mode == RECOVERED:
        cfn.script_errors(stack, method, [throttling_error() for _ in range(throttles)])
    elif mode == EXHAUSTED:
        cfn.script_errors(
            stack,
            method,
            [throttling_error() for _ in range(DEFAULT_RETRY_POLICY.max_attempts)],
        )
    elif mode == HARD:
        error = FakeClientError(code, f"injected {code} for {stack}")
        cfn.script_reconciliation(stack, **{f"{method}_error": error})


# ---------------------------------------------------------------------------
# Property 20
# ---------------------------------------------------------------------------


# Feature: multi-stack-drift-manager, Property 20: Failures are isolated and processing continues
@settings(max_examples=100, deadline=None)
@given(scenario=cases(), action=st.sampled_from(list(ReconciliationAction)))
def test_failures_are_isolated_and_processing_continues(scenario, action):
    clock = FakeClock()
    cfn = FakeCfnClient(clock)
    for resource, *_ in scenario:
        cfn.add_stack(resource.stackName)
    for resource, mode, adopt_method, code, throttles in scenario:
        _inject(cfn, action, resource, mode, adopt_method, code, throttles)

    resources = [r for r, *_ in scenario]
    expected_failed = {r.resourceId for r, mode, *_ in scenario if mode in FAILING_MODES}

    tracker = ProgressTracker(clock)
    rc = Reconciler(cfn, clock, progress=tracker)
    scope = rc.set_scope(resources)
    confirmed = rc.confirm_scope(scope, resources)
    assert isinstance(confirmed, ConfirmedScope)

    results = rc.reconcile(confirmed, action)

    # Exactly one outcome per scoped resource, in scope order.
    assert [o.resourceId for o in results.outcomes] == [r.resourceId for r in resources]

    # Counts are consistent with the injected failures.
    assert results.total == len(resources)
    assert results.failed == len(expected_failed)
    assert results.succeeded == len(resources) - len(expected_failed)

    by_id = {o.resourceId: o for o in results.outcomes}
    for resource in resources:
        outcome = by_id[resource.resourceId]
        assert outcome.action is action
        status = rc.resource_status(resource.key)
        if resource.resourceId in expected_failed:
            # Failed with a reason; prior drift status retained (4.7).
            assert isinstance(outcome.result, Failed)
            assert outcome.result.reason.strip()
            assert status is resource.resource.resourceDriftStatus
            assert rc.resources[resource.key] == resource.resource
        else:
            # Every other resource is still processed (4.8).
            assert isinstance(outcome.result, Succeeded)
            assert status is ResourceDriftStatus.IN_SYNC

    # Processing continued past failures: every stack saw its first CFN call.
    first_method = (
        "update_stack"
        if action is ReconciliationAction.ALIGN_TO_TEMPLATE
        else "create_change_set"
    )
    called_stacks = {c.kwargs["StackName"] for c in cfn.calls_for(first_method)}
    assert called_stacks == {r.stackName for r in resources}

    # Each recorded failure is reflected in the reported progress (5.4).
    op = rc.operation
    assert op is not None
    snapshot = tracker.observe(op)
    assert snapshot.total == len(resources)
    assert snapshot.completed == snapshot.total
    assert snapshot.remaining == 0
    assert {f.id for f in snapshot.failures} == expected_failed
    assert len(snapshot.failures) == len(expected_failed)
    for failure in snapshot.failures:
        assert failure.reason.strip()
        assert failure.reason == by_id[failure.id].result.reason

    summary = tracker.summary(op)
    assert summary.failed == len(expected_failed)
    assert summary.succeeded == len(resources) - len(expected_failed)
    assert {i.id for i in summary.items if i.outcome is ItemOutcome.FAILED} == expected_failed
