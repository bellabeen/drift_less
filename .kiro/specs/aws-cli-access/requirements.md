# Requirements Document

## Introduction

This feature adds a read-only command-line interface that runs the existing Multi-Stack Drift Manager against a real AWS account. An operator selects CloudFormation stacks in one account and one region, confirms the resolved stack set, runs drift detection, and receives a drift report. The CLI uses a boto3-backed adapter that implements the existing `CfnClient` protocol. The CLI only reads stack state and initiates drift detection; the CLI does not mutate live stacks. Reconciliation (adopt-actual and align-to-template) is out of scope and is not exposed by the CLI.

## Glossary

- **CLI**: The `drift-manager` command-line program, invocable through the `[project.scripts]` entry point and through `python -m drift_manager`.
- **Boto3_Adapter**: The read-only implementation of the `CfnClient` protocol (defined in `src/drift_manager/cfn_client.py`) that forwards calls to a boto3 CloudFormation client.
- **Drift_Manager**: The existing `DriftManager` orchestrator in `src/drift_manager/manager.py` that drives select, confirm, detect, and report phases.
- **Read_Method**: One of the `CfnClient` methods `list_stacks`, `describe_stacks`, `detect_stack_drift`, `describe_stack_drift_detection_status`, or `describe_stack_resource_drifts`.
- **Mutating_Method**: One of the `CfnClient` methods `create_change_set`, `execute_change_set`, or `update_stack`.
- **Credential_Chain**: The standard boto3 credential and configuration resolution order (environment variables, shared config and credentials files, instance or container role).
- **Selection_Criteria**: The stack selection inputs supported by the existing models: stack names (`Names`), tag filters (`Tags`), and region (`Region`).
- **Selection_Mode**: The single selection option used for a run: `--stack`, `--tag`, or `--all`.
- **Resolved_Stack_Set**: The set of stacks returned by the selector as a `Resolved` selection result, extended with Nested_Stack descendants as defined in Requirement 11; each stack appears in the Resolved_Stack_Set exactly once.
- **Directly_Selected_Stack**: A stack that matches the Selection_Criteria of the run, as opposed to a stack added only through nested-stack expansion.
- **Parent_Stack**: A stack that owns one or more `AWS::CloudFormation::Stack` resources, each of which is a Nested_Stack.
- **Nested_Stack**: A stack whose stack description carries a `ParentId` stack ARN identifying the Parent_Stack; also called a child stack.
- **Root_Stack**: A stack without a `ParentId`; the top of a Stack_Tree. The `RootId` of every Nested_Stack identifies the Root_Stack of the Stack_Tree.
- **Stack_Tree**: A Root_Stack together with all Nested_Stack descendants at every depth, linked by `ParentId`.
- **Descendant**: A Nested_Stack reachable from a given stack by following child links one or more levels down a Stack_Tree.
- **Orphan_Nested_Stack**: A Nested_Stack whose `ParentId` or `RootId` refers to a stack absent from the stack inventory returned in the Bound_Region.
- **Vanished_Stack**: A stack that the CLI requested when re-reading the selection to add Descendants (a Directly_Selected_Stack or one of its Descendants) and that the re-read did not return, because the stack was deleted between the selection reads.
- **Selection_Limit**: The existing selector bound of at most 500 stacks per resolved selection (`MAX_RESOLVED_STACKS`).
- **Confirmation_Prompt**: The interactive question that displays the Resolved_Stack_Set and asks the operator to approve detection.
- **Human_Report**: The human-readable drift report written by the CLI by default.
- **JSON_Report**: The machine-readable drift report produced by the existing reporter (`to_machine_readable`), extended by the CLI with the per-stack keys `parentStackName` and `rootStackName` defined in Requirement 11.11; `schemaVersion` is unchanged.
- **Progress_Output**: Status lines describing detection progress, written to stderr.
- **Exit_Code**: The process exit status returned by the CLI: 0 for all stacks in sync, 1 for drift found, 2 for errors, unknown or failed detections, `NOT_CHECKED` stacks, unmatched stack names, Vanished_Stacks, or a cancelled or aborted run.
- **NOT_CHECKED_Stack**: A stack in the Resolved_Stack_Set whose report status is `NOT_CHECKED`, either because the detector skipped it for its stack status or because CloudFormation reported it as not checked.
- **Bound_Region**: The single region resolved for a run, to which the Boto3_Adapter is bound.
- **AWS_Extra**: The `aws` optional dependency group in `pyproject.toml` that installs boto3.

## Requirements

### Requirement 1: CLI entry points and argument parsing

**User Story:** As an operator, I want a `drift-manager` command, so that I can check CloudFormation drift from a terminal or CI job.

#### Acceptance Criteria

1. THE CLI SHALL be invocable through a `[project.scripts]` console entry point named `drift-manager`.
2. THE CLI SHALL be invocable through `python -m drift_manager`.
3. THE CLI SHALL parse command-line arguments with the Python standard library `argparse` module.
4. THE CLI SHALL accept the options `--profile`, `--region`, `--yes`, `--format` with values `text` or `json`, and `--output FILE`.
5. THE CLI SHALL accept Selection_Criteria as the options `--stack` for one or more stack names, `--tag` for one or more tag filters in `KEY=VALUE` form, and `--all` for every live stack in the Bound_Region.
6. IF the operator supplies an invalid argument or an unsupported `--format` value, THEN THE CLI SHALL write a usage error message to stderr and exit with Exit_Code 2.
7. IF the operator supplies none of `--stack`, `--tag`, and `--all`, THEN THE CLI SHALL write an error message to stderr stating that one selection option is required and exit with Exit_Code 2.
8. IF the operator supplies more than one of `--stack`, `--tag`, and `--all`, THEN THE CLI SHALL write a usage error message to stderr stating that the selection options are mutually exclusive and exit with Exit_Code 2.
9. THE CLI SHALL accept the option `--no-nested` to disable nested-stack expansion as defined in Requirement 11.

### Requirement 2: AWS session and credentials

**User Story:** As an operator, I want the CLI to use a single AWS profile and region per run, so that I know exactly which account and region are being checked.

#### Acceptance Criteria

1. WHEN the operator supplies `--profile`, THE CLI SHALL create the boto3 session using the named profile.
2. WHEN the operator supplies `--region`, THE CLI SHALL create the boto3 CloudFormation client in the named region.
3. WHEN the operator omits `--profile` or `--region`, THE CLI SHALL resolve the omitted value through the Credential_Chain.
4. THE CLI SHALL use exactly one profile and exactly one region for the duration of a run.
5. THE CLI SHALL scope every CloudFormation call made during a run, for both selection and detection, to the Bound_Region.
6. IF no region can be resolved from `--region` or the Credential_Chain, THEN THE CLI SHALL write an error message to stderr stating that a region is required and exit with Exit_Code 2.
7. IF no credentials can be resolved, THEN THE CLI SHALL write an error message to stderr stating that AWS credentials were not found and exit with Exit_Code 2.
8. IF the named profile does not exist, THEN THE CLI SHALL write an error message to stderr naming the missing profile and exit with Exit_Code 2.
9. THE CLI SHALL exclude access key IDs, secret access keys, and session tokens from stdout, stderr, the Human_Report, the JSON_Report, and the `--output` file.

### Requirement 3: Read-only boto3 adapter

**User Story:** As an operator with production access, I want the AWS adapter to be read-only, so that running the CLI cannot change live stacks.

#### Acceptance Criteria

1. THE Boto3_Adapter SHALL implement the `CfnClient` protocol.
2. WHEN a Read_Method is called on the Boto3_Adapter, THE Boto3_Adapter SHALL forward the call and its keyword arguments, excluding arguments whose value is `None` and the `Region` argument, to the corresponding boto3 CloudFormation client operation and return the boto3 response dictionary, with each stack entry of `list_stacks` and `describe_stacks` responses annotated with the Bound_Region and with `DELETE_COMPLETE` stack summaries removed from `list_stacks` responses.
3. IF a Mutating_Method is called on the Boto3_Adapter, THEN THE Boto3_Adapter SHALL raise an exception stating that the adapter is read-only, without calling the boto3 client.
4. WHEN a boto3 CloudFormation operation raises a client error, THE Boto3_Adapter SHALL propagate the error to the caller so that the existing retry and outcome-mapping logic handles the error.
5. THE Boto3_Adapter SHALL accept an injected boto3 CloudFormation client so that tests can supply a botocore `Stubber`-wrapped client.
6. IF a Read_Method is called with a `Region` argument that differs from the Bound_Region, THEN THE Boto3_Adapter SHALL raise an error naming both regions, without calling the boto3 client.

### Requirement 4: Stack selection and confirmation

**User Story:** As an operator, I want to review the resolved stack set before detection starts, so that I only check the stacks I intended.

#### Acceptance Criteria

1. WHEN the CLI starts a run, THE CLI SHALL call the Drift_Manager select phase with the Selection_Criteria built from the command-line options.
2. WHEN selection returns a Resolved_Stack_Set, THE CLI SHALL write the stack names of the Resolved_Stack_Set to stderr.
3. WHEN selection returns names that matched no stack, THE CLI SHALL write the unmatched names to stderr.
4. WHILE stdin is a TTY and `--yes` is absent, WHEN selection returns a Resolved_Stack_Set, THE CLI SHALL display the Confirmation_Prompt.
5. WHEN the operator approves the Confirmation_Prompt, THE CLI SHALL call the Drift_Manager confirm phase and proceed to detection.
6. WHEN the operator declines the Confirmation_Prompt, THE CLI SHALL call the Drift_Manager cancel phase, write a cancellation message to stderr, and exit with Exit_Code 2.
7. WHEN `--yes` is present, THE CLI SHALL confirm the Resolved_Stack_Set without displaying the Confirmation_Prompt.
8. IF stdin is not a TTY and `--yes` is absent, THEN THE CLI SHALL write an error message to stderr stating that `--yes` is required for non-interactive runs and exit with Exit_Code 2 before detection starts.
9. IF selection returns an empty, failed, or rejected selection result, THEN THE CLI SHALL write the selection outcome and reason to stderr and exit with Exit_Code 2.
10. WHEN `--all` is supplied, THE CLI SHALL build the Selection_Criteria as a single `Region` criterion whose region is the Bound_Region.
11. WHEN `--stack` is supplied, THE CLI SHALL build the Selection_Criteria as a single `Names` criterion.
12. WHEN `--tag` is supplied, THE CLI SHALL build the Selection_Criteria as a single `Tags` criterion.
13. WHILE `--all` is in effect, IF the Bound_Region holds more live stacks than the Selection_Limit, THEN THE CLI SHALL limit the Resolved_Stack_Set to the first 500 stacks in inventory order retained by the existing selector, run detection and reporting on those stacks only, and leave the remaining stacks unchecked.

### Requirement 5: Drift detection and progress

**User Story:** As an operator, I want to see detection progress, so that I know the run is advancing on large stack sets.

#### Acceptance Criteria

1. WHEN the Resolved_Stack_Set is confirmed, THE CLI SHALL run drift detection through the Drift_Manager using the existing detection limits (maximum concurrency 10, 5-second poll interval, 300-second per-stack timeout, retry with backoff on throttling).
2. WHILE detection is in progress, THE CLI SHALL write Progress_Output to stderr each time the count of completed stacks changes.
3. THE CLI SHALL write Progress_Output exclusively to stderr.
4. WHEN detection finishes, THE CLI SHALL write a detection summary produced by `summarize_detection` to stderr.

### Requirement 6: Report output

**User Story:** As an operator or CI pipeline, I want a human-readable or JSON report, so that I can read results directly or parse them in automation.

#### Acceptance Criteria

1. WHEN detection finishes and `--format` is absent or `text`, THE CLI SHALL write the Human_Report to stdout.
2. WHEN detection finishes and `--format json` is supplied, THE CLI SHALL write to stdout the JSON_Report: the structured output of the existing reporter (`to_machine_readable`) with the per-stack `parentStackName` and `rootStackName` keys of Requirement 11.11 added and `schemaVersion` unchanged.
3. WHILE `--format json` is in effect, THE CLI SHALL write only the JSON_Report to stdout.
4. WHEN `--output FILE` is supplied, THE CLI SHALL write the report in the selected format to FILE.
5. IF writing to the `--output` FILE fails, THEN THE CLI SHALL write an error message to stderr naming the file and exit with Exit_Code 2.
6. THE Human_Report SHALL list each drifted stack with the logical resource ID, resource type, and drift status of each drifted resource.

### Requirement 7: Exit codes

**User Story:** As a CI pipeline owner, I want deterministic exit codes, so that I can gate jobs on drift results.

#### Acceptance Criteria

1. WHEN every stack in the Resolved_Stack_Set is in sync, selection returned no unmatched names, and no stack is a Vanished_Stack, THE CLI SHALL exit with Exit_Code 0.
2. WHEN at least one stack is drifted, no detection is unknown or failed, no stack is a NOT_CHECKED_Stack, selection returned no unmatched names, and no stack is a Vanished_Stack, THE CLI SHALL exit with Exit_Code 1.
3. IF at least one stack detection is unknown or failed, THEN THE CLI SHALL exit with Exit_Code 2.
4. IF the run is cancelled, aborted, or fails with an unhandled error, THEN THE CLI SHALL write an error message to stderr and exit with Exit_Code 2.
5. IF an error ends the run, THEN THE CLI SHALL write a single-line error message to stderr in place of a Python traceback.
6. IF at least one stack in the Resolved_Stack_Set is a NOT_CHECKED_Stack, THEN THE CLI SHALL exit with Exit_Code 2.
7. WHEN selection returns a Resolved_Stack_Set with unmatched names, THE CLI SHALL write the unmatched names to stderr, run detection and reporting on the resolved stacks, and exit with Exit_Code 2.
8. THE CLI SHALL determine the Exit_Code from the status of every stack in the Resolved_Stack_Set, including every Nested_Stack added by nested-stack expansion.
9. IF at least one stack is a Vanished_Stack, THEN THE CLI SHALL run detection and reporting on the remaining stacks of the Resolved_Stack_Set and exit with Exit_Code 2.

### Requirement 8: Read-only scope

**User Story:** As a production account owner, I want the CLI to exclude reconciliation, so that the CLI is safe to grant to read-only operators.

#### Acceptance Criteria

1. THE CLI SHALL limit operations to selection, drift detection, and reporting.
2. THE CLI SHALL omit commands and options for adopt-actual and align-to-template reconciliation.
3. THE CLI SHALL invoke CloudFormation exclusively through Read_Methods of the Boto3_Adapter.

### Requirement 9: Packaging and dependencies

**User Story:** As a maintainer, I want boto3 to be optional, so that the core library stays dependency-free.

#### Acceptance Criteria

1. THE `pyproject.toml` core `dependencies` list SHALL remain empty.
2. THE AWS_Extra SHALL declare boto3 with a pinned exact version, installable through `pip install drift-manager[aws]`.
3. THE `dev` extra SHALL declare boto3 with the same pinned exact version as the AWS_Extra.
4. THE `drift_manager` package SHALL be importable without boto3 installed.
5. IF boto3 is not installed when the CLI runs, THEN THE CLI SHALL write an error message to stderr instructing the operator to install `drift-manager[aws]` and exit with Exit_Code 2.

### Requirement 10: Offline testability

**User Story:** As a maintainer, I want the AWS adapter and CLI tested without network access, so that the test suite runs in CI without AWS credentials.

#### Acceptance Criteria

1. THE test suite SHALL verify each Read_Method of the Boto3_Adapter against a botocore `Stubber`-wrapped CloudFormation client.
2. THE test suite SHALL verify that each Mutating_Method of the Boto3_Adapter raises an exception and records no call on the Stubber-wrapped client.
3. THE test suite SHALL verify CLI Exit_Code behavior for in-sync, drifted, unknown or failed, NOT_CHECKED_Stack, unmatched names (Exit_Code 2 with resolved stacks still reported), cancelled, non-TTY without `--yes`, missing boto3, and missing credentials scenarios without network access.
4. THE test suite SHALL verify `--all` selection and the mutual exclusivity of `--stack`, `--tag`, and `--all` without network access.
5. THE test suite SHALL verify, without network access, nested-stack expansion from a selected Parent_Stack, `--no-nested`, a Stack_Tree of depth 2 or greater, direct selection of a Nested_Stack, absence of duplicate detections under `--all`, Orphan_Nested_Stack warnings, expansion beyond the Selection_Limit, a Vanished_Stack, and Exit_Code 1 and Exit_Code 2 outcomes driven solely by a Nested_Stack status.

### Requirement 11: Nested stack checking

**User Story:** As an operator, I want drift checked across a parent stack and all of its nested child stacks, so that drift inside a child stack is reported even though CloudFormation drift detection on a parent does not cover its nested stacks.

#### Acceptance Criteria

1. WHILE `--no-nested` is absent, WHEN selection through `--stack` or `--tag` returns a Directly_Selected_Stack that has Descendants, THE CLI SHALL add every Descendant of that stack, at every depth, to the Resolved_Stack_Set before the Confirmation_Prompt.
2. WHEN `--no-nested` is supplied, THE CLI SHALL limit the Resolved_Stack_Set to the Directly_Selected_Stacks.
3. WHEN `--all` is supplied, THE CLI SHALL include every Root_Stack and every Nested_Stack returned in the Bound_Region in the Resolved_Stack_Set, subject to the Selection_Limit of Requirement 4.13, without a separate nested-stack expansion step.
4. THE CLI SHALL include each stack in the Resolved_Stack_Set exactly once and run drift detection on each stack exactly once per run, including when a stack is both a Directly_Selected_Stack and a Descendant of another Directly_Selected_Stack.
5. WHEN the operator selects a Nested_Stack directly by name, THE CLI SHALL exclude the ancestors of that Nested_Stack from the Resolved_Stack_Set unless an ancestor is itself a Directly_Selected_Stack.
6. THE CLI SHALL determine the parent of each Nested_Stack from the `ParentId` stack ARN and the root from the `RootId` stack ARN returned by CloudFormation.
7. WHEN the CLI writes the Resolved_Stack_Set to stderr, THE CLI SHALL list each Nested_Stack indented beneath its Parent_Stack and SHALL state the count of Root_Stacks, the count of Nested_Stacks, and the total count of stacks.
8. WHEN the Resolved_Stack_Set is confirmed, THE CLI SHALL run drift detection on each Parent_Stack and each Nested_Stack in the Resolved_Stack_Set under the detection limits of Requirement 5.
9. THE Human_Report SHALL list each Nested_Stack indented beneath its Parent_Stack and SHALL show the drift status of each stack.
10. WHEN at least one Descendant of a stack in the Human_Report is in the Resolved_Stack_Set and is drifted, THE Human_Report SHALL mark that stack, whatever its own drift status, with an indication that drift exists in a Descendant.
11. THE JSON_Report SHALL include, for each stack entry, the parent stack name and the root stack name, with a `null` value for both fields on a Root_Stack.
12. WHEN at least one Nested_Stack in the Resolved_Stack_Set is drifted, THE CLI SHALL exit with an Exit_Code of at least 1.
13. IF at least one Nested_Stack in the Resolved_Stack_Set has an unknown or failed detection or is a NOT_CHECKED_Stack, THEN THE CLI SHALL exit with Exit_Code 2.
14. IF a Nested_Stack is an Orphan_Nested_Stack, THEN THE CLI SHALL write a warning to stderr naming the Orphan_Nested_Stack and the missing `ParentId` or `RootId` identifier, treat an Orphan_Nested_Stack whose `ParentId` is absent from the inventory as a Root_Stack for display and reporting, keep the parent of an Orphan_Nested_Stack whose only absent identifier is `RootId` and use the top of its parent chain in the inventory as its root, and run drift detection on the stacks of the Resolved_Stack_Set.
15. THE CLI SHALL obtain nested-stack hierarchy information exclusively through Read_Methods of the Boto3_Adapter.
16. IF nested-stack expansion of a `--stack` or `--tag` selection produces more stacks than the Selection_Limit, THEN THE CLI SHALL write an error message to stderr stating the expanded count and the 500-stack limit and exit with Exit_Code 2 before detection starts.
17. IF a stack is a Vanished_Stack, THEN THE CLI SHALL write a warning to stderr naming the Vanished_Stack and stating that it was not checked, and continue the run with the stacks returned by the re-read.
