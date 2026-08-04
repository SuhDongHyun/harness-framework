---
name: harness-plan
description: Create and inspect one draft Personal Codex Harness run for a coding goal, verify prerequisites, and open its read-only dashboard. Use when a user asks to plan or start a new harness-managed coding task, split work into executable steps, define allowed paths, or prepare Acceptance Criteria before approval.
---

# Harness Plan

1. Read `AGENTS.md`, `HARNESS_DESIGN.md`, and only the project documents relevant to the goal.
2. Clarify only missing information that would materially change scope or safety. Preserve the user's goal as one new run; do not combine unrelated goals under one run ID.
3. Run `python3 scripts/harness.py doctor`. If the Harness Codex runtime home or login check fails, tell the user to invoke `$harness-setup` and stop. Do not reproduce its setup, authentication, or config-editing workflow inside this skill. Stop and report any other failed check.
4. Run `python3 scripts/harness.py plan "<goal>"` without paraphrasing the goal. Never escalate the `plan` command or bypass the nested planner's `read-only` sandbox. The CLI preflight must confirm the dedicated runtime home before it creates run state.
5. Read `.harness/runs/<run-id>/plan.json` and the generated step documents.
6. Check that each step has one objective, bounded `allowed_paths`, explicit forbidden changes, and shell-free argv Acceptance Criteria.
7. Start `python3 scripts/harness.py ui <run-id> --open-browser` in a managed background terminal. Do not wait for the dashboard process to exit. If the browser cannot open, provide the emitted localhost URL.
8. Report the run ID, dashboard URL, step summary, risks, and verification commands. Tell the user to approve with `$harness-approve <run-id>`.
9. Do not approve the plan and do not edit project files.
