from __future__ import annotations

import json

from ..agents import AgentRunResult
from ..domain import PlanStep, StepResult, ValidationError


def execution_prompt(
    step: PlanStep, summaries: list[str], last_error: str | None
) -> str:
    context = "\n".join(f"- {summary}" for summary in summaries) or "- None"
    retry = last_error or "None"
    return (
        f"Execute only {step.id} ({step.name}).\n\n"
        f"Objective: {step.objective}\n"
        f"Read first: {json.dumps(step.read_files, ensure_ascii=False)}\n"
        f"Allowed paths: {json.dumps(step.allowed_paths, ensure_ascii=False)}\n"
        f"Forbidden changes: {json.dumps(step.forbidden_changes, ensure_ascii=False)}\n\n"
        f"Completed step summaries:\n{context}\n\n"
        f"Previous failure evidence:\n{retry}\n\n"
        "Do not edit .harness run metadata. Make the minimum required changes. "
        "Return only the JSON object required by the output schema. The controller "
        "will independently run acceptance commands and decide completion."
    )


def parse_step_result(
    result: AgentRunResult,
) -> tuple[StepResult | None, str | None]:
    if not result.process_succeeded:
        return None, agent_failure(result)
    try:
        return StepResult.from_dict(result.final_payload), None
    except ValidationError as error:
        return None, f"invalid step result: {error}"


def agent_failure(result: AgentRunResult) -> str:
    if result.timed_out:
        return "Codex execution timed out"
    if result.reader_failed:
        return result.stderr or "Codex output reader failed"
    if result.final_payload_truncated:
        return "Codex final payload exceeded the configured payload limit"
    if result.malformed_event_count:
        return f"Codex emitted malformed JSONL events: {result.malformed_event_count}"
    if result.stderr:
        return result.stderr
    return (
        f"Codex execution failed: exit={result.exit_code}, "
        f"terminal_event={result.terminal_event!r}"
    )
