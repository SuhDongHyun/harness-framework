from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime

from ..domain import PlanStep


def default_run_id(goal: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:24] or "task"
    timestamp = datetime.now(UTC).strftime("%Y%m%dt%H%M%S%f")
    return f"run-{timestamp}-{secrets.token_hex(3)}-{slug}"


def planning_prompt(goal: str, parallel_readers: int = 0) -> str:
    parallel = (
        " Delegate independent read-only repository exploration to at most "
        f"{parallel_readers} subagents. Do not delegate writes or allow recursive "
        "delegation. Wait for their summaries before producing the plan."
        if parallel_readers
        else ""
    )
    return (
        "Create an implementation plan for the goal below. Do not edit files. "
        "Return only the JSON object required by the output schema. Use step IDs "
        "step-00, step-01, and so on. Keep each step focused, list only required "
        "read_files, declare depends_on using only earlier step IDs, constrain "
        "allowed_paths, and express every verification as an argv array without "
        "shell operators. The output goal field must exactly equal this JSON "
        f"string, without paraphrasing: {json.dumps(goal, ensure_ascii=False)}."
        f"{parallel}\n\n"
        f"Goal:\n{goal}"
    )


def step_document(step: PlanStep) -> str:
    commands = "\n".join(
        f"- `{' '.join(command)}`" for command in step.acceptance_commands
    )
    return (
        f"# {step.id}: {step.name}\n\n"
        f"## Objective\n\n{step.objective}\n\n"
        "## Dependencies\n\n"
        + ("\n".join(f"- `{value}`" for value in step.depends_on) or "- None")
        + "\n\n"
        "## Read files\n\n"
        + "\n".join(f"- `{path}`" for path in step.read_files)
        + "\n\n## Allowed paths\n\n"
        + "\n".join(f"- `{path}`" for path in step.allowed_paths)
        + "\n\n## Acceptance commands\n\n"
        + (commands or "- None")
        + "\n"
    )
