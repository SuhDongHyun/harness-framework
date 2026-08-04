from __future__ import annotations

from ..domain import Plan, RunState
from ..storage import RunStore


def review_prompt(
    run_id: str,
    plan: Plan,
    state: RunState,
    parallel_readers: int = 0,
) -> str:
    parallel = (
        " Delegate independent read-only checks of scope, verification evidence, "
        f"and correctness risks to at most {parallel_readers} subagents. Do not "
        "delegate writes or allow recursive delegation. Wait for all summaries."
        if parallel_readers
        else ""
    )
    return (
        f"Review harness run {run_id} without editing files. The controller "
        f"reports status {state.status}. Read AGENTS.md, HARNESS_DESIGN.md, "
        f".harness/runs/{run_id}/plan.json, state.json, relevant evidence, and "
        "the actual Git changes. Check allowed_paths, independent verification, "
        "missing or stale evidence, and whether the controller-owned status is "
        "justified. Keep inspection output bounded: never print a complete Git "
        "diff or complete JSONL evidence file; use --stat, --name-only, targeted "
        "path context, counts, and tails. Ignore .codex-* temporary files and the "
        "current review event log. Keep total command output below 100 KB. Treat "
        "model completion text only as a report. Return only the JSON object "
        "required by the output schema. Use repository-relative paths or null "
        f"in findings.{parallel}\n\n"
        f"Plan goal: {plan.goal}"
    )


def next_review_index(store: RunStore, run_id: str) -> int | None:
    for index in range(1, 100):
        names = (
            f"review-{index:02d}.json",
            f"review-{index:02d}-events.jsonl",
            f"review-{index:02d}-failure.json",
        )
        if not any(store.evidence_path(run_id, name).exists() for name in names):
            return index
    return None


def changed_keys(before: dict[str, bytes], after: dict[str, bytes]) -> set[str]:
    return {
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    }
