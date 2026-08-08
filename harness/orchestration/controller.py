from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ..agents import AgentRunner, AgentRunRequest, AgentRunResult
from ..config import HarnessConfig
from ..domain import (
    Plan,
    PlanStep,
    ReviewResult,
    RunState,
    StepResult,
    ValidationError,
)
from ..safety.git_guard import GitError, GitGuard, paths_outside_allowed
from ..safety.verifier import VerificationResult, Verifier
from ..safety.workspace import (
    apply_workspace_changes,
    collect_workspace_changes,
    copy_repository,
)
from ..storage import RunStore
from .parallel import (
    IsolatedStepOutcome,
    failed_agent_result,
    isolated_safety_error,
    select_parallel_batch,
)
from .planning import default_run_id, planning_prompt, step_document
from .reviewing import changed_keys, next_review_index, review_prompt
from .sequential import (
    agent_failure,
    contradictory_network_blocker,
    execution_prompt,
    parse_step_result,
)


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class ControllerPaths:
    plan_schema: Path
    step_result_schema: Path
    review_result_schema: Path


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
            review_result_schema=self.root / "schemas" / "review-result.schema.json",
        )
        self.run_id_factory = run_id_factory or default_run_id

    def plan(self, goal: str) -> str:
        clean_goal = goal.strip()
        if not clean_goal:
            raise HarnessError("goal must not be empty")
        run_id = self.run_id_factory(clean_goal)
        self.store.create_run(run_id, clean_goal)
        request = AgentRunRequest(
            prompt=planning_prompt(
                clean_goal,
                self.config.parallel_readers.max_workers
                if self.config.parallel_readers.enabled
                else 0,
            ),
            sandbox="read-only",
            output_schema=self.paths.plan_schema,
            cwd=self.root,
            event_log=self.store.evidence_path(run_id, "plan-events.jsonl"),
            timeout_seconds=self.config.timeout_seconds,
            max_event_log_bytes=self.config.max_event_log_bytes,
            max_final_payload_bytes=self.config.max_final_payload_bytes,
            max_tool_output_bytes=self.config.max_tool_output_bytes,
            model=self.config.planner.model,
            reasoning_effort=self.config.planner.reasoning_effort,
            subagents_enabled=self.config.parallel_readers.enabled,
            max_subagents=self.config.parallel_readers.max_workers,
            subagent_model=self.config.parallel_readers.profile.model,
            subagent_reasoning_effort=(
                self.config.parallel_readers.profile.reasoning_effort
            ),
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
                    "event_log_truncated": result.event_log_truncated,
                    "final_payload_truncated": result.final_payload_truncated,
                    "reader_failed": result.reader_failed,
                    "model": request.model,
                    "reasoning_effort": request.reasoning_effort,
                    "usage": result.usage,
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
                step_document(step),
            )
        state = RunState.from_plan(run_id, plan)
        self._write_state(state)
        self.store.append_event(
            run_id,
            {
                "type": "plan.created",
                "model": request.model,
                "reasoning_effort": request.reasoning_effort,
                "usage": result.usage,
                "event_log_truncated": result.event_log_truncated,
            },
        )
        return run_id

    def approve(self, run_id: str) -> None:
        plan = self._read_plan(run_id)
        state = self.status(run_id)
        if plan.status != "draft" or state.status != "draft":
            raise HarnessError("only a draft plan can be approved")
        network_steps = [step.id for step in plan.steps if step.network_access is True]
        if network_steps and not self.config.network.executor_enabled:
            raise HarnessError(
                "plan requests executor network access for "
                + ", ".join(network_steps)
                + "; enable harness.network.executor_enabled before approval"
            )
        approved_plan = plan.with_status("approved")
        snapshot = self.git_guard.snapshot()
        self.store.clear_step_documents(run_id)
        for index, step in enumerate(approved_plan.steps):
            self.store.write_step_text(
                run_id,
                index,
                step.name,
                step_document(step),
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
        if self.config.parallel_writers.enabled:
            return self._run_parallel_steps(
                run_id, state, plan, previous_summaries
            )
        for plan_step in plan.steps:
            terminal_state = self._run_step(
                run_id, state, plan_step, previous_summaries
            )
            if terminal_state is not None:
                return terminal_state
        return self._finish_run(run_id, state, plan)

    def _run_parallel_steps(
        self,
        run_id: str,
        state: RunState,
        plan: Plan,
        previous_summaries: list[str],
    ) -> RunState:
        pending = list(plan.steps)
        completed_ids: set[str] = set()
        while pending:
            ready = [
                step
                for step in pending
                if set(step.depends_on).issubset(completed_ids)
            ]
            if not ready:
                return self._fail_run(
                    state,
                    state.step(pending[0].id),
                    "parallel scheduler found no dependency-ready step",
                )
            batch = select_parallel_batch(
                ready, self.config.parallel_writers.max_workers
            )
            main_before = self.git_guard.snapshot()
            state.current_step = None
            for step in batch:
                state_step = state.step(step.id)
                state_step.update(status="running", attempts=1, error=None)
                self.store.append_event(
                    run_id,
                    {
                        "type": "step.running",
                        "step": step.id,
                        "attempt": 1,
                        "isolated": True,
                        "model": self.config.executor.model,
                        "reasoning_effort": self.config.executor.reasoning_effort,
                        "network_access": self._executor_network_access(step),
                    },
                )
            self._write_state(state)
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                futures = [
                    pool.submit(
                        self._run_isolated_step,
                        run_id,
                        step,
                        list(previous_summaries),
                    )
                    for step in batch
                ]
                outcomes = [future.result() for future in futures]
            main_after = self.git_guard.snapshot()
            if main_before.fingerprint != main_after.fingerprint:
                return self._fail_parallel_batch(
                    state,
                    outcomes,
                    "Git working tree changed during isolated parallel execution",
                )
            for outcome in outcomes:
                self._write_isolated_evidence(run_id, outcome)
            failed = next(
                (
                    outcome
                    for outcome in outcomes
                    if outcome.error is not None
                    or outcome.step_result is None
                    or outcome.step_result.outcome != "completed"
                ),
                None,
            )
            if failed is not None:
                if (
                    failed.step_result is not None
                    and failed.step_result.outcome == "blocked"
                ):
                    for outcome in outcomes:
                        if outcome is not failed:
                            state.step(outcome.step.id).update(
                                status="failed",
                                error="parallel batch was not integrated",
                            )
                    return self._block_step(
                        state,
                        state.step(failed.step.id),
                        failed.step_result,
                    )
                return self._fail_parallel_batch(
                    state,
                    outcomes,
                    failed.error or "parallel worker did not complete",
                    failed.step.id,
                )
            try:
                for outcome in outcomes:
                    apply_workspace_changes(self.root, outcome.changes)
            except (OSError, ValidationError) as error:
                return self._fail_parallel_batch(
                    state, outcomes, f"parallel integration failed: {error}"
                )
            for index, outcome in enumerate(outcomes):
                state.current_step = outcome.step.id
                state_step = state.step(outcome.step.id)
                state_step["status"] = "verifying"
                state_step["attempts"] = outcome.attempt
                self._write_state(state)
                self.store.append_event(
                    run_id,
                    {
                        "type": "step.verifying",
                        "step": outcome.step.id,
                        "attempt": outcome.attempt,
                        "isolated": False,
                    },
                )
                verified, safety_error = self._verify_with_guard(
                    run_id, outcome.step.acceptance_commands
                )
                self.store.write_evidence_atomic(
                    run_id,
                    f"{outcome.step.id}-attempt-{outcome.attempt:02d}-verification.json",
                    verified.to_dict(),
                )
                error = safety_error or (
                    None if verified.ok else verified.failure_summary()
                )
                if error is not None:
                    for remaining in outcomes[index + 1 :]:
                        state.step(remaining.step.id).update(
                            status="failed",
                            error="parallel batch integration verification failed",
                        )
                    return self._fail_run(state, state_step, error)
                if outcome.step_result is None:
                    return self._fail_run(
                        state, state_step, "parallel worker result disappeared"
                    )
                self._complete_step(
                    state,
                    state_step,
                    outcome.step_result,
                    outcome.attempt,
                    previous_summaries,
                )
                completed_ids.add(outcome.step.id)
                pending.remove(outcome.step)
        return self._finish_run(run_id, state, plan)

    def _run_isolated_step(
        self,
        run_id: str,
        step: PlanStep,
        previous_summaries: list[str],
    ) -> IsolatedStepOutcome:
        with tempfile.TemporaryDirectory(prefix=f"harness-{step.id}-") as temporary:
            workspace = Path(temporary) / "repo"
            try:
                copy_repository(self.root, workspace)
                guard = GitGuard(workspace)
                original = guard.snapshot()
            except (OSError, GitError, ValidationError) as error:
                return IsolatedStepOutcome(
                    step,
                    1,
                    failed_agent_result(str(error)),
                    None,
                    None,
                    (),
                    f"cannot prepare isolated workspace: {error}",
                )
            last_error: str | None = None
            latest_result = failed_agent_result("agent did not run")
            for attempt in range(1, self.config.max_retries + 1):
                before = guard.snapshot()
                network_access = self._executor_network_access(step)
                request = AgentRunRequest(
                    prompt=execution_prompt(
                        step,
                        previous_summaries,
                        last_error,
                        network_access=network_access,
                    ),
                    sandbox="workspace-write",
                    output_schema=self.paths.step_result_schema,
                    cwd=workspace,
                    event_log=self.store.evidence_path(
                        run_id, f"{step.id}-attempt-{attempt:02d}.jsonl"
                    ),
                    timeout_seconds=self.config.timeout_seconds,
                    max_event_log_bytes=self.config.max_event_log_bytes,
                    max_final_payload_bytes=self.config.max_final_payload_bytes,
                    max_tool_output_bytes=self.config.max_tool_output_bytes,
                    model=self.config.executor.model,
                    reasoning_effort=self.config.executor.reasoning_effort,
                    network_access=network_access,
                )
                latest_result = self.runner.run(request)
                after = guard.snapshot()
                safety_error = isolated_safety_error(
                    guard, before, after, step.allowed_paths
                )
                if safety_error is not None:
                    return IsolatedStepOutcome(
                        step,
                        attempt,
                        latest_result,
                        None,
                        None,
                        (),
                        safety_error,
                    )
                parsed, parse_error = parse_step_result(latest_result)
                if parse_error is not None:
                    last_error = parse_error
                elif parsed is not None and parsed.outcome == "blocked":
                    blocker_error = contradictory_network_blocker(
                        parsed, network_access=network_access
                    )
                    if blocker_error is None:
                        return IsolatedStepOutcome(
                            step, attempt, latest_result, parsed, None, (), None
                        )
                    last_error = blocker_error
                elif parsed is not None and parsed.outcome == "failed":
                    last_error = parsed.error_message or "Codex reported failure"
                elif parsed is not None:
                    verification_before = guard.snapshot()
                    verified = self.verifier.verify(
                        step.acceptance_commands, workspace
                    )
                    verification_after = guard.snapshot()
                    verification_error = isolated_safety_error(
                        guard,
                        verification_before,
                        verification_after,
                        (),
                    )
                    if verification_error is not None:
                        return IsolatedStepOutcome(
                            step,
                            attempt,
                            latest_result,
                            parsed,
                            verified,
                            (),
                            "isolated verification changed workspace",
                        )
                    if verified.ok:
                        changed = guard.changed_paths(original, verification_after)
                        outside = paths_outside_allowed(changed, step.allowed_paths)
                        if outside:
                            return IsolatedStepOutcome(
                                step,
                                attempt,
                                latest_result,
                                parsed,
                                verified,
                                (),
                                "files changed outside allowed_paths: "
                                + ", ".join(sorted(outside)),
                            )
                        try:
                            changes = collect_workspace_changes(workspace, changed)
                        except (OSError, ValidationError) as error:
                            return IsolatedStepOutcome(
                                step,
                                attempt,
                                latest_result,
                                parsed,
                                verified,
                                (),
                                str(error),
                            )
                        return IsolatedStepOutcome(
                            step,
                            attempt,
                            latest_result,
                            parsed,
                            verified,
                            changes,
                            None,
                        )
                    last_error = verified.failure_summary()
                if attempt == self.config.max_retries:
                    break
            return IsolatedStepOutcome(
                step,
                self.config.max_retries,
                latest_result,
                None,
                None,
                (),
                last_error or "parallel worker failed",
            )

    def _write_isolated_evidence(
        self, run_id: str, outcome: IsolatedStepOutcome
    ) -> None:
        self.store.write_evidence_atomic(
            run_id,
            f"{outcome.step.id}-attempt-{outcome.attempt:02d}-agent.json",
            {
                "isolated": True,
                "exit_code": outcome.agent_result.exit_code,
                "terminal_event": outcome.agent_result.terminal_event,
                "timed_out": outcome.agent_result.timed_out,
                "output_truncated": outcome.agent_result.output_truncated,
                "event_log_truncated": outcome.agent_result.event_log_truncated,
                "final_payload_truncated": (
                    outcome.agent_result.final_payload_truncated
                ),
                "model": self.config.executor.model,
                "reasoning_effort": self.config.executor.reasoning_effort,
                "network_access": self._executor_network_access(outcome.step),
                "usage": outcome.agent_result.usage,
                "stderr": outcome.agent_result.stderr,
                "final_payload": outcome.agent_result.final_payload,
                "changed_paths": [change.path for change in outcome.changes],
                "error": outcome.error,
            },
        )
        if outcome.verification is not None:
            self.store.write_evidence_atomic(
                run_id,
                f"{outcome.step.id}-attempt-{outcome.attempt:02d}-isolated-verification.json",
                outcome.verification.to_dict(),
            )

    def _fail_parallel_batch(
        self,
        state: RunState,
        outcomes: list[IsolatedStepOutcome],
        message: str,
        failed_step_id: str | None = None,
    ) -> RunState:
        target = failed_step_id or outcomes[0].step.id
        for outcome in outcomes:
            state_step = state.step(outcome.step.id)
            state_step["attempts"] = outcome.attempt
            if outcome.step.id != target:
                state_step.update(
                    status="failed", error="parallel batch was not integrated"
                )
        return self._fail_run(state, state.step(target), message)

    def review(self, run_id: str) -> ReviewResult:
        plan = self._read_plan(run_id)
        state_before = self.status(run_id)
        git_before = self.git_guard.snapshot()
        index = next_review_index(self.store, run_id)
        if index is None:
            raise HarnessError("review artifact limit reached")
        event_log = self.store.evidence_path(
            run_id, f"review-{index:02d}-events.jsonl"
        )
        request = AgentRunRequest(
            prompt=review_prompt(
                run_id,
                plan,
                state_before,
                self.config.parallel_readers.max_workers
                if self.config.parallel_readers.enabled
                else 0,
            ),
            sandbox="read-only",
            output_schema=self.paths.review_result_schema,
            cwd=self.root,
            event_log=event_log,
            timeout_seconds=self.config.timeout_seconds,
            max_event_log_bytes=self.config.max_event_log_bytes,
            max_final_payload_bytes=self.config.max_final_payload_bytes,
            max_tool_output_bytes=self.config.max_tool_output_bytes,
            model=self.config.reviewer.model,
            reasoning_effort=self.config.reviewer.reasoning_effort,
            subagents_enabled=self.config.parallel_readers.enabled,
            max_subagents=self.config.parallel_readers.max_workers,
            subagent_model=self.config.parallel_readers.profile.model,
            subagent_reasoning_effort=(
                self.config.parallel_readers.profile.reasoning_effort
            ),
        )
        self.store.append_event(
            run_id,
            {
                "type": "review.running",
                "review": index,
                "model": request.model,
                "reasoning_effort": request.reasoning_effort,
            },
        )
        runs_before = self.store.capture_runs_files()
        result = self.runner.run(request)
        git_after = self.git_guard.snapshot()
        runs_after = self.store.capture_runs_files()
        expected_runs = dict(runs_before)
        expected_event = f"{run_id}/evidence/{event_log.name}"
        if expected_event in runs_after:
            expected_runs[expected_event] = runs_after[expected_event]
        unexpected_runs = changed_keys(expected_runs, runs_after)
        if unexpected_runs:
            self.store.restore_runs_files(expected_runs)
        if git_before.fingerprint != git_after.fingerprint:
            raise HarnessError("review changed the Git working tree")
        if unexpected_runs:
            raise HarnessError("review changed controller-owned run metadata")
        if not result.process_succeeded:
            failure = agent_failure(result)
            self.store.write_evidence_atomic(
                run_id,
                f"review-{index:02d}-failure.json",
                {
                    "model": request.model,
                    "reasoning_effort": request.reasoning_effort,
                    "usage": result.usage,
                    "event_log_truncated": result.event_log_truncated,
                    "final_payload_truncated": result.final_payload_truncated,
                    "error": failure,
                },
            )
            self.store.append_event(
                run_id,
                {
                    "type": "review.failed",
                    "review": index,
                    "error": failure,
                },
            )
            raise HarnessError("Codex did not produce a completed review")
        try:
            review = ReviewResult.from_dict(result.final_payload)
        except ValidationError as error:
            raise HarnessError(f"invalid review result: {error}") from error
        if review.observed_status != state_before.status:
            raise HarnessError("review reported a different run status")
        if self.status(run_id).to_dict() != state_before.to_dict():
            raise HarnessError("review changed controller-owned run state")
        self.store.write_evidence_atomic(
            run_id,
            f"review-{index:02d}.json",
            {
                "model": request.model,
                "reasoning_effort": request.reasoning_effort,
                "network_access": request.network_access,
                "usage": result.usage,
                "event_log_truncated": result.event_log_truncated,
                "result": review.to_dict(),
            },
        )
        self.store.append_event(
            run_id,
            {
                "type": "review.completed",
                "review": index,
                "finding_count": len(review.findings),
                "event_log_truncated": result.event_log_truncated,
            },
        )
        return review

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
                {
                    "type": "step.running",
                    "step": plan_step.id,
                    "attempt": attempt,
                    "model": self.config.executor.model,
                    "reasoning_effort": self.config.executor.reasoning_effort,
                    "network_access": self._executor_network_access(plan_step),
                },
            )

            agent_result, safety_error = self._run_agent_attempt(
                run_id, plan_step, attempt, previous_summaries, last_error
            )
            if safety_error is not None:
                return self._fail_run(state, state_step, safety_error)

            parsed, parse_error = parse_step_result(agent_result)
            if parse_error is not None:
                last_error = parse_error
            elif parsed is not None and parsed.outcome == "blocked":
                blocker_error = contradictory_network_blocker(
                    parsed,
                    network_access=self._executor_network_access(plan_step),
                )
                if blocker_error is None:
                    return self._block_step(state, state_step, parsed)
                last_error = blocker_error
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
        network_access = self._executor_network_access(plan_step)
        request = AgentRunRequest(
            prompt=execution_prompt(
                plan_step,
                previous_summaries,
                last_error,
                network_access=network_access,
            ),
            sandbox="workspace-write",
            output_schema=self.paths.step_result_schema,
            cwd=self.root,
            event_log=self.store.evidence_path(
                run_id, f"{plan_step.id}-attempt-{attempt:02d}.jsonl"
            ),
            timeout_seconds=self.config.timeout_seconds,
            max_event_log_bytes=self.config.max_event_log_bytes,
            max_final_payload_bytes=self.config.max_final_payload_bytes,
            max_tool_output_bytes=self.config.max_tool_output_bytes,
            model=self.config.executor.model,
            reasoning_effort=self.config.executor.reasoning_effort,
            network_access=network_access,
        )
        result = self.runner.run(request)
        after = self.git_guard.snapshot()
        changed_paths = self.git_guard.changed_paths(before, after)

        runs_after = self.store.capture_runs_files()
        expected_runs = dict(run_files)
        expected_event = f"{run_id}/evidence/{request.event_log.name}"
        if expected_event in runs_after:
            expected_runs[expected_event] = runs_after[expected_event]
        changed_run_files = changed_keys(expected_runs, runs_after)
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
                "event_log_truncated": result.event_log_truncated,
                "final_payload_truncated": result.final_payload_truncated,
                "reader_failed": result.reader_failed,
                "model": request.model,
                "reasoning_effort": request.reasoning_effort,
                "network_access": request.network_access,
                "usage": result.usage,
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

    def _executor_network_access(self, plan_step: PlanStep) -> bool:
        return (
            self.config.network.executor_enabled
            and plan_step.network_access is True
        )

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
