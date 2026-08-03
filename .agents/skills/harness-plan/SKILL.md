---
name: harness-plan
description: Create and inspect a draft execution plan for the Personal Codex Harness without editing project code. Use when a user asks to plan a coding goal, split work into executable steps, define allowed paths, or prepare Acceptance Criteria before approval.
---

# Harness Plan

1. Read `AGENTS.md`, `docs/HARNESS_DESIGN.md`, and only the project documents relevant to the goal.
2. Clarify only missing information that would materially change scope or safety.
3. Run `python3 scripts/harness.py plan "<goal>"`.
4. Read `.harness/runs/<run-id>/plan.json` and the generated step documents.
5. Check that each step has one objective, bounded `allowed_paths`, explicit forbidden changes, and shell-free argv Acceptance Criteria.
6. Report the run ID, step summary, risks, and commands that will prove completion.
7. Do not approve the plan and do not edit project files. Approval belongs to the user-facing `approve` command.
