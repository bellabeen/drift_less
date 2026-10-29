"""Drift_Reporter: aggregates detection/reconciliation results into reports.

Task 7.1 implements :func:`report` (consolidated report aggregation).
Task 7.2 adds the machine-readable structured form (:func:`to_machine_readable`
/ :func:`to_json`) and its inverse (:func:`from_machine_readable` /
:func:`from_json`).

Outcome → DriftStatus mapping used by the consolidated report:

* ``Detected(status, ...)`` → the recorded ``status`` (Requirement 2.3).
* ``Unknown(reason)``       → ``DriftStatus.UNKNOWN`` (detection failed or
  did not complete; Requirement 2.4).
* ``Skipped(reason)``       → ``DriftStatus.NOT_CHECKED`` (detection was never
  initiated because the stack's state disallowed it; Requirement 2.5).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from typing import Any, Mapping

from drift_manager.models import (
    ABSENT,
    Detected,
    DetectionOutcome,
    DetectionResults,
    DifferenceType,
    DriftReport,
    DriftStatus,
    PropertyDifference,
    ResourceDrift,
    ResourceDriftStatus,
    Skipped,
    StackDetectionOutcome,
    StackReportEntry,
    StatusCounts,
    Unknown,
)

#: Message emitted when the resolved set contains zero stacks (Requirement 3.2).
EMPTY_REPORT_MESSAGE = "No stacks were reported: the resolved stack set is empty."


# ---------------------------------------------------------------------------
# Outcome mapping helpers
# ---------------------------------------------------------------------------


def outcome_status(outcome: DetectionOutcome) -> DriftStatus:
    """Map a per-stack detection outcome to its stack-level DriftStatus."""
    if isinstance(outcome, Detected):
        return outcome.status
    if isinstance(outcome, Unknown):
        return DriftStatus.UNKNOWN
    if isinstance(outcome, Skipped):
        return DriftStatus.NOT_CHECKED
    raise TypeError(f"Unsupported detection outcome: {outcome!r}")


def _normalize_difference(diff: PropertyDifference) -> PropertyDifference:
    """Ensure the ABSENT marker is applied for ADD-only / REMOVE-only diffs.

    ADD means the value exists only in the actual configuration, so the
    expected value is ABSENT; REMOVE means it exists only in the template, so
    the actual value is ABSENT (Requirement 3.5).
    """
    if diff.differenceType is DifferenceType.ADD and diff.expectedValue is not ABSENT:
        return replace(diff, expectedValue=ABSENT)
    if diff.differenceType is DifferenceType.REMOVE and diff.actualValue is not ABSENT:
        return replace(diff, actualValue=ABSENT)
    return diff


def _normalize_resource(resource: ResourceDrift) -> ResourceDrift:
    diffs = tuple(_normalize_difference(d) for d in resource.propertyDifferences)
    if diffs == resource.propertyDifferences:
        return resource
    return replace(resource, propertyDifferences=diffs)


def _build_entry(stack: StackDetectionOutcome) -> StackReportEntry:
    """Build one report entry; drifted resources only when status is DRIFTED."""
    status = outcome_status(stack.outcome)
    resources: tuple[ResourceDrift, ...] = ()
    if status is DriftStatus.DRIFTED and isinstance(stack.outcome, Detected):
        resources = tuple(_normalize_resource(r) for r in stack.outcome.driftedResources)
    return StackReportEntry(
        stackName=stack.stackName, status=status, driftedResources=resources
    )


def _count_statuses(entries: tuple[StackReportEntry, ...]) -> StatusCounts:
    counter = Counter(entry.status for entry in entries)
    return StatusCounts(
        DRIFTED=counter.get(DriftStatus.DRIFTED, 0),
        IN_SYNC=counter.get(DriftStatus.IN_SYNC, 0),
        NOT_CHECKED=counter.get(DriftStatus.NOT_CHECKED, 0),
        UNKNOWN=counter.get(DriftStatus.UNKNOWN, 0),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def report(results: DetectionResults) -> DriftReport:
    """Aggregate bulk detection results into a consolidated DriftReport.

    * One entry per outcome, in input order — no stack omitted or duplicated
      (Requirement 3.1). ``DetectionResults`` already holds exactly one outcome
      per stack in the resolved set.
    * Drifted-resource entries are present iff the status is DRIFTED
      (Requirements 3.3, 3.4), with property differences carrying ABSENT
      markers for ADD/REMOVE (Requirement 3.5).
    * The four status counts sum to the number of stacks (Requirement 3.6).
    * An empty resolved set yields no entries and a descriptive message
      (Requirement 3.2).
    """
    entries = tuple(_build_entry(stack) for stack in results.outcomes)
    return DriftReport(
        entries=entries,
        counts=_count_statuses(entries),
        emptyMessage=EMPTY_REPORT_MESSAGE if not entries else None,
    )


# ---------------------------------------------------------------------------
# Machine-readable structured report (task 7.2, Requirement 3.7)
# ---------------------------------------------------------------------------
#
# Structured form (JSON-compatible dict):
#
#   {
#     "schemaVersion": 1,
#     "stacks": [
#       {
#         "stackName": str,
#         "driftStatus": "DRIFTED" | "IN_SYNC" | "NOT_CHECKED" | "UNKNOWN",
#         "driftedResources": [
#           {
#             "logicalResourceId": str,
#             "physicalResourceId": str | null,
#             "resourceType": str,
#             "resourceDriftStatus": str,
#             "propertyDifferences": [
#               {
#                 "propertyPath": str,
#                 "differenceType": "ADD" | "REMOVE" | "NOT_EQUAL",
#                 "expectedValue": {"absent": true} | {"absent": false, "value": any},
#                 "actualValue":   {"absent": true} | {"absent": false, "value": any}
#               }
#             ]
#           }
#         ]
#       }
#     ],
#     "counts": {"DRIFTED": int, "IN_SYNC": int, "NOT_CHECKED": int, "UNKNOWN": int},
#     "emptyMessage": str | null
#   }
#
# Property values are wrapped so that the ABSENT marker (``{"absent": true}``)
# is unambiguous and distinct from a genuine JSON ``null`` value
# (``{"absent": false, "value": null}``).

#: Version of the structured report schema produced by to_machine_readable.
SCHEMA_VERSION = 1


def _encode_value(value: Any) -> dict[str, Any]:
    if value is ABSENT:
        return {"absent": True}
    return {"absent": False, "value": value}


def _decode_value(data: Mapping[str, Any]) -> Any:
    if not isinstance(data, Mapping) or "absent" not in data:
        raise ValueError(f"Malformed property value: {data!r}")
    if data["absent"] is True:
        return ABSENT
    if data["absent"] is False and "value" in data:
        return data["value"]
    raise ValueError(f"Malformed property value: {data!r}")


def _encode_difference(diff: PropertyDifference) -> dict[str, Any]:
    return {
        "propertyPath": diff.propertyPath,
        "differenceType": diff.differenceType.value,
        "expectedValue": _encode_value(diff.expectedValue),
        "actualValue": _encode_value(diff.actualValue),
    }


def _encode_resource(resource: ResourceDrift) -> dict[str, Any]:
    return {
        "logicalResourceId": resource.logicalResourceId,
        "physicalResourceId": resource.physicalResourceId,
        "resourceType": resource.resourceType,
        "resourceDriftStatus": resource.resourceDriftStatus.value,
        "propertyDifferences": [
            _encode_difference(d) for d in resource.propertyDifferences
        ],
    }


def _encode_entry(entry: StackReportEntry) -> dict[str, Any]:
    return {
        "stackName": entry.stackName,
        "driftStatus": entry.status.value,
        "driftedResources": [_encode_resource(r) for r in entry.driftedResources],
    }


def to_machine_readable(drift_report: DriftReport) -> dict[str, Any]:
    """Convert a DriftReport into JSON-compatible structured data.

    Each stack entry exposes its stack-level drift status and drifted-resource
    entries; each drifted resource exposes its identifier, type, and
    property-level differences (Requirement 3.7). The result contains only
    dicts, lists, strings, numbers, booleans and ``None`` (plus the original
    property values), so it can be serialized with :func:`json.dumps`.
    """
    counts = drift_report.counts
    return {
        "schemaVersion": SCHEMA_VERSION,
        "stacks": [_encode_entry(e) for e in drift_report.entries],
        "counts": {
            "DRIFTED": counts.DRIFTED,
            "IN_SYNC": counts.IN_SYNC,
            "NOT_CHECKED": counts.NOT_CHECKED,
            "UNKNOWN": counts.UNKNOWN,
        },
        "emptyMessage": drift_report.emptyMessage,
    }


def to_json(drift_report: DriftReport, *, indent: int | None = None) -> str:
    """Serialize a DriftReport to a JSON string via :func:`to_machine_readable`."""
    return json.dumps(to_machine_readable(drift_report), indent=indent)


def _require(data: Mapping[str, Any], key: str) -> Any:
    if not isinstance(data, Mapping) or key not in data:
        raise ValueError(f"Malformed structured report: missing {key!r}")
    return data[key]


def _decode_difference(data: Mapping[str, Any]) -> PropertyDifference:
    return PropertyDifference(
        propertyPath=_require(data, "propertyPath"),
        expectedValue=_decode_value(_require(data, "expectedValue")),
        actualValue=_decode_value(_require(data, "actualValue")),
        differenceType=DifferenceType(_require(data, "differenceType")),
    )


def _decode_resource(data: Mapping[str, Any]) -> ResourceDrift:
    return ResourceDrift(
        logicalResourceId=_require(data, "logicalResourceId"),
        resourceType=_require(data, "resourceType"),
        resourceDriftStatus=ResourceDriftStatus(_require(data, "resourceDriftStatus")),
        propertyDifferences=tuple(
            _decode_difference(d) for d in _require(data, "propertyDifferences")
        ),
        physicalResourceId=data.get("physicalResourceId"),
    )


def _decode_entry(data: Mapping[str, Any]) -> StackReportEntry:
    return StackReportEntry(
        stackName=_require(data, "stackName"),
        status=DriftStatus(_require(data, "driftStatus")),
        driftedResources=tuple(
            _decode_resource(r) for r in _require(data, "driftedResources")
        ),
    )


def from_machine_readable(data: Mapping[str, Any]) -> DriftReport:
    """Reconstruct a DriftReport from its structured form.

    Inverse of :func:`to_machine_readable`. Raises ``ValueError`` on malformed
    input or an unsupported schema version.
    """
    version = _require(data, "schemaVersion")
    if version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported structured report schema version: {version!r}")
    counts = _require(data, "counts")
    return DriftReport(
        entries=tuple(_decode_entry(e) for e in _require(data, "stacks")),
        counts=StatusCounts(
            DRIFTED=_require(counts, "DRIFTED"),
            IN_SYNC=_require(counts, "IN_SYNC"),
            NOT_CHECKED=_require(counts, "NOT_CHECKED"),
            UNKNOWN=_require(counts, "UNKNOWN"),
        ),
        emptyMessage=data.get("emptyMessage"),
    )


def from_json(text: str) -> DriftReport:
    """Parse a JSON string produced by :func:`to_json` back into a DriftReport."""
    return from_machine_readable(json.loads(text))
