# Harness Design

This repository-level document defines the authority and package boundaries of
the Personal Codex Harness.

## Authority boundaries

- `HarnessController` is the only owner of run and step state.
- Agent output is a structured report, never completion evidence by itself.
- `Verifier` command results and Git snapshots decide whether work completed.
- The progress UI is non-authoritative and exposes no mutation endpoint.

## Linux/WSL runtime boundary

The supported host platform is Linux, including WSL. Every production Codex
invocation uses one dedicated persistent home outside the repository. Its
default is `${XDG_STATE_HOME:-$HOME/.local/state}/personal-codex-harness/codex-home`;
`HARNESS_CODEX_HOME` may select another absolute path. The `$harness-setup` skill
creates the leaf directory with mode `0700`, runs the separate login flow, and
atomically merges the exact path into the outer Codex configuration. It never
copies authentication material from another home or replaces unrelated config.

The outer Codex session may write the repository and exactly that dedicated
state root. The Python controller itself is not launched with unrestricted host
access. Before `plan`, `approve`, `run`, or `review` can mutate state, CLI
preflight requires the dedicated directory to be writable in the active sandbox
and requires a non-symlink `auth.json` inaccessible to group and other users.
It also executes `codex login status` against that exact home before state
changes.

Nested Codex processes receive the dedicated path as `CODEX_HOME`, run
ephemerally without user configuration or rules, use `approval_policy=never`,
and reset `sandbox_workspace_write.writable_roots` to an empty array. Network is
disabled for inner command execution. The role sandbox remains authoritative:
planner and reviewer are read-only; executor is workspace-write only in its
assigned working tree.

Controller-owned verification commands run through `codex sandbox` with the
Linux `:workspace` permission profile, no extra writable roots, and no network.
The verifier removes `CODEX_HOME` from the verified command's environment. This
prevents verification from inheriting the outer state directory as a write
target, while Git and run-metadata guards still detect repository mutations.
Harness runtime-path and OpenAI API-key variables are also excluded from nested
agent shells and verification commands.
The workspace profile is not a complete confidentiality boundary for all
host-readable files, so Acceptance Criteria remain explicit, shell-free,
user-reviewed argv.

Agent event history, structured final payloads, individual command-tool output,
and verifier stdout/stderr use independent byte budgets. Oversized command output
is stored as a bounded head/tail summary with its original byte count. Reaching
the event-history budget is an audit warning rather than completion evidence:
the runner still consumes and parses the entire stream, while malformed events,
reader failures, oversized final payloads, Git guards, and controller-owned
verification remain authoritative failures. Runtime diagnostics also reject
configured agent or subagent models absent from the Codex bundled model catalog.

## Package boundaries

- `harness/domain/` owns validated plans, execution results, reviews, run state,
  and shared contract validation.
- `harness/orchestration/` owns workflow coordination. Only
  `HarnessController` mutates run or step state; the planning, reviewing,
  sequential, and parallel modules provide stateless policy and scheduling
  helpers.
- `harness/agents/` owns Codex CLI invocation, JSONL collection, and bounded
  subprocess stream handling.
- `harness/safety/` owns Git boundaries, independent command verification, and
  isolated workspace integration.
- `harness/storage/` owns run artifacts and atomic persistence.
- `harness/ui/` owns the read-only HTTP dashboard, progress broker, and static
  assets.
- `harness/cli.py` and `harness/config.py` remain top-level composition roots.

## Model roles

Role profiles are loaded from `.harness/config.toml` and passed explicitly to
every Codex invocation. Planner and reviewer calls may enable bounded read-only
subagents. Executor calls disable subagents unless controller-level isolated
parallel writers are explicitly enabled.

## Planning and dependencies

Plans use sequential IDs and may declare `depends_on` with earlier step IDs.
This restriction makes the dependency graph acyclic without requiring a
separate cycle-breaking algorithm. Sequential mode follows plan order.

## Run identity and skill workflow

One user goal owns one run ID. Its draft and approved plan, step attempts,
verification evidence, terminal state, and one or more advisory reviews remain
under that run. A materially different goal or a changed post-approval Git
baseline requires a new run instead of reusing the old identity.

`$harness-plan` is the normal user entry point. It checks prerequisites, creates
one draft run, and starts that run's read-only dashboard without approving or
editing project files. Only an explicit `$harness-approve <run-id>` invocation
authorizes the workflow layer to call `approve`, `run`, and then `review` for
that exact run. These commands remain separate controller operations; the skill
only sequences them and cannot replace controller-owned evidence or state.
If runtime setup is missing, the planning skill routes the user to
`$harness-setup`. That setup skill may request escalation only for the exact
directory-creation, dedicated-login, or atomic config-merge command. It never
escalates `plan`, `run`, or `review` as a whole.

## Review

`review <run-id>` runs Codex read-only with the reviewer profile. It examines
the plan, state, Git changes, and controller evidence and returns structured
findings. The controller verifies that Git and run state did not change, stores
the report under `evidence/`, and leaves terminal state unchanged.

## Parallel readers

Planner and reviewer calls can ask Codex to delegate independent repository
exploration to bounded read-only subagents. The Codex JSONL stream remains the
evidence source for child activity and aggregate usage. Executors explicitly
disable this route so a workspace-write turn cannot create uncontrolled writers.

## Parallel writers

Parallel writers are disabled by default. When enabled, the controller:

1. Selects dependency-ready steps with pairwise-disjoint `allowed_paths`.
2. Copies the approved current repository into one temporary workspace per step.
3. Runs and retries each agent only inside its copy.
4. Runs controller-owned step verification in the isolated workspace.
5. Rejects Git metadata, index, out-of-scope, deletion, symlink, and special-file changes.
6. Integrates a batch only when every worker succeeded and the real Git baseline
   remained unchanged.
7. Re-runs every step Acceptance Criteria in the real working tree, followed by
   final verification.

The controller never stages, commits, pushes, or rewrites the real `.git`
directory during integration.

## Progress UI

`run --ui` starts a loopback-only HTTP server and an in-memory `ProgressBroker`.
Codex events are published to the broker as they are read. The browser polls a
read-only snapshot endpoint every 750 ms and combines live telemetry with
controller-owned `state.json`, `events.jsonl`, and evidence artifacts.

The UI uses live events only for presentation. Refreshing the page reconstructs
authoritative status and historical activity from run artifacts. The server
rejects POST requests, uses a restrictive Content Security Policy, and never
binds beyond `127.0.0.1`.

The standalone `ui <run-id> --open-browser` command may ask the operating system
to open the dashboard. Browser-launch failure is non-fatal because the emitted
loopback URL remains usable. Skill-started dashboards run independently from
execution so automatic review can follow a terminal run while the UI stays up.
