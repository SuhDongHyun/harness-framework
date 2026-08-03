from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import HarnessConfig
from .git_guard import GitGuard, paths_outside_allowed
from .models import Plan, PlanStep, RunState, StepResult, ValidationError
from .runner import AgentRunner, AgentRunRequest, AgentRunResult
from .store import RunStore
from .verifier import VerificationResult, Verifier


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class ControllerPaths:
    plan_schema: Path
    step_result_schema: Path


class HarnessController:
    def __init__(
        self,
        *,
        root: Path,
        store: RunStore,
        runner: AgentRunner,
        verifier: Verifier,
        git_guard: GitGuard,
        config: HarnessConfig,
        run_id_factory: Callable[[str], str] | None = None,
    ):
        self.root = root.resolve()
        self.store = store
        self.runner = runner
        self.verifier = verifier
        self.git_guard = git_guard
        self.config = config
        self.paths = ControllerPaths(
            plan_schema=self.root / "schemas" / "plan.schema.json",
            step_result_schema=self.root / "schemas" / "step-result.schema.json",
        )
        self.run_id_factory = run_id_factory or _default_run_id

    def plan(self, goal: str) -> str:
        clean_goal = goal.strip()
        if not clean_goal:
            raise HarnessError("goal must not be empty")
        run_id = self.run_id_factory(clean_goal)
        self.store.create_run(run_id, clean_goal)
        request = AgentRunRequest(
            prompt=self._planning_prompt(clean_goal),
            sandbox="read-only",
            output_schema=self.paths.plan_schema,
            cwd=self.root,
            event_log=self.store.evidence_path(run_id, "plan-events.jsonl"),
            timeout_seconds=self.config.timeout_seconds,
            max_output_bytes=self.config.max_output_bytes,
        )
        result = self.runner.run(request)
        if not result.process_succeeded:
            self.store.append_event(
                run_id,
                {
                    "type": "plan.failed",
                    "exit_code": result.exit_code,
                    "terminal_event": result.terminal_event,
                    "malformed_event_count": result.malformed_event_count,
                    "output_truncated": result.output_truncated,
                    "reader_failed": result.reader_failed,
                    "stderr": result.stderr,
                },
            )
            raise HarnessError("Codex did not produce a completed structured plan")
        try:
            plan = Plan.from_dict(result.final_payload)
        except ValidationError as error:
            raise HarnessError(f"invalid plan result: {error}") from error
        if plan.status != "draft":
            raise HarnessError("new plan must have draft status")
        if plan.goal != clean_goal:
            raise HarnessError("planned goal does not match the requested goal")
        self.store.write_json_atomic(run_id, "plan.json", plan.to_dict())
        for index, step in enumerate(plan.steps):
            self.store.write_step_text(
                run_id,
                index,
                step.name,
                self._step_document(step),
            )
        state = RunState.from_plan(run_id, plan)
        self._write_state(state)
        self.store.append_event(run_id, {"type": "plan.created"})
        return run_id

    def approve(self, run_id: str) -> None:
        plan = self._read_plan(run_id)
        state = self.status(run_id)
        if plan.status != "draft" or state.status != "draft":
            raise HarnessError("only a draft plan can be approved")
        approved_plan = plan.with_status("approved")
        snapshot = self.git_guard.snapshot()
        self.store.clear_step_documents(run_id)
        for index, step in enumerate(approved_plan.steps):
            self.store.write_step_text(
                run_id,
                index,
                step.name,
                self._step_document(step),
            )
        self.store.write_json_atomic(run_id, "plan.json", approved_plan.to_dict())
        state = RunState.from_plan(run_id, approved_plan)
        state.status = "approved"
        state.plan_sha256 = approved_plan.sha256()
        state.approved_git_fingerprint = snapshot.fingerprint
        self._write_state(state)
        self.store.write_json_atomic(run_id, "approved-git.json", snapshot.to_dict())
        self.store.append_event(
            run_id,
            {
                "type": "plan.approved",
                "plan_sha256": state.plan_sha256,
                "git_fingerprint": snapshot.fingerprint,
            },
        )

    def run(self, run_id: str) -> RunState:
        plan = self._read_plan(run_id)
        state = self.status(run_id)
        if plan.status != "approved" or state.status != "approved":
            raise HarnessError("run requires an approved plan")
        if plan.sha256() != state.plan_sha256:
            raise HarnessError("plan changed after approval")
        current_snapshot = self.git_guard.snapshot()
        if current_snapshot.fingerprint != state.approved_git_fingerprint:
            state.status = "blocked"
            state.terminal_result = "blocked"
            state.blocked_reason = "Git working tree changed after plan approval"
            state.required_action = "Review the changes and create a new approved run"
            self._write_state(state)
            self.store.append_event(
                run_id,
                {
                    "type": "run.blocked",
                    "reason": state.blocked_reason,
                },
            )
            return state

        state.status = "running"
        self._write_state(state)
        self.store.append_event(run_id, {"type": "run.running"})
        previous_summaries: list[str] = []
        for plan_step in plan.steps:
            terminal_state = self._run_step(
                run_id, state, plan_step, previous_summaries
            )
            if terminal_state is not None:
                return terminal_state
        return self._finish_run(run_id, state, plan)

    def _run_step(
        self,
        run_id: str,
        state: RunState,
        plan_step: PlanStep,
        previous_summaries: list[str],
    ) -> RunState | None:
        state_step = state.step(plan_step.id)
        last_error: str | None = None
        for attempt in range(1, self.config.max_retries + 1):
            state.current_step = plan_step.id
            state_step.update(status="running", attempts=attempt, error=None)
            self._write_state(state)
            self.store.append_event(
                run_id,
                {"type": "step.running", "step": plan_step.id, "attempt": attempt},
            )

            agent_result, safety_error = self._run_agent_attempt(
                run_id, plan_step, attempt, previous_summaries, last_error
            )
            if safety_error is not None:
                return self._fail_run(state, state_step, safety_error)

            parsed, parse_error = self._parse_step_result(agent_result)
            if parse_error is not None:
                last_error = parse_error
            elif parsed is not None and parsed.outcome == "blocked":
                return self._block_step(state, state_step, parsed)
            elif parsed is not None and parsed.outcome == "failed":
                last_error = parsed.error_message or "Codex reported failure"
            elif parsed is not None:
                state_step["status"] = "verifying"
                self._write_state(state)
                self.store.append_event(
                    run_id,
                    {
                        "type": "step.verifying",
                        "step": plan_step.id,
                        "attempt": attempt,
                    },
                )
                verified, safety_error = self._verify_with_guard(
                    run_id, plan_step.acceptance_commands
                )
                self.store.write_evidence_atomic(
                    run_id,
                    f"{plan_step.id}-attempt-{attempt:02d}-verification.json",
                    verified.to_dict(),
                )
                if safety_error is not None:
                    return self._fail_run(state, state_step, safety_error)
                if verified.ok:
                    self._complete_step(
                        state, state_step, parsed, attempt, previous_summaries
                    )
                    return None
                last_error = verified.failure_summary()

            if attempt == self.config.max_retries:
                return self._fail_run(
                    state,
                    state_step,
                    last_error or "step failed without an error",
                )
            state_step.update(status="retrying", error=last_error)
            self._write_state(state)
            self.store.append_event(
                run_id,
                {
                    "type": "step.retrying",
                    "step": plan_step.id,
                    "attempt": attempt,
                    "error": last_error,
                },
            )
        return None

    def _run_agent_attempt(
        self,
        run_id: str,
        plan_step: PlanStep,
        attempt: int,
        previous_summaries: list[str],
        last_error: str | None,
    ) -> tuple[AgentRunResult, str | None]:
        before = self.git_guard.snapshot()
        run_files = self.store.capture_runs_files()
        request = AgentRunRequest(
            prompt=self._execution_prompt(plan_step, previous_summaries, last_error),
            sandbox="workspace-write",
            output_schema=self.paths.step_result_schema,
            cwd=self.root,
            event_log=self.store.evidence_path(
                run_id, f"{plan_step.id}-attempt-{attempt:02d}.jsonl"
            ),
            timeout_seconds=self.config.timeout_seconds,
            max_output_bytes=self.config.max_output_bytes,
        )
        result = self.runner.run(request)
        after = self.git_guard.snapshot()
        changed_paths = self.git_guard.changed_paths(before, after)

        runs_after = self.store.capture_runs_files()
        expected_runs = dict(run_files)
        expected_event = f"{run_id}/evidence/{request.event_log.name}"
        if expected_event in runs_after:
            expected_runs[expected_event] = runs_after[expected_event]
        changed_run_files = _changed_keys(expected_runs, runs_after)
        metadata_changed = any("/evidence/" not in path for path in changed_run_files)
        evidence_changed = any("/evidence/" in path for path in changed_run_files)
        if changed_run_files:
            self.store.restore_runs_files(expected_runs)

        self.store.write_evidence_atomic(
            run_id,
            f"{plan_step.id}-attempt-{attempt:02d}-agent.json",
            {
                "exit_code": result.exit_code,
                "terminal_event": result.terminal_event,
                "timed_out": result.timed_out,
                "malformed_event_count": result.malformed_event_count,
                "output_truncated": result.output_truncated,
                "reader_failed": result.reader_failed,
                "stderr": result.stderr,
                "final_payload": result.final_payload,
                "changed_paths": sorted(changed_paths),
                "controller_metadata_changed": metadata_changed,
                "previous_evidence_changed": evidence_changed,
            },
        )

        safety_error: str | None = None
        if metadata_changed:
            safety_error = "Codex changed controller-owned run metadata"
        elif evidence_changed:
            safety_error = "Codex changed previous controller verification evidence"
        elif before.branch != after.branch or before.head != after.head:
            safety_error = "Git branch or HEAD changed during Codex execution"
        elif before.index_fingerprint != after.index_fingerprint:
            safety_error = "Git index changed during Codex execution"
        else:
            outside = paths_outside_allowed(changed_paths, plan_step.allowed_paths)
            if outside:
                safety_error = "files changed outside allowed_paths: " + ", ".join(
                    sorted(outside)
                )
        return result, safety_error

    def _parse_step_result(
        self, result: AgentRunResult
    ) -> tuple[StepResult | None, str | None]:
        if not result.process_succeeded:
            return None, self._agent_failure(result)
        try:
            return StepResult.from_dict(result.final_payload), None
        except ValidationError as error:
            return None, f"invalid step result: {error}"

    def _block_step(
        self,
        state: RunState,
        state_step: dict[str, object],
        result: StepResult,
    ) -> RunState:
        reason = result.blocked_reason or "Codex reported a blocker"
        state_step.update(status="blocked", error=result.blocked_reason)
        state.status = "blocked"
        state.terminal_result = "blocked"
        state.blocked_reason = reason
        state.required_action = result.required_action
        self._write_state(state)
        event = {"step": state_step["id"], "reason": reason}
        self.store.append_event(state.run_id, {"type": "step.blocked", **event})
        self.store.append_event(state.run_id, {"type": "run.blocked", **event})
        return state

    def _complete_step(
        self,
        state: RunState,
        state_step: dict[str, object],
        result: StepResult,
        attempt: int,
        previous_summaries: list[str],
    ) -> None:
        state_step.update(status="completed", summary=result.summary, error=None)
        self._write_state(state)
        previous_summaries.append(f"{state_step['id']}: {result.summary}")
        self.store.append_event(
            state.run_id,
            {"type": "step.completed", "step": state_step["id"], "attempt": attempt},
        )

    def _finish_run(self, run_id: str, state: RunState, plan: Plan) -> RunState:
        state.status = "verifying"
        state.current_step = None
        self._write_state(state)
        self.store.append_event(run_id, {"type": "run.verifying"})
        final_result, safety_error = self._verify_with_guard(
            run_id, plan.final_acceptance_commands
        )
        self.store.write_evidence_atomic(
            run_id, "final-verification.json", final_result.to_dict()
        )
        error = safety_error or (
            None if final_result.ok else final_result.failure_summary()
        )
        if error is not None:
            if state.steps:
                state.steps[-1]["error"] = error
            state.status = "failed"
            state.terminal_result = "failed"
            self._write_state(state)
            self.store.append_event(run_id, {"type": "run.failed", "error": error})
            return state
        state.status = "completed"
        state.terminal_result = "completed"
        self._write_state(state)
        self.store.append_event(run_id, {"type": "run.completed"})
        return state

    def status(self, run_id: str) -> RunState:
        return RunState.from_dict(self.store.read_json(run_id, "state.json"))

    def _read_plan(self, run_id: str) -> Plan:
        return Plan.from_dict(self.store.read_json(run_id, "plan.json"))

    def _write_state(self, state: RunState) -> None:
        self.store.write_json_atomic(state.run_id, "state.json", state.to_dict())

    def _verify_with_guard(
        self, run_id: str, commands: Sequence[Sequence[str]]
    ) -> tuple[VerificationResult, str | None]:
        before = self.git_guard.snapshot()
        run_files = self.store.capture_runs_files()
        result = self.verifier.verify(commands, self.root)
        after = self.git_guard.snapshot()

        runs_after = self.store.capture_runs_files()
        if runs_after != run_files:
            self.store.restore_runs_files(run_files)
            return result, "verification changed controller-owned run metadata"
        if before.branch != after.branch or before.head != after.head:
            return result, "verification changed Git branch or HEAD"
        if before.index_fingerprint != after.index_fingerprint:
            return result, "verification changed Git index"
        changed_paths = self.git_guard.changed_paths(before, after)
        if changed_paths:
            return (
                result,
                "verification changed repository paths: "
                + ", ".join(sorted(changed_paths)),
            )
        return result, None

    def _fail_run(
        self,
        state: RunState,
        state_step: dict[str, object],
        message: str,
    ) -> RunState:
        state_step["status"] = "failed"
        state_step["error"] = message
        state.status = "failed"
        state.terminal_result = "failed"
        self._write_state(state)
        self.store.append_event(
            state.run_id,
            {
                "type": "step.failed",
                "step": state_step["id"],
                "error": message,
            },
        )
        self.store.append_event(
            state.run_id,
            {
                "type": "run.failed",
                "step": state_step["id"],
                "error": message,
            },
        )
        return state

    @staticmethod
    def _agent_failure(result: AgentRunResult) -> str:
        if result.timed_out:
            return "Codex execution timed out"
        if result.reader_failed:
            return result.stderr or "Codex output reader failed"
        if result.output_truncated:
            return "Codex output exceeded the configured evidence limit"
        if result.malformed_event_count:
            return (
                f"Codex emitted malformed JSONL events: {result.malformed_event_count}"
            )
        if result.stderr:
            return result.stderr
        return (
            f"Codex execution failed: exit={result.exit_code}, "
            f"terminal_event={result.terminal_event!r}"
        )

    @staticmethod
    def _planning_prompt(goal: str) -> str:
        return (
            "Create an implementation plan for the goal below. Do not edit files. "
            "Return only the JSON object required by the output schema. Use step IDs "
            "step-00, step-01, and so on. Keep each step focused, list only required "
            "read_files, constrain allowed_paths, and express every verification as "
            "an argv array without shell operators.\n\n"
            f"Goal:\n{goal}"
        )

    @staticmethod
    def _step_document(step: PlanStep) -> str:
        commands = "\n".join(
            f"- `{' '.join(command)}`" for command in step.acceptance_commands
        )
        return (
            f"# {step.id}: {step.name}\n\n"
            f"## Objective\n\n{step.objective}\n\n"
            "## Read files\n\n"
            + "\n".join(f"- `{path}`" for path in step.read_files)
            + "\n\n## Allowed paths\n\n"
            + "\n".join(f"- `{path}`" for path in step.allowed_paths)
            + "\n\n## Acceptance commands\n\n"
            + (commands or "- None")
            + "\n"
        )

    @staticmethod
    def _execution_prompt(
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


def _default_run_id(goal: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:24] or "task"
    timestamp = datetime.now(UTC).strftime("%Y%m%dt%H%M%S%f")
    return f"run-{timestamp}-{secrets.token_hex(3)}-{slug}"


def _changed_keys(before: dict[str, bytes], after: dict[str, bytes]) -> set[str]:
    return {
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    }
