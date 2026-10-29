# Requirements Document

## Introduction

The Multi-Stack Drift Manager enables operators to detect configuration drift and reconcile drifted resources across many AWS CloudFormation stacks in a single bulk operation, instead of repeating drift detection and remediation one stack at a time. The feature provides bulk stack selection, bulk drift detection, consolidated drift reporting at the stack and resource level, and selective reconciliation of drifted resources across the chosen set of stacks, with progress tracking and summary reporting throughout.

## Glossary

- **Drift_Manager**: The system component that orchestrates bulk drift detection and reconciliation across multiple CloudFormation stacks.
- **Stack_Selector**: The component that resolves a user-supplied selection criterion into a concrete set of CloudFormation stacks.
- **Drift_Detector**: The component that initiates and tracks CloudFormation drift detection for each selected stack.
- **Drift_Reporter**: The component that aggregates and presents drift status and drifted-resource details.
- **Reconciler**: The component that applies or imports approved drift changes for selected resources.
- **Progress_Tracker**: The component that reports the real-time status of in-flight bulk operations.
- **CloudFormation Stack**: An AWS CloudFormation stack managed within a target AWS account and region.
- **Drift Status**: The CloudFormation-reported drift condition of a stack or resource (one of: `DRIFTED`, `IN_SYNC`, `NOT_CHECKED`, `UNKNOWN`).
- **Drifted Resource**: A stack resource whose actual configuration differs from its expected template configuration.
- **Reconciliation**: The act of resolving a drifted resource so that stack state and actual state agree, via applying template changes or importing actual configuration.
- **Selection Criterion**: A rule used to choose stacks, expressed as explicit stack names, resource tag filters, or all stacks in a region.
- **Operation Summary**: A consolidated end-of-run report describing per-stack and aggregate outcomes.

## Requirements

### Requirement 1: Bulk Stack Selection

**User Story:** As a cloud operator, I want to select multiple CloudFormation stacks by name, tag, or region scope, so that I can target a batch of stacks in one operation instead of handling each stack individually.

#### Acceptance Criteria

1. WHEN a user submits a Selection Criterion consisting of between 1 and 500 explicit stack names, THE Stack_Selector SHALL resolve the criterion to the set of stacks whose names exactly match the submitted names.
2. WHEN a user submits a Selection Criterion consisting of between 1 and 500 resource tag filters, THE Stack_Selector SHALL resolve the criterion to the set of stacks for which every submitted tag filter matches a stack tag by exact, case-sensitive key and value (AND semantics across all tag filters).
3. WHEN a user submits a Selection Criterion specifying all stacks in a region, THE Stack_Selector SHALL resolve the criterion to the set of all stacks in the specified region.
4. THE Stack_Selector SHALL resolve a Selection Criterion to at most 500 stacks.
5. IF a user submits a Selection Criterion consisting of explicit stack names and a submitted stack name does not correspond to an existing stack, THEN THE Stack_Selector SHALL exclude the unmatched name from the resolved set and record the unmatched name in the Operation Summary.
6. IF a user submits a Selection Criterion that contains zero stack names and zero tag filters, THEN THE Stack_Selector SHALL reject the Selection Criterion and indicate that at least one stack name or tag filter must be provided.
7. IF the region specified in a Selection Criterion is invalid or inaccessible, THEN THE Stack_Selector SHALL return a failed-selection result and record the region error reason.
8. IF a Selection Criterion resolves to zero stacks, THEN THE Stack_Selector SHALL return an empty-selection result indicating that no stacks matched the Selection Criterion.
9. THE Stack_Selector SHALL present the resolved set of stacks to the user for confirmation before any drift detection begins.
10. WHEN a user confirms the resolved set of stacks, THE Stack_Selector SHALL allow drift detection to proceed on the resolved set.
11. IF a user cancels the resolved set of stacks, THEN THE Stack_Selector SHALL abort the operation without initiating drift detection.

### Requirement 2: Bulk Drift Detection

**User Story:** As a cloud operator, I want to run drift detection on all selected stacks at once, so that I can learn the drift status of my entire fleet without triggering each detection manually.

#### Acceptance Criteria

1. WHEN a user confirms a resolved set of stacks for drift detection, THE Drift_Detector SHALL initiate drift detection for each stack in the resolved set, processing a maximum of 10 stacks concurrently.
2. WHILE drift detection is in progress for a stack, THE Drift_Detector SHALL poll CloudFormation for the detection outcome at an interval of 5 seconds and retrieve the outcome once CloudFormation reports completion for that stack.
3. WHEN drift detection completes for a stack, THE Drift_Detector SHALL record the stack-level Drift Status for that stack.
4. IF drift detection for a stack fails, THEN THE Drift_Detector SHALL record a `UNKNOWN` Drift Status for that stack and store the failure reason.
5. WHERE a stack is in a state that CloudFormation does not permit drift detection on, THE Drift_Detector SHALL skip detection for that stack and record a skipped outcome with the reason.
6. IF drift detection for a stack does not reach completion within 300 seconds of initiation, THEN THE Drift_Detector SHALL stop waiting for that stack, record a `UNKNOWN` Drift Status, and store a timeout reason.
7. WHEN every stack in the resolved set has reached a recorded outcome of detected Drift Status, `UNKNOWN` Drift Status, or skipped outcome, THE Drift_Detector SHALL signal completion of the bulk detection operation.

### Requirement 3: Drift Status Reporting

**User Story:** As a cloud operator, I want a consolidated report of drift status and the specific drifted resources per stack, so that I can understand the scope of drift across my fleet in one place.

#### Acceptance Criteria

1. WHEN bulk drift detection completes, THE Drift_Reporter SHALL present every stack in the resolved set together with the recorded stack-level Drift Status for that stack, with one entry per stack and no stack omitted or duplicated.
2. IF the resolved set contains zero stacks, THEN THE Drift_Reporter SHALL present the consolidated report with no stack entries and a descriptive message indicating that no stacks were reported.
3. WHERE a stack has a Drift Status of `DRIFTED`, THE Drift_Reporter SHALL present every Drifted Resource recorded for that stack together with the resource identifier and the resource type.
4. WHERE a stack has a Drift Status other than `DRIFTED`, THE Drift_Reporter SHALL present no Drifted Resource entries for that stack.
5. WHERE a Drifted Resource has one or more property-level differences, THE Drift_Reporter SHALL present, for each differing property, the property identifier, the expected value, and the actual value, indicating absence of a value for a property that was only added or only removed.
6. WHEN presenting the consolidated report, THE Drift_Reporter SHALL display, for each of the four Drift Status values (`DRIFTED`, `IN_SYNC`, `NOT_CHECKED`, `UNKNOWN`), the count of stacks in the resolved set having that status, reporting a count of zero where no stack has that status, such that the sum of the four counts equals the number of stacks in the resolved set.
7. WHERE a user requests the report in a machine-readable format, THE Drift_Reporter SHALL produce the report as structured data in which each stack entry exposes its stack-level Drift Status and its Drifted Resource entries, and each Drifted Resource entry exposes its resource identifier, resource type, and property-level differences, parseable without reference to presentation formatting.

### Requirement 4: Selective Reconciliation of Drift

**User Story:** As a cloud operator, I want to selectively reconcile drifted resources across the selected stacks in one workflow, so that I can resolve drift in bulk rather than fixing each stack separately.

#### Acceptance Criteria

1. WHEN a user selects between 1 and 500 Drifted Resources for Reconciliation, THE Reconciler SHALL record the selected resources as the reconciliation scope.
2. IF a user attempts to confirm a reconciliation scope that contains zero Drifted Resources, THEN THE Reconciler SHALL reject the confirmation, retain the current scope unchanged, and present an indication that at least one Drifted Resource must be selected.
3. IF the set of selected Drifted Resources changes after the reconciliation scope is recorded and before confirmation, THEN THE Reconciler SHALL invalidate the recorded scope and require the user to re-confirm the updated scope before applying any action.
4. WHEN a user confirms the reconciliation scope, THE Reconciler SHALL apply the chosen reconciliation action to each selected Drifted Resource in the scope.
5. WHERE a user chooses to align actual configuration to the template, THE Reconciler SHALL update the drifted resource so that its actual configuration matches the stack template definition, and SHALL set the resource Drift Status to in-sync upon successful update.
6. WHERE a user chooses to adopt the actual configuration, THE Reconciler SHALL import the actual resource configuration into the stack template state, and SHALL set the resource Drift Status to in-sync upon successful import.
7. WHEN Reconciliation completes for a resource, THE Reconciler SHALL record the per-resource reconciliation outcome as either succeeded or failed, including the resource identifier and the applied reconciliation action.
8. IF Reconciliation for a resource fails, THEN THE Reconciler SHALL record a failed outcome with the failure reason, leave that resource's prior Drift Status unchanged, and continue Reconciliation for the remaining selected resources.
9. WHEN Reconciliation completes for all resources in the reconciliation scope, THE Reconciler SHALL produce an Operation Summary reporting the total count of selected resources, the count of succeeded outcomes, and the count of failed outcomes.
10. THE Reconciler SHALL require explicit user confirmation of the reconciliation scope before applying any reconciliation action to any Drifted Resource.

### Requirement 5: Progress Tracking

**User Story:** As a cloud operator, I want real-time progress for bulk detection and reconciliation, so that I can monitor long-running operations across many stacks.

#### Acceptance Criteria

1. WHILE a bulk drift detection operation is running, THE Progress_Tracker SHALL report the total count of stacks in the operation, the count of stacks completed, and the count of stacks remaining.
2. WHILE a bulk reconciliation operation is running, THE Progress_Tracker SHALL report the total count of resources in the operation, the count of resources completed, and the count of resources remaining.
3. WHEN the status of any stack or resource in an in-flight operation changes, THE Progress_Tracker SHALL update the reported progress to reflect the changed status within 2 seconds of the status change.
4. IF an individual stack or resource fails during a bulk operation, THEN THE Progress_Tracker SHALL record the failed stack or resource, continue processing the remaining stacks or resources, and include the failure in the reported progress.
5. WHEN a bulk operation completes, THE Progress_Tracker SHALL report the operation as complete and SHALL report an Operation Summary containing the count of stacks or resources that succeeded and the count that failed.

### Requirement 6: Operation Summary Reporting

**User Story:** As a cloud operator, I want a summary report after each bulk operation, so that I can review outcomes and identify stacks that need follow-up.

#### Acceptance Criteria

1. WHEN a bulk drift detection operation completes, THE Drift_Reporter SHALL produce an Operation Summary listing each CloudFormation Stack included in the operation with its final Drift Status.
2. WHEN a bulk reconciliation operation completes, THE Drift_Reporter SHALL produce an Operation Summary listing each Drifted Resource targeted for Reconciliation with its outcome as one of succeeded, failed, or skipped.
3. WHEN an Operation Summary is produced, THE Operation Summary SHALL include aggregate counts of succeeded outcomes, failed outcomes, and skipped outcomes, such that the sum of these counts equals the total number of items listed in the summary.
4. WHERE an operation produced failed or skipped outcomes, THE Operation Summary SHALL include, for each failed or skipped item, the item identifier and the recorded reason.
5. IF producing the Operation Summary fails, THEN THE Drift_Reporter SHALL present an error indication and SHALL preserve the recorded per-item outcomes.
