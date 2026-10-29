"""CloudFormation client adapter interface (``CfnClient`` protocol).

This module defines the thin boundary between the Multi-Stack Drift Manager and
the AWS CloudFormation API. The orchestration logic (selector, detector,
reporter, reconciler) depends only on this protocol so that it can run against
the real boto3 CloudFormation client in production and against the in-memory
``FakeCfnClient`` (see ``tests/fakes.py``) in tests.

The method signatures and the shape of the request/response dictionaries follow
the AWS CloudFormation API surface documented in the design:

- ``list_stacks`` / ``describe_stacks`` - stack inventory and metadata.
- ``detect_stack_drift`` - initiate asynchronous per-stack drift detection,
  returning a ``StackDriftDetectionId``.
- ``describe_stack_drift_detection_status`` - poll the detection operation;
  returns ``DetectionStatus`` (``DETECTION_IN_PROGRESS`` / ``DETECTION_FAILED``
  / ``DETECTION_COMPLETE``) and ``StackDriftStatus`` (``DRIFTED`` / ``IN_SYNC``
  / ``UNKNOWN`` / ``NOT_CHECKED``).
- ``describe_stack_resource_drifts`` - per-resource drift once detection
  completes; paginated via ``NextToken``.
- ``create_change_set`` / ``execute_change_set`` / ``update_stack`` -
  reconciliation primitives (import change set for adopt-actual, update /
  template-reassert change set for align-to-template).

Dictionaries use the CloudFormation PascalCase key convention so the real
adapter is a near pass-through to boto3. These are documented via ``TypedDict``
aliases to make the expected shapes explicit without constraining callers.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Response/record shape aliases (documentation-oriented)
# ---------------------------------------------------------------------------
#
# These are plain ``dict`` aliases rather than strict schemas. They document the
# PascalCase CloudFormation shapes the protocol exchanges without forcing a
# rigid structure on the fake or the real adapter.

#: A stack summary as returned by ``list_stacks`` (``StackSummaries`` entry).
#: Keys: ``StackName``, ``StackId``, ``StackStatus``, and optionally ``Region``.
StackSummary = dict[str, Any]

#: A full stack description as returned by ``describe_stacks`` (``Stacks``
#: entry). Keys: ``StackName``, ``StackId``, ``StackStatus``, ``Tags``
#: (list of ``{"Key", "Value"}``), and optionally ``Region``.
StackDescription = dict[str, Any]

#: A per-resource drift record (``StackResourceDrifts`` entry). Keys:
#: ``LogicalResourceId``, ``PhysicalResourceId``, ``ResourceType``,
#: ``StackResourceDriftStatus``, ``PropertyDifferences``.
ResourceDriftRecord = dict[str, Any]


@runtime_checkable
class CfnClient(Protocol):
    """Protocol for the CloudFormation operations the drift manager needs.

    All methods mirror the corresponding boto3 CloudFormation client calls and
    exchange CloudFormation-style PascalCase dictionaries. Implementations may
    raise exceptions to model API errors; callers (detector / reconciler) map
    those to ``Unknown`` / ``Failed`` outcomes.
    """

    # -- Stack inventory -----------------------------------------------------

    def list_stacks(
        self, *, NextToken: Optional[str] = None, **kwargs: Any
    ) -> dict[str, Any]:
        """List stacks in the account/region.

        Returns a dict with ``StackSummaries`` (a list of :data:`StackSummary`)
        and an optional ``NextToken`` for pagination.
        """
        ...

    def describe_stacks(
        self, *, StackName: Optional[str] = None, NextToken: Optional[str] = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Describe one stack (``StackName`` given) or all stacks.

        Returns a dict with ``Stacks`` (a list of :data:`StackDescription`) and
        an optional ``NextToken``.
        """
        ...

    # -- Drift detection -----------------------------------------------------

    def detect_stack_drift(self, *, StackName: str, **kwargs: Any) -> dict[str, Any]:
        """Initiate asynchronous drift detection for ``StackName``.

        Returns a dict with ``StackDriftDetectionId`` used to poll status.
        """
        ...

    def describe_stack_drift_detection_status(
        self, *, StackDriftDetectionId: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Poll the status of a drift detection operation.

        Returns a dict with at least ``StackId``, ``StackDriftDetectionId``,
        ``DetectionStatus`` (``DETECTION_IN_PROGRESS`` / ``DETECTION_FAILED`` /
        ``DETECTION_COMPLETE``) and, once meaningful, ``StackDriftStatus``
        (``DRIFTED`` / ``IN_SYNC`` / ``UNKNOWN`` / ``NOT_CHECKED``),
        ``DriftedStackResourceCount``, and ``DetectionStatusReason``.
        """
        ...

    def describe_stack_resource_drifts(
        self,
        *,
        StackName: str,
        NextToken: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return per-resource drift for a stack whose detection completed.

        Paginated via ``NextToken``. Returns a dict with
        ``StackResourceDrifts`` (a list of :data:`ResourceDriftRecord`) and an
        optional ``NextToken`` for the next page.
        """
        ...

    # -- Reconciliation ------------------------------------------------------

    def create_change_set(
        self,
        *,
        StackName: str,
        ChangeSetName: str,
        ChangeSetType: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a change set.

        ``ChangeSetType=IMPORT`` is used for adopt-actual (resource import);
        a plain update change set re-asserts the template for align-to-template.
        Returns a dict with ``Id`` and ``StackId``.
        """
        ...

    def execute_change_set(
        self, *, ChangeSetName: str, StackName: Optional[str] = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Execute a previously created change set."""
        ...

    def update_stack(self, *, StackName: str, **kwargs: Any) -> dict[str, Any]:
        """Update a stack directly (align-to-template reassert path).

        Returns a dict with ``StackId``.
        """
        ...
