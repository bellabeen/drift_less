"""Property test: successful reconciliation sets the resource to in-sync (task 8.6).

Feature: multi-stack-drift-manager, Property 19: Successful reconciliation sets the resource to in-sync

For any scoped resource whose reconciliation succeeds, align-to-template
re-asserts the template definition (template update) and adopt-actual imports
the actual configuration, and in both cases the resource's drift status is set
to in-sync.

**Validates: Requirements 4.5, 4.6**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from drift_manager.models import (
    ABSENT,
    DifferenceType,
    PropertyDifference,
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
    ScopedResource,
)
from tests.fakes import FakeClock, FakeCfnClient, throttling_error

STACKS = ("stack-a", "stack-b", "stack-c")
RESOURCE_TYPES = ("AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::DynamoDB::Table")
ALIGN_METHODS = ("update_stack",)
ADOPT_METHODS = ("create_change_set", "execute_change_set")

# ---------------------------------------------------------------------------
# Strategies (local to this file)
# ---------------------------------------------------------------------------

_ident = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij0123456789", min_size=1, max_size=8)


@st.composite
def property_differences(draw):
    kind = draw(st.sampled_from(list(DifferenceType)))
    path = "/" + draw(_ident)
    value = draw(st.one_of(st.integers(), _ident, st.booleans()))
    other = draw(st.one_of(st.integers(), _ident, st.booleans()))
    if kind is DifferenceType.ADD:
        return PropertyDifference(path, ABSENT, value, kind)
    if kind is DifferenceType.REMOVE:
        return PropertyDifference(path, value, ABSENT, kind)
    return PropertyDifference(path, value, other, kind)


@st.composite
def scoped_resource(draw):
    stack = draw(st.sampled_from(STACKS))
    logical = "R" + draw(_ident)
    status = draw(
        st.sampled_from([ResourceDriftStatus.MODIFIED, ResourceDriftStatus.DELETED])
    )
    diffs = (
        tuple(draw(st.lists(property_differences(), min_size=1, max_size=3)))
        if status is ResourceDriftStatus.MODIFIED
        else ()
    )
    physical = draw(st.one_of(st.none(), _ident.map(lambda s: f"phys-{s}")))
    return ScopedResource(
        stackName=stack,
        resource=ResourceDrift(
            logicalResourceId=logical,
            resourceType=draw(st.sampled_from(RESOURCE_TYPES)),
            resourceDriftStatus=status,
            propertyDifferences=diffs,
            physicalResourceId=physical,
        ),
    )


scopes = st.lists(
    scoped_resource(),
    min_size=1,
    max_size=12,
    unique_by=lambda r: (r.stackName, r.resource.logicalResourceId),
)

# Transient throttles per (method, stack): at most 4 so the default retry
# policy (5 attempts) always succeeds eventually.
throttle_counts = st.dictionaries(
    st.tuples(st.sampled_from(ALIGN_METHODS + ADOPT_METHODS), st.sampled_from(STACKS)),
    st.integers(min_value=1, max_value=4),
    max_size=4,
)


# ---------------------------------------------------------------------------
# Property 19
# ---------------------------------------------------------------------------


# Feature: multi-stack-drift-manager, Property 19: Successful reconciliation sets the resource to in-sync
@settings(max_examples=100, deadline=None)
@given(
    resources=scopes,
    action=st.sampled_from(list(ReconciliationAction)),
    throttles=throttle_counts,
)
def test_successful_reconciliation_sets_in_sync(resources, action, throttles):
    clock = FakeClock()
    cfn = FakeCfnClient(clock)
    for name in STACKS:
        cfn.add_stack(name)
    for (method, stack), count in throttles.items():
        cfn.script_errors(stack, method, [throttling_error() for _ in range(count)])

    rc = Reconciler(cfn, clock, progress=ProgressTracker(clock))
    scope = rc.set_scope(resources)
    confirmed = rc.confirm_scope(scope, resources)
    assert isinstance(confirmed, ConfirmedScope)

    results = rc.reconcile(confirmed, action)

    # Every resource succeeded and is now IN_SYNC with no property differences.
    assert (results.total, results.succeeded, results.failed) == (
        len(resources),
        len(resources),
        0,
    )
    assert [o.resourceId for o in results.outcomes] == [r.resourceId for r in resources]
    for outcome in results.outcomes:
        assert isinstance(outcome.result, Succeeded)
        assert outcome.action is action
    current = rc.resources
    for item in resources:
        assert rc.resource_status(item.key) is ResourceDriftStatus.IN_SYNC
        drift = current[item.key]
        assert drift.resourceDriftStatus is ResourceDriftStatus.IN_SYNC
        assert drift.propertyDifferences == ()
        assert drift.logicalResourceId == item.resource.logicalResourceId
        assert drift.resourceType == item.resource.resourceType

    # The CFN calls match the action's translation for each resource. Scripted
    # throttles are consumed by the first resource of the stack (retried until
    # success), so that resource makes ``1 + throttles`` attempts.
    remaining = dict(throttles)

    def attempts(method: str, stack: str) -> int:
        return 1 + remaining.pop((method, stack), 0)

    if action is ReconciliationAction.ALIGN_TO_TEMPLATE:
        expected = []
        for item in resources:
            expected += [item.stackName] * attempts("update_stack", item.stackName)
        updates = cfn.calls_for("update_stack")
        assert [c.kwargs["StackName"] for c in updates] == expected
        assert all(c.kwargs.get("UsePreviousTemplate") is True for c in updates)
        assert cfn.call_count("create_change_set") == 0
        assert cfn.call_count("execute_change_set") == 0
    else:
        assert cfn.call_count("update_stack") == 0
        creates = cfn.calls_for("create_change_set")
        executes = cfn.calls_for("execute_change_set")
        expected_creates = []
        expected_executes = []
        for item in resources:
            drift = item.resource
            to_import = [
                {
                    "ResourceType": drift.resourceType,
                    "LogicalResourceId": drift.logicalResourceId,
                    "ResourceIdentifier": {
                        "PhysicalResourceId": drift.physicalResourceId
                        or drift.logicalResourceId
                    },
                }
            ]
            expected_creates += [(item.stackName, to_import)] * attempts(
                "create_change_set", item.stackName
            )
            expected_executes += [item.stackName] * attempts(
                "execute_change_set", item.stackName
            )
        assert [
            (c.kwargs["StackName"], c.kwargs["ResourcesToImport"]) for c in creates
        ] == expected_creates
        assert all(c.kwargs["ChangeSetType"] == IMPORT_CHANGE_SET_TYPE for c in creates)
        assert [c.kwargs["StackName"] for c in executes] == expected_executes

        # Each execute targets the change set created for the same resource.
        create_names = []
        for c in creates:
            if not create_names or create_names[-1] != c.kwargs["ChangeSetName"]:
                create_names.append(c.kwargs["ChangeSetName"])
        execute_targets = []
        for c in executes:
            if not execute_targets or execute_targets[-1] != c.kwargs["ChangeSetName"]:
                execute_targets.append(c.kwargs["ChangeSetName"])
        assert len(create_names) == len(execute_targets) == len(resources)
        for name, target in zip(create_names, execute_targets):
            assert f"/{name}/" in target
