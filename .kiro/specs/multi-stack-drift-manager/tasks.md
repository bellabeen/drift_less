# Implementation Plan: Multi-Stack Drift Manager

## Overview

This plan implements the Multi-Stack Drift Manager in Python, building bottom-up: shared data models and a mockable CloudFormation client adapter first, then each component (Stack_Selector, Drift_Detector, Drift_Reporter, Reconciler, Progress_Tracker) with its property-based tests, then wiring through the Drift_Manager orchestrator, and finally thin integration tests over the mocked CloudFormation boundary.

Property-based tests use the Hypothesis library. Each property test runs a minimum of 100 iterations (`@settings(max_examples=100)`) and is tagged with a comment in the format `Feature: multi-stack-drift-manager, Property N: {property_text}`. A fake clock and a fake/mock CloudFormation client are used so timing, polling cadence, concurrency bounds, and gating can be asserted deterministically without AWS.

Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP; core implementation tasks are never optional.

## Tasks

- [x] 1. Set up project structure, data models, and the CloudFormation client boundary
  - [x] 1.1 Create package layout and testing/toolchain setup
    - Create `src/drift_manager/` package with `__init__.py` and module stubs: `models.py`, `cfn_client.py`, `selector.py`, `detector.py`, `reporter.py`, `reconciler.py`, `progress.py`, `manager.py`
    - Create `tests/` with `conftest.py` and a `fakes.py` module for the fake clock and fake CloudFormation client
    - Add `pyproject.toml` (or `requirements-dev.txt`) declaring `hypothesis` and `pytest` as dev dependencies; pin exact versions
    - _Requirements: 1.1, 2.1, 3.1, 4.1, 5.1, 6.1_

  - [x] 1.2 Implement core data models
    - Implement `SelectionCriterion` as `Names` / `Tags` / `Region` variants, `DriftStatus` enum (`DRIFTED`, `IN_SYNC`, `NOT_CHECKED`, `UNKNOWN`), `ResourceDrift`, `PropertyDifference` (with an explicit `ABSENT` marker and `DifferenceType` = `ADD`/`REMOVE`/`NOT_EQUAL`), `OperationSummary`, and `SummaryItem` as immutable dataclasses
    - Implement the detection/report/reconciliation result containers: `StackDetectionOutcome`, `DetectionResults`, `StackReportEntry`, `DriftReport`, `ReconciliationOutcome`, `ReconciliationResults`, `ProgressSnapshot`
    - _Requirements: 1.1, 3.5, 3.6, 4.7, 5.1, 6.3_

  - [x]* 1.3 Write unit tests for data model construction and the ABSENT marker
    - Verify `PropertyDifference` marks expected absent for ADD-only and actual absent for REMOVE-only
    - _Requirements: 3.5_

  - [x] 1.4 Define the CloudFormation client adapter interface and in-memory fake
    - Define a `CfnClient` protocol/ABC in `cfn_client.py` with the methods used by the design: `list_stacks`, `describe_stacks`, `detect_stack_drift`, `describe_stack_drift_detection_status`, `describe_stack_resource_drifts` (paginated via `NextToken`), `create_change_set`, `execute_change_set`, `update_stack`
    - Implement `FakeCfnClient` in `tests/fakes.py` backed by a scripted stack inventory and scripted detection/reconciliation responses, driven by a `FakeClock`; record call counts, call order, and timestamps so cadence, gating, and concurrency can be asserted
    - _Requirements: 2.2, 2.3, 4.5, 4.6_

- [x] 2. Implement Stack_Selector
  - [x] 2.1 Implement selection resolution and confirmation gating
    - Implement `resolve(criterion)` returning `Resolved` / `EmptySelection` / `FailedSelection` / `RejectedSelection`
    - Names: exact match, record unmatched names in `unmatchedNames`; Tags: AND, exact, case-sensitive; Region: all stacks in region, invalid/inaccessible → `FailedSelection`; validate zero-names-and-zero-filters before resolution → `RejectedSelection`; bound resolved set to at most 500; zero matches → `EmptySelection`
    - Implement `confirm(resolved)` → `ConfirmedSet` and `cancel(resolved)` → `Aborted`, exposing a gate the detector must respect
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10, 1.11_

  - [x]* 2.2 Write property test for explicit-name resolution
    - **Property 1: Explicit names resolve to exact matches**
    - **Validates: Requirements 1.1, 1.5**

  - [x]* 2.3 Write property test for tag-filter resolution
    - **Property 2: Tag filters match by exact case-sensitive AND semantics**
    - **Validates: Requirements 1.2**

  - [x]* 2.4 Write property test for region resolution
    - **Property 3: Region criterion resolves to all stacks in the region**
    - **Validates: Requirements 1.3**

  - [x]* 2.5 Write property test for the 500-stack bound
    - **Property 4: Resolution never exceeds 500 stacks**
    - **Validates: Requirements 1.4**

  - [x]* 2.6 Write property test for confirmation gating
    - **Property 5: Detection is gated on confirmation**
    - **Validates: Requirements 1.9, 1.11**

  - [x]* 2.7 Write unit tests for selection edge cases
    - 1.6 zero names and zero filters → rejected with message; 1.7 invalid/inaccessible region → `FailedSelection` with reason; 1.8 zero matches → `EmptySelection`; 1.10 confirm → detection allowed on resolved set
    - _Requirements: 1.6, 1.7, 1.8, 1.10_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement Progress_Tracker
  - [x] 4.1 Implement progress observation and failure recording
    - Implement `observe(operation)` → `ProgressSnapshot` with `total`, `completed`, `remaining`, `failures`, maintaining `completed + remaining == total`
    - Record per-item failures, include them in the snapshot, and keep the operation running; expose completion plus a succeeded/failed summary; drive timing with the `FakeClock` so updates reflect changes within 2s
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [x]* 4.2 Write property test for progress count conservation
    - **Property 21: Progress conserves the item total**
    - **Validates: Requirements 5.1, 5.2**

  - [x]* 4.3 Write unit test for 2-second update reflection
    - 5.3 status change reflected in progress within 2s using the fake clock
    - _Requirements: 5.3_

- [x] 5. Implement Drift_Detector
  - [x] 5.1 Implement the per-stack detection state machine
    - Implement the state machine over `Pending → Detecting → Completed/Unknown/Skipped`: initiate `detect_stack_drift`; poll `describe_stack_drift_detection_status` every 5s via the fake clock; on `DETECTION_COMPLETE` fetch paginated `describe_stack_resource_drifts` and record `StackDriftStatus` + drifted resources; on `DETECTION_FAILED` or initiation error record `Unknown(reason)`; on elapsed ≥ 300s record `Unknown(timeout)` and stop; if stack state disallows detection record `Skipped(reason)` without initiating; make terminal states final (no further polling)
    - _Requirements: 2.2, 2.3, 2.4, 2.5, 2.6_

  - [x] 5.2 Implement the bounded-concurrency worker pool and completion signal
    - Drive detection for a `ConfirmedSet` with max concurrency 10, admitting the next pending stack only when a slot is released at a terminal outcome; emit per-item progress to Progress_Tracker; signal bulk completion when every stack has exactly one terminal outcome
    - _Requirements: 2.1, 2.7_

  - [x]* 5.3 Write property test for the concurrency bound
    - **Property 6: Concurrency never exceeds the bound**
    - **Validates: Requirements 2.1**

  - [x]* 5.4 Write property test for completed-status recording
    - **Property 7: Completed detection records the reported stack status**
    - **Validates: Requirements 2.3**

  - [x]* 5.5 Write property test for terminal UNKNOWN on non-completion
    - **Property 8: Non-completion yields a terminal UNKNOWN with reason**
    - **Validates: Requirements 2.4, 2.6**

  - [x]* 5.6 Write property test for skipped-stack handling
    - **Property 9: Detection-disallowed stacks are skipped without initiation**
    - **Validates: Requirements 2.5**

  - [x]* 5.7 Write property test for bulk-detection termination
    - **Property 10: Bulk detection terminates with exactly one terminal outcome per stack**
    - **Validates: Requirements 2.7**

  - [x]* 5.8 Write unit test for 5s poll cadence
    - 2.2 verify 5s polling cadence with the fake clock until completion
    - _Requirements: 2.2_

  - [x] 5.9 Implement bounded retry with backoff for transient CloudFormation errors
    - Add a shared retry helper (e.g. `retry.py`) that retries throttling/transient errors from `CfnClient` calls with bounded exponential backoff, sleeping via the injected clock; non-transient errors are not retried
    - Apply it to `detect_stack_drift`, `describe_stack_drift_detection_status`, and `describe_stack_resource_drifts` in `StackDetection`; retries must stay within the 300s per-stack timeout window, and exhaustion records `Unknown(reason)` with the last error
    - _Requirements: 2.4, 2.6_

  - [x]* 5.10 Write unit tests for retry and backoff
    - Throttled-then-success call completes detection; exhausted retries yield `Unknown(reason)`; non-transient errors are not retried; backoff never extends past the 300s timeout (fake clock)
    - _Requirements: 2.4, 2.6_

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement Drift_Reporter
  - [x] 7.1 Implement consolidated report aggregation
    - Implement `report(results)` → `DriftReport`: one entry per stack with no omission/duplication, drifted-resource entries present iff status is `DRIFTED` (with resource id and type), property differences exposing expected/actual with the ABSENT marker, and the four status counts summing to the number of stacks; emit a descriptive empty message when there are no entries
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 7.2 Implement the machine-readable structured report
    - Implement `toMachineReadable(report)` → structured data (e.g. JSON) exposing each stack entry's drift status and drifted-resource entries, and each resource's id, type, and property differences, parseable without presentation formatting
    - _Requirements: 3.7_

  - [x] 7.3 Implement operation summaries for detection and reconciliation
    - Implement production of `OperationSummary` for detection (each stack with final drift status) and reconciliation (each targeted resource with succeeded/failed/skipped), with succeeded+failed+skipped == items and failed/skipped items carrying identifier and reason; on summary-production failure present an error indication while preserving recorded per-item outcomes
    - _Requirements: 4.9, 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x]* 7.4 Write property test for report bijection over the resolved set
    - **Property 11: Report is a bijection over the resolved set**
    - **Validates: Requirements 3.1, 6.1**

  - [x]* 7.5 Write property test for conditional drifted-resource listing
    - **Property 12: Drifted resources are shown if and only if the stack is DRIFTED**
    - **Validates: Requirements 3.3, 3.4**

  - [x]* 7.6 Write property test for property-difference absence markers
    - **Property 13: Property differences expose expected/actual with correct absence marker**
    - **Validates: Requirements 3.5**

  - [x]* 7.7 Write property test for status-count conservation
    - **Property 14: Status counts conserve the stack total**
    - **Validates: Requirements 3.6**

  - [x]* 7.8 Write property test for machine-readable round-trip
    - **Property 15: Machine-readable report round-trips report data**
    - **Validates: Requirements 3.7**

  - [x]* 7.9 Write property test for operation-summary conservation
    - **Property 22: Operation summaries conserve the item total**
    - **Validates: Requirements 4.9, 5.5, 6.2, 6.3**

  - [x]* 7.10 Write property test for failed/skipped summary identifiers and reasons
    - **Property 23: Failed and skipped summary items carry identifier and reason**
    - **Validates: Requirements 6.4**

  - [x]* 7.11 Write unit tests for reporter edge cases
    - 3.2 empty resolved set → empty report with message; 6.5 summary production failure → error indication, outcomes preserved
    - _Requirements: 3.2, 6.5_

- [x] 8. Implement Reconciler
  - [x] 8.1 Implement scope recording, invalidation, and confirmation gating
    - Implement `setScope(resources[1..500])` → `Scope`; `confirmScope(scope, currentSelection)` returning `ConfirmedScope` / `ScopeInvalidated` (when the selection changed) / `RejectedEmpty` (empty scope, retain prior scope); forbid applying any action before confirmation
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.10_

  - [x] 8.2 Implement action translation with continue-on-failure
    - Implement `reconcile(scope, action)`: `AlignToTemplate` → `update_stack` / template-reassert change set; `AdoptActual` → `create_change_set(ChangeSetType=IMPORT)` + `execute_change_set`; on success set resource drift status to in-sync; apply the action exactly once per scoped resource; on failure record `Failed(reason)`, leave prior drift status, continue remaining resources, and surface the failure to Progress_Tracker; produce `ReconciliationResults` with total/succeeded/failed
    - Wrap reconciliation `CfnClient` calls with the retry helper from 5.9; exhaustion records `Failed(reason)`
    - _Requirements: 4.5, 4.6, 4.7, 4.8, 4.9_

  - [x]* 8.3 Write property test for scope recording
    - **Property 16: Selected resources are recorded as the reconciliation scope**
    - **Validates: Requirements 4.1**

  - [x]* 8.4 Write property test for scope invalidation on change
    - **Property 17: Changing the selection invalidates the recorded scope**
    - **Validates: Requirements 4.3**

  - [x]* 8.5 Write property test for confirmation gating and exactly-once coverage
    - **Property 18: Reconciliation is gated on confirmation and covers every scoped resource**
    - **Validates: Requirements 4.4, 4.10**

  - [x]* 8.6 Write property test for successful reconciliation setting in-sync
    - **Property 19: Successful reconciliation sets the resource to in-sync**
    - **Validates: Requirements 4.5, 4.6**

  - [x]* 8.7 Write property test for isolated failures and continuation
    - **Property 20: Failures are isolated and processing continues**
    - **Validates: Requirements 4.7, 4.8, 5.4**

  - [x]* 8.8 Write unit test for empty-scope confirmation rejection
    - 4.2 empty scope confirmed → rejected, scope unchanged
    - _Requirements: 4.2_

- [x] 9. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Wire components through the Drift_Manager orchestrator
  - [x] 10.1 Implement the orchestrator and phase flow
    - Implement `Drift_Manager` holding operation state and enforcing the confirmation gates: selection → confirm → bulk detection (via Drift_Detector + Progress_Tracker) → consolidated report (via Drift_Reporter) → scope selection → confirm → reconciliation (via Reconciler + Progress_Tracker) → operation summary
    - Inject the `CfnClient` and clock dependencies so the orchestrator runs against the fake in tests and a real adapter in production
    - _Requirements: 1.9, 1.10, 1.11, 2.7, 3.1, 4.4, 4.10, 5.5, 6.1, 6.2_

  - [x]* 10.2 Write unit test for end-to-end gate enforcement
    - Verify detection does not start before confirmation and reconciliation does not start before scope confirmation, using the fake client's call records
    - _Requirements: 1.9, 4.10_

- [x] 11. Integration tests over the mocked CloudFormation boundary
  - [ ]* 11.1 Write detect → report → reconcile happy-path integration test
    - Drive a small stack set through the full pipeline against `FakeCfnClient`, asserting wiring to `detect_stack_drift`, `describe_stack_drift_detection_status`, and `describe_stack_resource_drifts`
    - _Requirements: 2.1, 2.7, 3.1, 4.4_

  - [x]* 11.2 Write adopt-actual (resource import) integration test
    - Reconcile with `AdoptActual`, asserting the `create_change_set(ChangeSetType=IMPORT)` + `execute_change_set` path and resulting in-sync status
    - _Requirements: 4.6_

  - [ ]* 11.3 Write align-to-template (update) integration test
    - Reconcile with `AlignToTemplate`, asserting the `update_stack` / template-reassert change set path and resulting in-sync status
    - _Requirements: 4.5_

- [ ] 12. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each task references specific requirements for traceability, and each property test task references the exact design property it validates.
- All 23 correctness properties from the design are covered: Properties 1–5 (Stack_Selector), 6–10 (Drift_Detector), 11–15 and 22–23 (Drift_Reporter), 16–20 (Reconciler), 21 (Progress_Tracker).
- Property-based tests use Hypothesis with a minimum of 100 iterations each and the required `Feature: multi-stack-drift-manager, Property N` tag; a fake clock and fake CloudFormation client make timing, polling cadence, concurrency bounds, and gating deterministic.
- Checkpoints ensure incremental validation after each major component.
- Task 5.9 covers the design's error-handling row for transient CloudFormation errors (bounded retry with backoff); the Reconciler reuses the same helper in 8.2.
- The dependency graph includes the checkpoint tasks, and each checkpoint waits on every task in its section.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "1.4"] },
    { "id": 3, "tasks": ["2.1", "4.1"] },
    { "id": 4, "tasks": ["2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "4.2", "4.3", "5.1"] },
    { "id": 5, "tasks": ["3", "5.2", "5.4", "5.5", "5.6", "5.8"] },
    { "id": 6, "tasks": ["5.3", "5.7", "5.9"] },
    { "id": 7, "tasks": ["5.10"] },
    { "id": 8, "tasks": ["6"] },
    { "id": 9, "tasks": ["7.1", "8.1"] },
    { "id": 10, "tasks": ["7.2", "7.3", "7.4", "7.5", "7.6", "7.7", "7.11", "8.2", "8.3", "8.4", "8.8"] },
    { "id": 11, "tasks": ["7.8", "7.9", "7.10", "8.5", "8.6", "8.7"] },
    { "id": 12, "tasks": ["9"] },
    { "id": 13, "tasks": ["10.1"] },
    { "id": 14, "tasks": ["10.2", "11.1", "11.2", "11.3"] },
    { "id": 15, "tasks": ["12"] }
  ]
}
```
