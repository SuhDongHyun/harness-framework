---
name: harness-review
description: Review Personal Codex Harness runs and repository changes without editing them. Use after plan creation or execution to check approved scope, changed paths, controller state, retry evidence, Acceptance Criteria, and whether a completion claim is justified.
---

# Harness Review

1. Read `AGENTS.md`, `HARNESS_DESIGN.md`, the run's `plan.json`, `state.json`, and relevant evidence files.
2. Compare actual Git changes with every step's `allowed_paths`.
3. Confirm the controller, not model output, owns terminal state.
4. Check each completed step has passing independent verification evidence.
5. Check `final-verification.json` before accepting a completed run.
6. Treat missing, malformed, stale, or failed evidence as a finding.
7. Report findings by severity with exact paths and evidence. State explicitly whether the run is `completed`, `failed`, or `blocked`.
8. Do not edit files, rerun a failed workflow, commit, or push during review.
