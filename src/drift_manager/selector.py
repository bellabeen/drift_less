"""Stack_Selector: resolves a selection criterion to a bounded stack set.

Implements the Stack_Selector component of the design (task 2.1): it turns a
user-supplied :class:`~drift_manager.models.SelectionCriterion` into a concrete,
bounded (1-500) set of CloudFormation stacks, then gates downstream drift
detection behind an explicit confirmation step.

Resolution rules (design "Stack_Selector"):
- **Names** - exact match; names with no matching stack are excluded from the
  resolved set and recorded in ``unmatchedNames``.
- **Tags** - AND semantics; a stack matches only when, for *every* filter, it
  carries a tag whose key and value both exactly (case-sensitively) equal the
  filter's key and value.
- **Region** - all stacks located in the region; an invalid or inaccessible
  region yields a :class:`FailedSelection`.
- Validation precedes resolution: a criterion with zero names and zero tag
  filters is rejected (:class:`RejectedSelection`).
- The resolved set is bounded to at most 500 stacks.
- Zero matches yield an :class:`EmptySelection`.

Confirmation gating (design Property 5, requirements 1.9-1.11):
- :meth:`StackSelector.resolve` performs no mutation and initiates no
  detection; it only reads the stack inventory through the injected
  :class:`~drift_manager.cfn_client.CfnClient`.
- A :class:`ConfirmedSet` - the token the Drift_Detector requires before it may
  begin - can only be produced by :meth:`StackSelector.confirm`. There is no
  other public constructor path (direct construction raises), and the detector
  must pass its input through :func:`require_confirmed`, so detection cannot
  start without confirmation.
- :meth:`StackSelector.cancel` returns :class:`Aborted` and never yields a
  :class:`ConfirmedSet`, so cancelling aborts the operation.

The selector depends only on the ``CfnClient`` protocol (``list_stacks`` /
``describe_stacks``), never on boto3 directly, so it runs against the in-memory
``FakeCfnClient`` in tests and a real adapter in production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

from .cfn_client import CfnClient
from .models import Names, Region, SelectionCriterion, Tags

#: Maximum number of stacks a selection criterion may resolve to.
MAX_RESOLVED_STACKS = 500


# ---------------------------------------------------------------------------
# Resolved stack record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stack:
    """A single resolved CloudFormation stack.

    Captures the inventory metadata needed by the confirmation step and the
    downstream detector. ``tags`` is an immutable key/value mapping represented
    as a tuple of pairs so the dataclass stays frozen and hashable.
    """

    name: str
    region: str
    status: str
    tags: tuple[tuple[str, str], ...] = ()
    stackId: Optional[str] = None

    def tag_value(self, key: str) -> Optional[str]:
        """Return the value for tag ``key`` (exact match) or ``None``."""
        for k, v in self.tags:
            if k == key:
                return v
        return None


# ---------------------------------------------------------------------------
# Selection results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """A successful resolution to 1-500 stacks.

    ``unmatchedNames`` records explicitly submitted stack names that did not
    correspond to an existing stack (names criterion only); it is empty for the
    tags and region criteria.
    """

    stacks: tuple[Stack, ...]
    unmatchedNames: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmptySelection:
    """The criterion resolved to zero stacks."""

    message: str


@dataclass(frozen=True)
class FailedSelection:
    """Resolution failed, e.g. an invalid or inaccessible region."""

    reason: str


@dataclass(frozen=True)
class RejectedSelection:
    """The criterion was rejected before resolution (zero names, zero filters)."""

    message: str


# The result of :meth:`StackSelector.resolve`.
SelectionResult = Union[Resolved, EmptySelection, FailedSelection, RejectedSelection]


# ---------------------------------------------------------------------------
# Confirmation gate tokens
# ---------------------------------------------------------------------------


#: Module-private capability proving a ``ConfirmedSet`` came from ``confirm``.
_CONFIRM_TOKEN = object()


@dataclass(frozen=True)
class ConfirmedSet:
    """A confirmed, detection-ready set of stacks (the detection gate token).

    This is the sole artifact the Drift_Detector accepts as evidence that the
    operator confirmed the resolved set. It can only be produced by
    :meth:`StackSelector.confirm`; ``resolve`` returns a :class:`Resolved`
    (which the detector does *not* accept) and ``cancel`` returns
    :class:`Aborted`. Direct construction is rejected (the private ``_token``
    must be the selector's capability), so requiring a ``ConfirmedSet``
    enforces design Property 5: no detection begins before confirmation, and
    none begins if the operator cancels.

    Detectors should call :func:`require_confirmed` on their input.
    """

    stacks: tuple[Stack, ...]
    unmatchedNames: tuple[str, ...] = ()
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _CONFIRM_TOKEN:
            raise ValueError(
                "ConfirmedSet can only be produced by StackSelector.confirm()"
            )


def require_confirmed(candidate: object) -> ConfirmedSet:
    """Gate for the Drift_Detector: return ``candidate`` only if it is confirmed.

    Raises :class:`TypeError` for anything that is not a :class:`ConfirmedSet`
    (e.g. an unconfirmed :class:`Resolved`, an :class:`Aborted`, or a rejected /
    empty / failed selection), so detection cannot start without confirmation.
    """
    if not isinstance(candidate, ConfirmedSet):
        raise TypeError(
            "drift detection requires a ConfirmedSet from StackSelector.confirm(); "
            f"got {type(candidate).__name__}"
        )
    return candidate


@dataclass(frozen=True)
class Aborted:
    """The operator cancelled the resolved set; no detection will be initiated."""

    message: str = "selection cancelled; no drift detection initiated"


# ---------------------------------------------------------------------------
# Stack_Selector
# ---------------------------------------------------------------------------


class StackSelector:
    """Resolves selection criteria and gates detection behind confirmation.

    Depends only on the injected :class:`~drift_manager.cfn_client.CfnClient`
    for stack inventory lookups.
    """

    def __init__(self, cfn: CfnClient) -> None:
        self._cfn = cfn

    # -- Resolution ----------------------------------------------------------

    def resolve(self, criterion: SelectionCriterion) -> SelectionResult:
        """Resolve ``criterion`` to a bounded stack set or an explanatory result.

        Dispatches on the criterion variant. Performs no mutation and never
        initiates drift detection - it only reads the stack inventory.
        """
        if isinstance(criterion, Names):
            return self._resolve_names(criterion)
        if isinstance(criterion, Tags):
            return self._resolve_tags(criterion)
        if isinstance(criterion, Region):
            return self._resolve_region(criterion)
        raise TypeError(f"unsupported selection criterion: {criterion!r}")

    def _resolve_names(self, criterion: Names) -> SelectionResult:
        # 1.6: validation precedes resolution.
        if len(criterion.names) == 0:
            return RejectedSelection(
                message="at least one stack name or tag filter must be provided"
            )

        inventory = {stack.name: stack for stack in self._load_inventory()}

        matched: list[Stack] = []
        unmatched: list[str] = []
        seen: set[str] = set()
        for name in criterion.names:
            stack = inventory.get(name)
            if stack is None:
                # 1.5: exclude the name, record it for the summary.
                if name not in seen:
                    unmatched.append(name)
            elif name not in seen:
                matched.append(stack)
            seen.add(name)

        return self._finalize(matched, unmatched_names=tuple(unmatched))

    def _resolve_tags(self, criterion: Tags) -> SelectionResult:
        # 1.6: validation precedes resolution.
        if len(criterion.filters) == 0:
            return RejectedSelection(
                message="at least one stack name or tag filter must be provided"
            )

        matched = [
            stack
            for stack in self._load_inventory()
            # 1.2: AND semantics, exact case-sensitive key and value.
            if all(stack.tag_value(f.key) == f.value for f in criterion.filters)
        ]
        return self._finalize(matched)

    def _resolve_region(self, criterion: Region) -> SelectionResult:
        # 1.7: an invalid or inaccessible region fails resolution.
        try:
            inventory = self._load_inventory(region=criterion.region)
        except Exception as exc:  # noqa: BLE001 - surface the region error reason
            return FailedSelection(
                reason=f"region {criterion.region!r} is invalid or inaccessible: {exc}"
            )

        # 1.3: all stacks in the specified region.
        matched = [stack for stack in inventory if stack.region == criterion.region]
        return self._finalize(matched)

    def _finalize(
        self, matched: list[Stack], *, unmatched_names: tuple[str, ...] = ()
    ) -> SelectionResult:
        """Apply the 500 bound and the empty-selection rule to ``matched``."""
        if len(matched) == 0:
            # 1.8: zero matches.
            return EmptySelection(
                message="no stacks matched the selection criterion"
            )
        # 1.4: bound the resolved set to at most 500 stacks.
        bounded = tuple(matched[:MAX_RESOLVED_STACKS])
        return Resolved(stacks=bounded, unmatchedNames=unmatched_names)

    # -- Confirmation gate ---------------------------------------------------

    def confirm(self, resolved: Resolved) -> ConfirmedSet:
        """Confirm a :class:`Resolved` set, producing the detection gate token.

        1.9 / 1.10: the resolved set is confirmed by the operator and detection
        is now allowed to proceed on exactly these stacks.
        """
        if not isinstance(resolved, Resolved):
            raise TypeError(
                "only a Resolved selection can be confirmed; "
                f"got {type(resolved).__name__}"
            )
        return ConfirmedSet(
            stacks=resolved.stacks,
            unmatchedNames=resolved.unmatchedNames,
            _token=_CONFIRM_TOKEN,
        )

    def cancel(self, resolved: Resolved) -> Aborted:
        """Cancel a :class:`Resolved` set; no detection is initiated.

        1.11: cancelling aborts the operation. No :class:`ConfirmedSet` is
        produced, so the detector can never run for this selection.
        """
        if not isinstance(resolved, Resolved):
            raise TypeError(
                "only a Resolved selection can be cancelled; "
                f"got {type(resolved).__name__}"
            )
        return Aborted()

    # -- Inventory loading ---------------------------------------------------

    def _load_inventory(self, *, region: Optional[str] = None) -> list[Stack]:
        """Load the stack inventory via the ``CfnClient``.

        Combines ``list_stacks`` (summaries: name, id, status, region) with
        ``describe_stacks`` (tags) and follows ``NextToken`` pagination. When a
        ``region`` is requested, it is forwarded to the client so an invalid or
        inaccessible region surfaces as an exception that
        :meth:`_resolve_region` maps to :class:`FailedSelection`.
        """
        summaries = self._paginate("list_stacks", "StackSummaries", region=region)
        descriptions = self._paginate("describe_stacks", "Stacks", region=region)

        tags_by_name: dict[str, tuple[tuple[str, str], ...]] = {}
        for desc in descriptions:
            name = desc.get("StackName")
            if name is None:
                continue
            tags_by_name[name] = tuple(
                (t.get("Key"), t.get("Value")) for t in desc.get("Tags", [])
            )

        stacks: list[Stack] = []
        for summary in summaries:
            name = summary.get("StackName")
            if name is None:
                continue
            stacks.append(
                Stack(
                    name=name,
                    region=summary.get("Region", ""),
                    status=summary.get("StackStatus", ""),
                    tags=tags_by_name.get(name, ()),
                    stackId=summary.get("StackId"),
                )
            )
        return stacks

    def _paginate(
        self, method_name: str, result_key: str, *, region: Optional[str] = None
    ) -> list[dict]:
        """Collect all pages from a paginated ``CfnClient`` list method."""
        method = getattr(self._cfn, method_name)
        items: list[dict] = []
        next_token: Optional[str] = None
        while True:
            kwargs: dict = {}
            if next_token is not None:
                kwargs["NextToken"] = next_token
            if region is not None:
                kwargs["Region"] = region
            response = method(**kwargs)
            items.extend(response.get(result_key, []))
            next_token = response.get("NextToken")
            if not next_token:
                break
        return items
