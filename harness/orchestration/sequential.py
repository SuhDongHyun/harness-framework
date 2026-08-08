from __future__ import annotations

import json

from ..agents import AgentRunResult
from ..domain import PlanStep, StepResult, ValidationError


def execution_prompt(
    step: PlanStep,
    summaries: list[str],
    last_error: str | None,
    *,
    network_access: bool,
) -> str:
    context = "\n".join(f"- {summary}" for summary in summaries) or "- None"
    retry = last_error or "None"
    network = "ENABLED" if network_access else "DISABLED"
    network_guidance = (
        "Outbound network use is approved. Do not add offline-only flags to "
        "honor a default-off policy, and do not report that network approval "
        "is missing."
        if network_access
        else "Do not attempt outbound network access."
    )
    return (
        f"Execute only {step.id} ({step.name}).\n\n"
        f"Objective: {step.objective}\n"
        f"Read first: {json.dumps(step.read_files, ensure_ascii=False)}\n"
        f"Allowed paths: {json.dumps(step.allowed_paths, ensure_ascii=False)}\n"
        f"Forbidden changes: {json.dumps(step.forbidden_changes, ensure_ascii=False)}\n\n"
        f"Effective executor network access: {network}. This value already "
        "reflects both repository policy and explicit approval for this step. "
        f"{network_guidance}\n\n"
        f"Completed step summaries:\n{context}\n\n"
        f"Previous failure evidence:\n{retry}\n\n"
        "Do not edit .harness run metadata. Make the minimum required changes. "
        "Report blocked only when progress genuinely requires user action or an "
        "external state change. Report ordinary command, dependency, test, or "
        "implementation failures as failed so the controller can retry them. "
        "Return only the JSON object required by the output schema. The controller "
        "will independently run acceptance commands and decide completion."
    )


def contradictory_network_blocker(
    result: StepResult, *, network_access: bool
) -> str | None:
    """Reject executor blockers contradicted by controller-owned network policy."""
    if result.outcome != "blocked" or not network_access:
        return None
    report = "\n".join(
        value
        for value in (
            result.error_message,
            result.blocked_reason,
            result.required_action,
        )
        if value
    ).casefold()
    missing_approval_markers = (
        "approve network",
        "network approval",
        "network-access approval",
        "network access approval",
        "grant network access",
        "enable network access",
        "network_access: true approval",
        "네트워크 승인",
        "네트워크 접근 승인",
        "네트워크 허용",
        "네트워크 활성화",
    )
    offline_failure_markers = ("--offline", "enotcached", "only-if-cached")
    claims_missing_approval = any(
        marker in report for marker in missing_approval_markers
    )
    used_offline_only_failure = any(
        marker in report for marker in offline_failure_markers
    )
    if not claims_missing_approval and not used_offline_only_failure:
        return None
    reason = result.blocked_reason or result.error_message or "network blocker"
    return (
        "Codex reported a network blocker contradicted by controller-owned "
        f"effective network_access=true: {reason}"
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
