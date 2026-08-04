---
name: harness-plan
description: Create and inspect one draft Personal Codex Harness run for a coding goal, verify prerequisites, and open its read-only dashboard. Use when a user asks to plan or start a new harness-managed coding task, split work into executable steps, define allowed paths, or prepare Acceptance Criteria before approval.
---

# Harness Plan

1. Read `AGENTS.md`, `HARNESS_DESIGN.md`, and only the project documents relevant to the goal.
2. Clarify only missing information that would materially change scope or safety. Preserve the user's goal as one new run; do not combine unrelated goals under one run ID.
3. Run `python3 scripts/harness.py doctor`. Stop and report failed checks when it returns nonzero.
4. Run `python3 scripts/harness.py plan "<goal>"` without paraphrasing the goal. This command launches a nested Codex process that must initialize its own `$CODEX_HOME` before the planner's `read-only` sandbox applies. Request scoped sandbox escalation for this exact command before the first attempt; keep the nested planner `read-only` and never bypass its sandbox. If escalation is denied, stop without retrying inside the outer sandbox.
5. Read `.harness/runs/<run-id>/plan.json` and the generated step documents.
6. Check that each step has one objective, bounded `allowed_paths`, explicit forbidden changes, and shell-free argv Acceptance Criteria.
7. Start `python3 scripts/harness.py ui <run-id> --open-browser` in a managed background terminal. Do not wait for the dashboard process to exit. If the browser cannot open, provide the emitted localhost URL.
8. Report the run ID, dashboard URL, step summary, risks, and verification commands. Tell the user to approve with `$harness-approve <run-id>`.
9. Do not approve the plan and do not edit project files.
