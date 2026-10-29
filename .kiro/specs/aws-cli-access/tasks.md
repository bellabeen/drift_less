# Implementation Plan: AWS CLI Access

## Overview

This plan builds the read-only `drift-manager` CLI in Python, bottom-up: packaging and boto3 install, the `Boto3CfnClient` adapter and `build_client`, the `stack_tree` hierarchy module, the `text_report` renderers, `cli.py` with nested-stack resolution, and last `__main__.py` with the end-to-end tests. The core pipeline modules (`selector`, `detector`, `manager`, `reporter`, `models`) stay unchanged. The only existing files modified are `pyproject.toml` and, additively, `tests/fakes.py`. Tests run offline with `.venv/bin/python -m pytest`, using botocore `Stubber` for the adapter and `tests/fakes.FakeCfnClient` plus `FakeClock` for the CLI.

The tasks implement the approved and resolved design decisions:
- `--stack`, `--tag`, and `--all` are mutually exclusive; `--all` maps to `Region(bound_region)` and keeps the selector's 500-stack bound (Decisions 2, 7, 10).
- `NOT_CHECKED` stacks, unmatched `--stack` names, and Vanished_Stacks map to exit 2 (Decisions 3, 4, 11).
- The adapter adds `Region` to stack entries and drops `DELETE_COMPLETE` summaries (Decision 5).
- The JSON_Report is the existing reporter's output plus per-stack `parentStackName` / `rootStackName` (Decision 8).
- Only a missing `ParentId` makes an orphan a root; a `RootId`-only orphan keeps its parent (Decision 9).
- The descendant-drift marker applies to any stack with a drifted Descendant (Decision 12).
- `sts:GetCallerIdentity` is not included (Decision 6).

## Tasks

- [ ] 1. Packaging and dependencies
  - [ ] 1.1 Update `pyproject.toml` and install boto3 into `.venv`
    - Keep `[project] dependencies = []`
    - Add `[project.optional-dependencies] aws = ["boto3==1.43.118"]` and add `"boto3==1.43.118"` to the existing `dev` extra (same pin)
    - Add `[project.scripts] drift-manager = "drift_manager.cli:main"`
    - Run `.venv/bin/python -m pip install -e '.[dev]'` and confirm `.venv/bin/python -c "import boto3, botocore.stub"` succeeds
    - Run `.venv/bin/python -m pytest` to confirm the existing suite still passes
    - _Requirements: 1.1, 9.1, 9.2, 9.3_

- [ ] 2. Read-only boto3 adapter (`src/drift_manager/aws_adapter.py`)
  - [ ] 2.1 Implement the error types and `Boto3CfnClient`
    - Define `CliError(message, exit_code=2)`, `ReadOnlyAdapterError`, `RegionMismatchError`, `INSTALL_HINT`, `_HIDDEN_STACK_STATUSES`, and `_clean()`
    - Implement `Boto3CfnClient(client, region)` with a `region` property and the five Read_Methods. Each goes through `_scope()`, which strips `None` kwargs, consumes `Region`, and raises `RegionMismatchError` on a mismatch
    - `list_stacks` adds `"Region"` to each summary and drops `DELETE_COMPLETE` entries; `ParentId` / `RootId` pass through. `describe_stacks` adds `"Region"` to each stack. The other three return the boto3 response unchanged
    - The three Mutating_Methods raise `ReadOnlyAdapterError` without touching the client. No `__getattr__` pass-through
    - botocore `ClientError` propagates uncaught. No top-level boto3 import
    - _Requirements: 2.4, 2.5, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 9.4, 11.6_

  - [ ] 2.2 Write Stubber tests for the adapter (`tests/test_aws_adapter_stubber.py`)
    - Build the stubbed client with `boto3.client("cloudformation", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")`; clear `AWS_*` env vars and point `AWS_SHARED_CREDENTIALS_FILE` / `AWS_CONFIG_FILE` at non-existent paths
    - One test per Read_Method using `add_response(op, response, expected_params)` and `assert_no_pending_responses()`. The `list_stacks` response includes a nested summary and the test asserts `ParentId` / `RootId` come back
    - Two-page `list_stacks` pagination via `NextToken`, run through `StackSelector` and through `load_stack_tree` (task 4.2)
    - `add_client_error("detect_stack_drift", "Throttling")` propagates and `is_transient_error` returns `True`. `AccessDenied` propagates and returns `False`
    - Each Mutating_Method raises `ReadOnlyAdapterError` and the Stubber records no call
    - `isinstance(adapter, CfnClient)` is `True`
    - _Requirements: 3.1, 3.3, 3.4, 10.1, 10.2, 11.6_

  - [ ]* 2.3 Write property test for Read_Method forwarding (`tests/test_aws_adapter_prop_forwarding.py`)
    - **Property 3: Read_Methods forward exactly the non-None arguments**
    - Recording fake boto3 client; `st.sampled_from(READ_METHODS)` and `st.dictionaries(param_names, st.none() | st.text())` without `Region`
    - **Validates: Requirements 3.2, 3.5**

  - [ ]* 2.4 Write property test for single-region scoping (`tests/test_aws_adapter_prop_region.py`)
    - **Property 4: Adapter is scoped to a single region**
    - Bound vs requested regions; summary lists with `st.sampled_from(STACK_STATUSES)` including `DELETE_COMPLETE` and optional `ParentId` / `RootId`; check `Region` annotation, filtering, relative order, and unchanged other keys
    - **Validates: Requirements 2.4, 2.5, 3.2, 3.6, 11.6**

  - [ ]* 2.5 Write property test for Mutating_Method refusal (`tests/test_aws_adapter_prop_readonly.py`)
    - **Property 5: Mutating_Methods are refused without reaching the client**
    - `st.sampled_from(MUTATING_METHODS)` with arbitrary kwargs dicts; recording client must have zero calls
    - **Validates: Requirements 3.3, 8.3**

  - [ ] 2.6 Implement `AwsContext` and `build_client` in `aws_adapter.py`
    - Frozen dataclass `AwsContext(client, region, profile)`
    - `build_client(profile, region, *, session_factory=None)` lazily imports `boto3` / `botocore`. An `ImportError` becomes `CliError(INSTALL_HINT)`
    - `ProfileNotFound` becomes a message naming the profile. Region comes from `--region`, falling back to `session.region_name`, with a clear error when neither is set
    - `get_credentials()` returning `None` gives the "not found" message. A `NoCredentialsError` / `BotoCoreError` gives a class-name-only message. Never read or format credential attributes
    - Create the `"cloudformation"` client with `Config(retries={"mode": "standard", "max_attempts": 3}, connect_timeout=10, read_timeout=30)` and wrap it in `Boto3CfnClient`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 2.7, 2.8, 2.9, 9.4, 9.5_

  - [ ]* 2.7 Write unit tests for `build_client` (`tests/test_build_client.py`)
    - Fake session factory: `profile_name` is passed through, `--region` takes precedence over `session.region_name`, `ProfileNotFound(profile="x")` gives a message naming `x`, no region gives the region error, `None` credentials give the credentials error, a `BotoCoreError` subclass gives the class-name-only message, and `session.client` is called with `"cloudformation"` and a standard-mode `Config`
    - _Requirements: 2.1, 2.2, 2.3, 2.6, 2.7, 2.8, 9.5_

- [ ] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass with `.venv/bin/python -m pytest`, ask the user if questions arise.

- [ ] 4. Nested-stack hierarchy (`src/drift_manager/stack_tree.py`)
  - [ ] 4.1 Extend test support for nested stacks
    - `tests/fakes.py` (additive): `ScriptedStack` gains `parent_id: Optional[str] = None` and `root_id: Optional[str] = None`, emitted as `ParentId` / `RootId` in `to_summary()` and `to_description()` only when set; `FakeCfnClient.add_stack` gains the same keyword arguments. Existing tests stay green
    - `tests/strategies_tree.py`: `forests()` (1–30 uniquely named stacks, parents drawn from earlier stacks, CloudFormation-style names and ARNs), `orphaned(forest)` (replaces random `ParentId` / `RootId` with ARNs outside the inventory), and `subsets(forest)` (selections with repeats, shuffled)
    - _Requirements: 10.5_

  - [ ] 4.2 Implement `StackNode`, `OrphanWarning`, `StackTree`, `build_stack_tree`, and `load_stack_tree`
    - `StackTree` with `nodes`, `children` and `roots` sorted by name, `orphans`, and `descendants`, `ancestors`, `position`, `order`, `ensure`
    - `build_stack_tree(summaries)`: index by `StackName` and `StackId`; a missing `ParentId` target gives `OrphanWarning(name, "ParentId", arn)` and makes the stack a root; a missing `RootId` target gives `OrphanWarning(name, "RootId", arn)` but keeps the parent, with `rootName` = top of the parent chain; a visited-set cycle guard treats the closing stack as a root with a `ParentId` warning
    - `load_stack_tree(cfn)` paginates `list_stacks` via `NextToken` through any `CfnClient`. No boto3 import
    - _Requirements: 11.6, 11.14, 11.15_

  - [ ]* 4.3 Write property test for hierarchy recovery (`tests/test_stack_tree_prop_hierarchy.py`)
    - **Property 12: Hierarchy is recovered from ParentId and RootId**
    - `forests()` serialized to summaries, `st.permutations` for page order
    - **Validates: Requirements 11.6**

  - [ ]* 4.4 Write property test for orphans (`tests/test_stack_tree_prop_orphans.py`)
    - **Property 15: Orphans are warned about and treated as roots**
    - `orphaned(forests())`; also check that a `RootId`-only orphan keeps its parent
    - **Validates: Requirements 11.14**

  - [ ] 4.5 Implement expansion, ordering, layout, markers, and JSON annotation
    - `expand_selection(tree, selected)`: tree-ordered, deduplicated union of the selection and every descendant; never adds ancestors
    - `order_report(report, tree)`: permutation of entries by tree position; counts and `emptyMessage` unchanged
    - `LayoutRow`, `layout(tree, names)` (depth counts displayed ancestors, `nestedIn` when the parent isn't displayed), and `hierarchy_counts(tree, names)` returning `(roots, nested, total)`
    - `nested_drift_markers(report, tree)`: names of entries, of any status, with at least one `DRIFTED` descendant in the report
    - `annotate_machine_readable(data, tree)`: adds `parentStackName` / `rootStackName` to each stack entry (`None` for roots and orphans treated as roots); `schemaVersion` and other keys unchanged
    - _Requirements: 6.2, 11.1, 11.4, 11.5, 11.7, 11.9, 11.10, 11.11_

  - [ ]* 4.6 Write property test for expansion (`tests/test_stack_tree_prop_expansion.py`)
    - **Property 13: Expansion is the descendant closure of the selection**
    - `forests()`, `subsets()`; oracle: naive recursive closure
    - **Validates: Requirements 11.1, 11.4, 11.5**

  - [ ]* 4.7 Write property test for tree order (`tests/test_stack_tree_prop_order.py`)
    - **Property 14: Tree order puts parents before children**
    - `forests()`, `subsets()`, random statuses for a `DriftReport`
    - **Validates: Requirements 11.7, 11.9**

  - [ ]* 4.8 Write property test for JSON annotation (`tests/test_stack_tree_prop_json.py`)
    - **Property 18: JSON parent and root fields match the tree**
    - `forests()` and orphaned forests, `subsets()`, `DriftReport` over the subset; check `schemaVersion == 1` and the `from_machine_readable` round trip
    - **Validates: Requirements 6.2, 11.11**

- [ ] 5. Report renderers (`src/drift_manager/text_report.py`)
  - [ ] 5.1 Implement `render_text`, `render_summary`, `render_selection`, and `render_orphan_warning`
    - `render_text(report, tree=None)`: header with counts, then one line per stack in report order with the status padded to 12 characters and the name indented `2 × depth` from `layout`; ` (nested in <parent>)` when the parent isn't in the report; `  [drift in nested stack]` for `nested_drift_markers`. Each `DRIFTED` stack gets `- {logicalResourceId} ({resourceType}): {resourceDriftStatus}` lines and property differences. Values go through `json.dumps(..., ensure_ascii=False, default=str)`, `ABSENT` renders as `<absent>`, an empty report renders `emptyMessage`. `tree=None` renders flat with no markers
    - `render_summary(summary, unmatched_names=())`: succeeded/failed/skipped counts, a reason per failed or skipped stack, and the given unmatched names. A `SummaryError` renders its message
    - `render_selection(stacks, unmatched_names, region, tree)`: `Resolved N stack(s) in {region} (R root, K nested):`, indented `  {name} ({status})` rows from `layout`, then `Not found: …` when there are unmatched names
    - `render_orphan_warning(warning)`: one `drift-manager: warning:` line naming the stack, the `ParentId` / `RootId` field, and the missing ARN
    - _Requirements: 4.2, 4.3, 5.4, 6.1, 6.6, 11.7, 11.9, 11.10, 11.14_

  - [ ]* 5.2 Write property test for the Human_Report (`tests/test_text_report_prop.py`)
    - **Property 9: Human_Report lists every drifted resource**
    - `DriftReport` via `reporter.report(DetectionResults(...))` with `st.text()` property values including control characters; `st.none() | forests()` for the tree; no raw U+0000–U+001F inside rendered values
    - **Validates: Requirements 6.6**

  - [ ]* 5.3 Write property test for indentation (`tests/test_text_report_prop_tree.py`)
    - **Property 16: Listing and report indentation follow the tree**
    - `forests()`, `subsets()`, random statuses; parse stack lines back to `(depth, name)`; header root and nested counts sum to the total
    - **Validates: Requirements 11.7, 11.9**

  - [ ]* 5.4 Write property test for the descendant-drift marker (`tests/test_text_report_prop_tree.py`)
    - **Property 17: The descendant-drift marker is exact**
    - `forests()`, `subsets()`, `st.sampled_from(DriftStatus)` per stack; oracle: naive descendant scan
    - **Validates: Requirements 11.10**

- [ ] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass with `.venv/bin/python -m pytest`, ask the user if questions arise.

- [ ] 7. CLI core (`src/drift_manager/cli.py`)
  - [ ] 7.1 Implement the parser and stream plumbing
    - `_ParserExit(status)` and `_Parser(argparse.ArgumentParser)`, overriding `_print_message` to use the injected streams and `exit` to raise `_ParserExit`
    - `_Streams(stdin, stdout, stderr)` with `err(text, end="\n")`, which writes and flushes stderr
    - `parse_tag(text)` splits on the first `=`, requires a non-empty key, and raises `argparse.ArgumentTypeError` on bad input
    - `build_parser()` with `--profile`, `--region`, `--stack` (append), `--tag` (append, `type=parse_tag`), `--all` (`dest="all_stacks"`), `--no-nested`, `--yes`/`-y`, `--format {text,json}` (default `text`), and `--output FILE`. `--stack`, `--tag`, and `--all` share one non-required mutually exclusive group. No reconcile/adopt/align options
    - Re-export `CliError` from `aws_adapter`
    - _Requirements: 1.3, 1.4, 1.5, 1.6, 1.8, 1.9, 8.2_

  - [ ] 7.2 Implement `exit_code_for`, `require_criterion`, and `build_criterion`
    - `exit_code_for(counts, unmatched_names=())`: 2 if `UNKNOWN > 0`, `NOT_CHECKED > 0`, or any unmatched name; otherwise 1 if `DRIFTED > 0`; otherwise 0. Callers pass unmatched names plus Vanished_Stacks
    - `require_criterion(args)` raises `CliError(NO_CRITERION)` (`exactly one selection option is required: pass --stack, --tag, or --all`) when none is set
    - `build_criterion(args, region)` returns `Names(tuple(args.stacks))`, `Tags(tuple(args.tags))`, or `Region(region)` for `--all`
    - _Requirements: 1.7, 4.1, 4.10, 4.11, 4.12, 7.1, 7.2, 7.3, 7.6, 7.7, 7.9_

  - [ ]* 7.3 Write property test for exit codes (`tests/test_exit_code_prop.py`)
    - **Property 1: Exit code follows status precedence**
    - `st.builds(StatusCounts, ...)` with four `st.integers(0, 50)` and `st.lists(st.text(min_size=1), max_size=3)` for unmatched names
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.6, 7.7**

  - [ ]* 7.4 Write property test for criterion mapping (`tests/test_cli_prop_tags.py`)
    - **Property 2: Selection options map to the matching criterion**
    - Keys `st.text(min_size=1)` filtered to exclude `=` and blank; values `st.text()`; names `st.lists(st.text(min_size=1), min_size=1)`; regions `st.from_regex(r"[a-z]{2}-[a-z]+-[1-9]", fullmatch=True)`; parsed through `build_parser()`
    - **Validates: Requirements 1.5, 4.1, 4.10, 4.11, 4.12**

  - [ ] 7.5 Implement `resolve_nested`
    - Load the tree with `load_stack_tree(client)`, write each orphan warning, and `tree.ensure(direct)` for stacks created after the tree read
    - Return the first `Resolved` unchanged with `--no-nested` or `--all` (the existing selector bound applies to `--all`)
    - Otherwise compute `expand_selection(tree, direct)`; if nothing is added, return the first selection; if the expansion exceeds `MAX_RESOLVED_STACKS`, raise `CliError` with the expanded count and the 500-stack limit; else re-select with `manager.select(Names(expanded))`
    - A non-`Resolved` re-selection raises the selection failure message. Each name in the re-selection's `unmatchedNames` is a Vanished_Stack: write one warning line and return it so it counts toward exit 2
    - _Requirements: 4.13, 7.9, 11.1, 11.2, 11.3, 11.4, 11.5, 11.14, 11.15, 11.16, 11.17_

  - [ ] 7.6 Implement `main`, `_run`, and the run helpers
    - `main(argv=None, *, stdin, stdout, stderr, client_factory, clock) -> int`. Defaults are `sys.*`, `build_client`, and `SystemClock()`. Never calls `sys.exit`. Handles `_ParserExit`, `CliError`, `KeyboardInterrupt` (`interrupted`), and generic `Exception`, each returning the right code
    - `_run` order: `require_criterion` → non-TTY without `--yes` gate → `client_factory` → context line → `DriftManager(ctx.client, clock)` with default limits → first `select` → `selection_failure_message` for Empty/Failed/Rejected → unmatched names from the first selection → `resolve_nested` → `render_selection` → `--yes` or `_prompt` (`confirm_selection` / `cancel_selection` + cancel error) → `_run_detection` → `render_summary(summary, unmatched + vanished)` → `order_report` → render text, or `annotate_machine_readable(to_machine_readable(...))` as `json.dumps(indent=2) + "\n"` → `write_report_file` before stdout → `exit_code_for(counts, unmatched + vanished)`
    - `_prompt` writes `Run drift detection on N stack(s) in R? [y/N]: ` to stderr, with N including nested stacks. It accepts only `y`/`yes` (case-insensitive, stripped). EOF counts as a decline
    - `_run_detection` writes a `Detecting drift on N stack(s)...` header, then loops `advance_detection()` and writes `Progress: c/n complete[, k failed]` only when `completed` changes
    - `describe_exception` uses `retry.error_code` and the first line of the message. `_error` writes one whitespace-collapsed `drift-manager: error: …` line
    - `write_report_file`: atomic write via `tempfile.mkstemp` in the target directory, `chmod 0o600`, `os.replace`. On `OSError`, unlink the temp file and raise `CliError` naming the path
    - Never call `select_scope`, `change_selection`, `confirm_scope`, or `reconcile`
    - _Requirements: 2.4, 2.5, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 5.1, 5.2, 5.3, 5.4, 6.1, 6.2, 6.3, 6.4, 6.5, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 8.1, 8.3, 11.8, 11.11, 11.12, 11.13_

  - [ ] 7.7 Write unit tests for argument parsing (`tests/test_cli_args.py`)
    - Every option parses, including `--all` and `--no-nested`
    - `--format xml`, an unknown flag, `--tag novalue`, and `--tag =v` each return 2 with stderr output and no factory call
    - Each pair of `--stack` / `--tag` / `--all`, and all three together, return 2 with a `not allowed with` usage error and no factory call
    - No selection option returns 2 with the exact `NO_CRITERION` message
    - `--help` returns 0 and doesn't mention reconcile, adopt, or align
    - _Requirements: 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 8.2, 10.4_

- [ ] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass with `.venv/bin/python -m pytest`, ask the user if questions arise.

- [ ] 9. Module entry point and end-to-end CLI tests
  - [ ] 9.1 Create `src/drift_manager/__main__.py`
    - `from .cli import main` then `raise SystemExit(main())`
    - _Requirements: 1.2_

  - [ ] 9.2 Create the shared CLI scenario strategy (`tests/strategies_cli.py`)
    - 1–15 stacks with random statuses, each with a scripted detection outcome: `IN_SYNC`, `DRIFTED` with resource pages, `DETECTION_FAILED`, never-completing, initiation error, or throttling then success. Optional unmatched names
    - Helpers to build a `FakeCfnClient` + `FakeClock`, a `FakeStdin(StringIO)` with a configurable `isatty()`, and a `client_factory` returning `AwsContext(Boto3CfnClient(fake, region), region, profile)`
    - _Requirements: 10.3_

  - [ ]* 9.3 Write property test for confirmation gating (`tests/test_cli_prop_gating.py`)
    - **Property 6: Detection starts only after confirmation**
    - `st.booleans()` for TTY and for `--yes`; answers drawn from `""`, `y`/`Y`/` yes `/`n`/`no`, and `st.text()`
    - **Validates: Requirements 4.4, 4.5, 4.6, 4.7, 4.8, 7.4**

  - [ ]* 9.4 Write property test for the run output contract (`tests/test_cli_prop_run_contract.py`)
    - **Property 7: A completed run honours the output contract**
    - Run with `--yes`, `--format json|text`, and `--output tmp_path/...` using the scenario strategy
    - `from_json(stdout) == order_report(manager.report(), tree)` (or `render_text` for text), the file matches stdout byte for byte, every resolved stack is detected even with unmatched names, the return value equals `exit_code_for(counts, unmatched)`, resolved and unmatched names appear on stderr, and there are zero mutating calls
    - **Validates: Requirements 4.2, 4.3, 6.1, 6.2, 6.3, 6.4, 7.1, 7.2, 7.3, 7.6, 7.7, 8.1, 8.3, 11.15**

  - [ ]* 9.5 Write property test for progress output (`tests/test_cli_prop_progress.py`)
    - **Property 8: Progress is monotonic and stderr-only**
    - `Progress: c/n` lines have strictly increasing `c` and constant `n` equal to the resolved count, the last `c` equals `n`, and stdout has no progress lines
    - **Validates: Requirements 5.2, 5.3**

  - [ ]* 9.6 Write property test for single-line errors (`tests/test_cli_prop_errors.py`)
    - **Property 10: Errors are a single stderr line**
    - `RuntimeError`, `ValueError`, `KeyError`, and `FakeClientError` with `st.text()` messages including newlines, raised from the factory, from `list_stacks` during selection, or from the hierarchy read
    - Exit is 2, stderr has exactly one `drift-manager: error:` line and no `Traceback`, and stdout is empty
    - **Validates: Requirements 7.4, 7.5**

  - [ ]* 9.7 Write property test for credential hygiene (`tests/test_cli_prop_credentials.py`)
    - **Property 11: Credentials never reach any output**
    - Generated key ID, secret, and token (`ascii_letters+digits+"/+"`, 16–40 chars) returned by a fake session via `build_client(session_factory=...)`
    - Outcomes drawn from in sync, drifted, unknown, cancelled, selection error, `AccessDenied`, and credential-provider error; none of the three strings may appear in stdout, stderr, or the output file
    - **Validates: Requirements 2.9**

  - [ ]* 9.8 Write property test for nested runs (`tests/test_cli_prop_nested_run.py`)
    - **Property 19: Nested runs check every resolved stack exactly once**
    - `forests()` scripted into `FakeCfnClient` with propagated tags; mode via `st.sampled_from(["stack", "tag", "all"])`; `st.booleans()` for `--no-nested`; random detection outcomes and unknown `--stack` names
    - **Validates: Requirements 7.7, 7.8, 11.2, 11.3, 11.4, 11.8, 11.12, 11.13**

  - [ ] 9.9 Write CLI scenario tests (`tests/test_cli_scenarios.py`)
    - Exit codes: in-sync → 0, drifted → 1, detection failed → 2, skipped (`NOT_CHECKED`) stack → 2
    - Unmatched name with one in-sync resolved stack → 2, with the resolved stack detected and reported and `Not found:` on stderr
    - Cancelled at the prompt → 2 with the cancel message. Non-TTY without `--yes` → 2 with no factory call
    - Missing boto3 via `monkeypatch.setitem(sys.modules, "boto3", None)` with the default factory → 2 with the install hint. Missing credentials → 2
    - Empty selection → 2. Summary reasons appear on stderr
    - `--all` builds `Region(resolved_region)` and checks every live stack through `Boto3CfnClient` (deleted stacks excluded). `--all` over 501 live stacks checks exactly the first 500 in inventory order
    - `--output` into a non-existent directory → 2 with empty stdout. The output file mode is `0o600`
    - `DriftManager` uses default detection limits
    - _Requirements: 2.5, 2.7, 4.3, 4.6, 4.8, 4.9, 4.10, 4.13, 5.1, 5.4, 6.5, 7.1, 7.2, 7.3, 7.6, 7.7, 9.5, 10.3, 10.4_

  - [ ] 9.10 Write nested-stack scenario tests (`tests/test_cli_nested_scenarios.py`)
    - Fixture: `app` → `app-Net`, `app-Svc` → `app-Svc-Api`, plus root `db`
    - `--stack app` checks the 4 stacks of the tree (depth 2), lists them indented with `(1 root, 3 nested)`, and makes exactly 3 `list_stacks` calls
    - `--stack app --no-nested` checks only `app` with no re-selection. `--stack app-Svc-Api` checks only that stack, shows `(nested in app-Svc)`, and JSON has `parentStackName: "app-Svc"`, `rootStackName: "app"`
    - `--stack app --stack app-Svc`, `--all`, and `--tag env=prod` with propagated tags each check every stack once; `--all` makes no re-selection
    - Only `app-Svc-Api` drifted → exit 1 with `[drift in nested stack]` on `app` and `app-Svc`. Only `app-Net` `UNKNOWN` → 2. Only `app-Net` skipped (`UPDATE_IN_PROGRESS`) → 2
    - A `ParentId` orphan → one warning naming the stack and ARN, listed as a root, detection still run. A `RootId`-only orphan → warning, parent kept
    - Expansion over 500 stacks → 2 with the limit message and no detection. A descendant deleted between the tree read and the re-selection → Vanished_Stack warning and exit 2 with the other stacks reported
    - _Requirements: 6.2, 7.8, 7.9, 10.5, 11.1, 11.2, 11.3, 11.4, 11.5, 11.7, 11.8, 11.9, 11.10, 11.11, 11.12, 11.13, 11.14, 11.16, 11.17_

  - [ ]* 9.11 Write packaging tests (`tests/test_packaging.py`)
    - `tomllib` checks: `dependencies == []`, the `aws` and `dev` boto3 pins are equal and exact, and the `drift-manager` script entry exists
    - A subprocess with `boto3` blocked imports `drift_manager`, `drift_manager.cli`, `drift_manager.aws_adapter`, `drift_manager.stack_tree`, and `drift_manager.text_report`
    - `.venv` Python `-m drift_manager --help` exits 0
    - `cli.py` source contains none of `reconcile(`, `select_scope(`, `confirm_scope(`, `change_selection(`
    - _Requirements: 1.1, 1.2, 8.1, 9.1, 9.2, 9.3, 9.4_

- [ ] 10. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass with `.venv/bin/python -m pytest`, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. Tasks 2.2, 7.7, 9.9, and 9.10 are required because Requirements 10.1–10.5 require that test coverage; 4.1 and 9.2 are required test support for them.
- Each property test uses `@settings(max_examples=100)` and a docstring tag `Feature: aws-cli-access, Property N: <title>`.
- Tests never need network access or real credentials. Clear `AWS_*` env vars and point the shared config/credentials files at non-existent paths.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "4.1"] },
    { "id": 2, "tasks": ["2.3", "2.4", "2.5", "2.6", "4.2"] },
    { "id": 3, "tasks": ["2.2", "2.7", "4.3", "4.4", "4.5"] },
    { "id": 4, "tasks": ["4.6", "4.7", "4.8", "5.1"] },
    { "id": 5, "tasks": ["5.2", "5.3", "5.4", "7.1"] },
    { "id": 6, "tasks": ["7.2"] },
    { "id": 7, "tasks": ["7.3", "7.4", "7.5"] },
    { "id": 8, "tasks": ["7.6"] },
    { "id": 9, "tasks": ["7.7", "9.1", "9.2"] },
    { "id": 10, "tasks": ["9.3", "9.4", "9.5", "9.6", "9.7", "9.8", "9.9", "9.10", "9.11"] }
  ]
}
```
