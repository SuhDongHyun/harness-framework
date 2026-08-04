---
name: harness-approve
description: Explicitly approve one Personal Codex Harness run, execute its approved plan, and automatically perform an independent review. Use only when the user names this skill with an exact run ID and intends to authorize execution after reviewing the draft plan.
---

# Harness Approve

1. Require exactly one run ID from an explicit `$harness-approve <run-id>` request. If it is missing or ambiguous, request it and stop.
2. Read `AGENTS.md`, `HARNESS_DESIGN.md`, and the run's `plan.json` and `state.json` without changing them.
3. Run `python3 scripts/harness.py status <run-id>`. Never approve a different or inferred run ID.
4. Run `python3 scripts/harness.py doctor`. Stop before approval and report the failed setup or login action when it returns nonzero.
5. Ensure a dashboard is available. If this thread has no live dashboard terminal for the run, start `python3 scripts/harness.py ui <run-id> --open-browser` in a managed background terminal and do not wait for it to exit.
6. If the state is `draft`, run `python3 scripts/harness.py approve <run-id>`. Treat the explicit skill invocation as authorization for this approval and the following execution. If the state is already `approved`, continue without approving again.
7. If the state is `approved`, run `python3 scripts/harness.py run <run-id>` and capture its JSON and exit code. Never escalate `run` or bypass the nested executor's `workspace-write` sandbox. Do not stop merely because a terminal `failed` or `blocked` result returns nonzero.
8. Do not start a second writer when the state is `running` or `verifying`. Report the active state and leave the dashboard running.
9. After the run reaches `completed`, `failed`, or `blocked`, run `python3 scripts/harness.py review <run-id>` exactly once, regardless of which terminal result occurred. Never escalate `review` or bypass the nested reviewer's `read-only` sandbox. A review remains advisory and must not rewrite controller state.
10. Run `python3 scripts/harness.py status <run-id>` once more. Report the final controller status, review findings, evidence paths, required action when blocked, and the dashboard URL. Leave the dashboard running.
