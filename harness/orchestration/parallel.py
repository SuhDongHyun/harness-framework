from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..agents import AgentRunResult
from ..domain import PlanStep, StepResult
from ..safety.git_guard import GitGuard, paths_outside_allowed
from ..safety.verifier import VerificationResult
from ..safety.workspace import WorkspaceChange, allowed_paths_overlap


@dataclass(frozen=True)
class IsolatedStepOutcome:
    step: PlanStep
    attempt: int
    agent_result: AgentRunResult
    step_result: StepResult | None
    verification: VerificationResult | None
    changes: tuple[WorkspaceChange, ...]
    error: str | None


def select_parallel_batch(
    ready: list[PlanStep], max_workers: int
) -> list[PlanStep]:
    selected: list[PlanStep] = []
    for step in ready:
        if any(
            allowed_paths_overlap(step.allowed_paths, other.allowed_paths)
            for other in selected
        ):
            continue
        selected.append(step)
        if len(selected) == max_workers:
            break
    return selected or [ready[0]]


def isolated_safety_error(
    guard: GitGuard,
    before,
    after,
    allowed_paths: Sequence[str],
) -> str | None:
    if before.branch != after.branch or before.head != after.head:
        return "isolated agent changed Git branch or HEAD"
    if before.index_fingerprint != after.index_fingerprint:
        return "isolated agent changed Git index"
    changed = guard.changed_paths(before, after)
    outside = paths_outside_allowed(changed, allowed_paths)
    if outside:
        return "files changed outside allowed_paths: " + ", ".join(sorted(outside))
    return None


def failed_agent_result(message: str) -> AgentRunResult:
    return AgentRunResult(
        exit_code=1,
        final_payload=None,
        stderr=message,
        timed_out=False,
        terminal_event=None,
    )
