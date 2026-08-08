# Personal Codex Harness

This repository contains a small, personal harness around the Codex CLI. The
current implementation creates a plan, waits for explicit approval, executes its
steps sequentially with retries, and decides completion from independent command
evidence.

## Working agreements

- Prefer focused, standard-library Python and support Python 3.11 or newer.
- Keep Codex CLI invocation and JSONL handling inside `harness/agents/runner.py`.
- Only `HarnessController` may change run or step state.
- Treat model output as a report. Controller-owned verification evidence decides
  whether a step or run completed.
- Preserve pre-existing user changes. Never stage, commit, push, revert, or delete
  them automatically.
- Keep execution inside each plan step's `allowed_paths`; never modify `.git` or
  controller-owned `.harness/runs` metadata.
- Keep network disabled by default. Only an approved step with
  `network_access: true` may enable executor network access, and only when the
  repository-level network policy also permits it. Planner, reviewer, and
  controller-owned verification remain offline.
- Keep sequential execution as the default. Parallel writers must remain
  explicit opt-in, dependency-aware, path-disjoint, and isolated from the real
  working tree until controller-owned verification passes.

## Current workflow

1. On Linux or WSL, `$harness-setup` prepares a private, dedicated Harness Codex
   home, completes its login flow, merges the per-user outer Codex writable-root
   entry, and verifies the result. A Codex restart is required after a new root.
2. `$harness-plan` checks prerequisites, runs Codex read-only, stores one draft
   run for one user goal, and opens its read-only localhost dashboard.
3. The user reviews or edits the draft, then explicitly invokes
   `$harness-approve <run-id>`.
4. The approval skill records the plan hash and current Git working-tree
   fingerprint, executes the run, and performs one independent review after a
   terminal result.
5. `run` rejects a changed approval baseline and retries failed execution or
   verification up to the configured limit. It runs sequentially by default;
   opt-in parallel writers execute dependency-ready, path-disjoint steps in
   isolated repository copies before controller integration.
6. The controller independently runs per-step and final acceptance commands. A
   command failure, out-of-scope edit, Git index/HEAD change, or run-metadata
   tampering prevents completion.
7. `status` reads the controller-owned run state and evidence remains under
   `.harness/runs/<run-id>/`.
8. `review` runs read-only with its own model profile and stores an advisory
   report without changing controller-owned terminal state.
9. `run --ui` and `ui` expose a localhost-only, read-only progress dashboard.

## User workflow

```text
$harness-setup
$harness-plan "<goal>"
$harness-approve <run-id>
```

## Internal CLI

```bash
python3 scripts/harness.py setup
python3 scripts/harness.py doctor
python3 scripts/harness.py plan "<goal>"
python3 scripts/harness.py approve <run-id>
python3 scripts/harness.py run <run-id>
python3 scripts/harness.py status <run-id>
python3 scripts/harness.py review <run-id>
python3 scripts/harness.py ui <run-id>
```

## Workflow routing

- Use `$harness-setup` for first installation or failed runtime-home/login
  diagnostics. It performs setup without creating or changing a run.
- Use `$harness-plan` to create and inspect one draft run and automatically open
  its dashboard without editing project code.
- Use `$harness-approve` only when the user explicitly approves an exact run ID;
  it sequences approval, execution, and one terminal review.
- Use `$harness-review` to review scope, state, Git changes, and verification
  evidence without editing.
- Use `HARNESS_DESIGN.md`, `harness/orchestration/controller.py`,
  `harness/domain/`, the schemas, and the tests as the source of truth for
  runtime behavior and contracts.

## Verification

Run the focused test module while editing, followed by the complete offline suite
and a compile check:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_<module> -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q harness scripts/harness.py
```

Do not claim a harness run or implementation is complete unless the
controller-owned verifier evidence passes.
