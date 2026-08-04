# Harness Design

## Authority boundaries

- `HarnessController` is the only owner of run and step state.
- Agent output is a structured report, never completion evidence by itself.
- `Verifier` command results and Git snapshots decide whether work completed.
- The progress UI is non-authoritative and exposes no mutation endpoint.

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
