import json
import tempfile
import unittest
from pathlib import Path

from harness.agents import AgentRunResult
from harness.config import AgentProfile, HarnessConfig
from harness.orchestration import HarnessController, HarnessError
from harness.orchestration.planning import default_run_id
from harness.safety import GitSnapshot
from harness.safety.verifier import (
    CommandEvidence,
    VerificationResult,
)
from harness.storage import RunStore
from tests.test_models_store import valid_plan


def completed_result(
    summary: str = "done", *, event_log_truncated: bool = False
) -> AgentRunResult:
    return AgentRunResult(
        exit_code=0,
        final_payload={
            "outcome": "completed",
            "summary": summary,
            "changed_files": ["src/core.py"],
            "error_message": None,
            "blocked_reason": None,
            "required_action": None,
        },
        stderr="",
        timed_out=False,
        terminal_event="turn.completed",
        event_log_truncated=event_log_truncated,
    )


def failed_result(message: str = "implementation failed") -> AgentRunResult:
    return AgentRunResult(
        exit_code=0,
        final_payload={
            "outcome": "failed",
            "summary": "",
            "changed_files": [],
            "error_message": message,
            "blocked_reason": None,
            "required_action": None,
        },
        stderr="",
        timed_out=False,
        terminal_event="turn.completed",
    )


def blocked_result() -> AgentRunResult:
    return AgentRunResult(
        exit_code=0,
        final_payload={
            "outcome": "blocked",
            "summary": "",
            "changed_files": [],
            "error_message": None,
            "blocked_reason": "API credential missing",
            "required_action": "Provide the credential",
        },
        stderr="",
        timed_out=False,
        terminal_event="turn.completed",
    )


def plan_result() -> AgentRunResult:
    return AgentRunResult(
        exit_code=0,
        final_payload=valid_plan(),
        stderr="",
        timed_out=False,
        terminal_event="turn.completed",
    )


def review_result(status: str = "draft") -> AgentRunResult:
    return AgentRunResult(
        exit_code=0,
        final_payload={
            "version": 1,
            "observed_status": status,
            "summary": "Evidence reviewed",
            "findings": [],
        },
        stderr="",
        timed_out=False,
        terminal_event="turn.completed",
    )


def failed_agent_run() -> AgentRunResult:
    return AgentRunResult(
        exit_code=1,
        final_payload=None,
        stderr="review failed",
        timed_out=False,
        terminal_event="turn.failed",
    )


def verification(ok: bool, message: str = "") -> VerificationResult:
    if ok:
        return VerificationResult(True, ())
    return VerificationResult(
        False,
        (
            CommandEvidence(
                argv=("python3", "-m", "unittest"),
                exit_code=1,
                stdout="",
                stderr=message or "tests failed",
                duration_seconds=0.1,
                timed_out=False,
            ),
        ),
    )


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []
        self.callback = None

    def run(self, request):
        self.requests.append(request)
        if self.callback is not None:
            self.callback(request)
        if not self.results:
            raise AssertionError("unexpected agent invocation")
        return self.results.pop(0)


class FakeVerifier:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.callback = None

    def verify(self, commands, cwd):
        self.calls.append((commands, cwd))
        if self.callback is not None:
            self.callback(commands, cwd)
        if not self.results:
            raise AssertionError("unexpected verifier invocation")
        return self.results.pop(0)


class FakeGitGuard:
    def __init__(self):
        self.fingerprint = "git-base"
        self.head = "abc"
        self.branch = "main"
        self.index_fingerprint = "index-base"
        self.changed_queue = []

    def snapshot(self):
        return GitSnapshot(
            branch=self.branch,
            head=self.head,
            porcelain="",
            dirty_paths=(),
            dirty_hashes={},
            fingerprint=self.fingerprint,
            index_fingerprint=self.index_fingerprint,
        )

    def changed_paths(self, before, after):
        if self.changed_queue:
            return set(self.changed_queue.pop(0))
        return set()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "schemas").mkdir()
        (self.root / "schemas" / "plan.schema.json").write_text("{}")
        (self.root / "schemas" / "step-result.schema.json").write_text("{}")
        (self.root / "schemas" / "review-result.schema.json").write_text("{}")
        self.store = RunStore(self.root / ".harness" / "runs")
        self.git = FakeGitGuard()
        self.config = HarnessConfig(
            max_retries=2,
            timeout_seconds=30,
            verification_timeout_seconds=30,
            max_event_log_bytes=10_000,
            max_final_payload_bytes=10_000,
            max_tool_output_bytes=2_000,
            max_verification_output_bytes=10_000,
            codex_command="codex",
            planner=AgentProfile("planner-model", "high"),
            executor=AgentProfile("executor-model", "xhigh"),
            reviewer=AgentProfile("reviewer-model", "high"),
        )

    def controller(self, runner_results, verifier_results=()):
        runner = FakeRunner(runner_results)
        verifier = FakeVerifier(verifier_results)
        controller = HarnessController(
            root=self.root,
            store=self.store,
            runner=runner,
            verifier=verifier,
            git_guard=self.git,
            config=self.config,
            run_id_factory=lambda goal: "run-1",
        )
        return controller, runner, verifier

    def test_plan_uses_read_only_and_saves_draft(self):
        controller, runner, _ = self.controller([plan_result()])
        run_id = controller.plan("Build a small feature")
        self.assertEqual("run-1", run_id)
        self.assertEqual("read-only", runner.requests[0].sandbox)
        self.assertEqual("planner-model", runner.requests[0].model)
        self.assertEqual("high", runner.requests[0].reasoning_effort)
        self.assertTrue(runner.requests[0].subagents_enabled)
        self.assertEqual(3, runner.requests[0].max_subagents)
        self.assertIn(
            'must exactly equal this JSON string, without paraphrasing: "Build a small feature"',
            runner.requests[0].prompt,
        )
        self.assertEqual("draft", controller.status(run_id).status)
        self.assertTrue((self.store.run_dir(run_id) / "steps" / "00-core.md").is_file())

    def test_approve_records_hash_and_git_fingerprint(self):
        controller, _, _ = self.controller([plan_result()])
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)
        state = controller.status(run_id)
        self.assertEqual("approved", state.status)
        self.assertIsNotNone(state.plan_sha256)
        self.assertEqual("git-base", state.approved_git_fingerprint)

    def test_review_uses_reviewer_profile_without_changing_state(self):
        controller, runner, _ = self.controller([plan_result(), review_result()])
        run_id = controller.plan("Build a small feature")
        before = controller.status(run_id).to_dict()
        result = controller.review(run_id)
        self.assertEqual("Evidence reviewed", result.summary)
        self.assertEqual("read-only", runner.requests[1].sandbox)
        self.assertEqual("reviewer-model", runner.requests[1].model)
        self.assertEqual("high", runner.requests[1].reasoning_effort)
        self.assertTrue(runner.requests[1].subagents_enabled)
        self.assertIn("never print a complete Git diff", runner.requests[1].prompt)
        self.assertIn("Read AGENTS.md, HARNESS_DESIGN.md", runner.requests[1].prompt)
        self.assertEqual(before, controller.status(run_id).to_dict())
        self.assertTrue(
            self.store.evidence_path(run_id, "review-01.json").is_file()
        )

    def test_failed_review_is_preserved_before_next_review(self):
        controller, _, _ = self.controller(
            [plan_result(), failed_agent_run(), review_result()]
        )
        run_id = controller.plan("Build a small feature")
        with self.assertRaisesRegex(HarnessError, "completed review"):
            controller.review(run_id)
        self.assertTrue(
            self.store.evidence_path(
                run_id, "review-01-failure.json"
            ).is_file()
        )
        controller.review(run_id)
        self.assertTrue(
            self.store.evidence_path(run_id, "review-02.json").is_file()
        )

    def test_approve_rebuilds_state_after_user_edits_draft_plan(self):
        controller, _, _ = self.controller([plan_result()])
        run_id = controller.plan("Build a small feature")
        edited = valid_plan()
        second = dict(edited["steps"][0])
        second["id"] = "step-01"
        second["name"] = "extra"
        edited["steps"].append(second)
        self.store.write_json_atomic(run_id, "plan.json", edited)
        controller.approve(run_id)
        state = controller.status(run_id)
        self.assertEqual(["step-00", "step-01"], [step["id"] for step in state.steps])
        self.assertTrue(
            (self.store.run_dir(run_id) / "steps" / "01-extra.md").is_file()
        )

    def test_unapproved_run_is_rejected(self):
        controller, _, _ = self.controller([plan_result()])
        run_id = controller.plan("Build a small feature")
        with self.assertRaises(HarnessError):
            controller.run(run_id)

    def test_changed_git_baseline_blocks_run(self):
        controller, _, _ = self.controller([plan_result()])
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)
        self.git.fingerprint = "git-changed"
        state = controller.run(run_id)
        self.assertEqual("blocked", state.status)
        self.assertIn("Git working tree", state.blocked_reason)

    def test_successful_step_and_final_verification_complete_run(self):
        controller, runner, verifier = self.controller(
            [plan_result(), completed_result()],
            [verification(True), verification(True)],
        )
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)
        state = controller.run(run_id)
        self.assertEqual("completed", state.status)
        self.assertEqual("completed", state.steps[0]["status"])
        self.assertEqual("executor-model", runner.requests[1].model)
        self.assertEqual("xhigh", runner.requests[1].reasoning_effort)
        self.assertEqual(2, len(verifier.calls))

    def test_verifier_failure_retries_then_succeeds(self):
        controller, runner, _ = self.controller(
            [plan_result(), completed_result("first"), completed_result("fixed")],
            [
                verification(False, "test failed"),
                verification(True),
                verification(True),
            ],
        )
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)
        state = controller.run(run_id)
        self.assertEqual("completed", state.status)
        self.assertEqual(2, state.steps[0]["attempts"])
        self.assertIn("test failed", runner.requests[2].prompt)

    def test_event_log_truncation_does_not_skip_controller_verification(self):
        controller, _, verifier = self.controller(
            [plan_result(), completed_result(event_log_truncated=True)],
            [verification(True), verification(True)],
        )
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)
        state = controller.run(run_id)
        self.assertEqual("completed", state.status)
        self.assertEqual(2, len(verifier.calls))
        evidence = json.loads(
            self.store.evidence_path(
                run_id, "step-00-attempt-01-agent.json"
            ).read_text()
        )
        self.assertTrue(evidence["event_log_truncated"])
        self.assertFalse(evidence["final_payload_truncated"])

    def test_maximum_retries_fail_run(self):
        controller, _, verifier = self.controller(
            [plan_result(), failed_result("first"), failed_result("second")]
        )
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)
        state = controller.run(run_id)
        self.assertEqual("failed", state.status)
        self.assertEqual("failed", state.steps[0]["status"])
        self.assertEqual(0, len(verifier.calls))

    def test_blocked_result_stops_without_verification(self):
        controller, _, verifier = self.controller([plan_result(), blocked_result()])
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)
        state = controller.run(run_id)
        self.assertEqual("blocked", state.status)
        self.assertEqual("API credential missing", state.blocked_reason)
        self.assertEqual(0, len(verifier.calls))

    def test_out_of_scope_change_fails_run(self):
        controller, _, verifier = self.controller([plan_result(), completed_result()])
        self.git.changed_queue = [{"README.md"}]
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)
        state = controller.run(run_id)
        self.assertEqual("failed", state.status)
        self.assertIn("README.md", state.steps[0]["error"])
        self.assertEqual(0, len(verifier.calls))

    def test_controller_metadata_tampering_fails_run(self):
        controller, runner, verifier = self.controller(
            [plan_result(), completed_result()]
        )
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)

        def tamper(request):
            if request.sandbox == "workspace-write":
                (self.store.run_dir(run_id) / "state.json").write_text("{}")

        runner.callback = tamper
        state = controller.run(run_id)
        self.assertEqual("failed", state.status)
        self.assertIn("controller-owned", state.steps[0]["error"])
        self.assertEqual(0, len(verifier.calls))

    def test_git_head_change_fails_run(self):
        controller, runner, verifier = self.controller(
            [plan_result(), completed_result()]
        )
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)

        def commit_during_run(request):
            if request.sandbox == "workspace-write":
                self.git.head = "new-head"

        runner.callback = commit_during_run
        state = controller.run(run_id)
        self.assertEqual("failed", state.status)
        self.assertIn("Git branch or HEAD", state.steps[0]["error"])
        self.assertEqual(0, len(verifier.calls))

    def test_previous_evidence_tampering_fails_run(self):
        controller, runner, _ = self.controller(
            [plan_result(), completed_result("first"), completed_result("second")],
            [
                verification(False, "retry"),
            ],
        )
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)
        execution_count = 0

        def tamper_previous_evidence(request):
            nonlocal execution_count
            if request.sandbox != "workspace-write":
                return
            execution_count += 1
            if execution_count == 2:
                self.store.evidence_path(
                    run_id, "step-00-attempt-01-agent.json"
                ).write_text("{}")

        runner.callback = tamper_previous_evidence
        state = controller.run(run_id)
        self.assertEqual("failed", state.status)
        self.assertIn("verification evidence", state.steps[0]["error"])

    def test_other_run_evidence_tampering_fails_and_restores(self):
        old_run = "run-old"
        self.store.create_run(old_run, "old goal")
        old_evidence = self.store.evidence_path(old_run, "result.json")
        old_evidence.write_text('{"ok": true}\n')
        controller, runner, _ = self.controller([plan_result(), completed_result()])
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)

        def tamper_other_run(request):
            if request.sandbox == "workspace-write":
                old_evidence.write_text("{}\n")

        runner.callback = tamper_other_run
        state = controller.run(run_id)
        self.assertEqual("failed", state.status)
        self.assertIn("verification evidence", state.steps[0]["error"])
        self.assertEqual('{"ok": true}\n', old_evidence.read_text())

    def test_final_verification_failure_fails_run(self):
        controller, _, _ = self.controller(
            [plan_result(), completed_result()],
            [verification(True), verification(False, "final failed")],
        )
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)
        state = controller.run(run_id)
        self.assertEqual("failed", state.status)
        self.assertIn("final failed", state.steps[0]["error"])

    def test_step_verifier_mutation_fails_run(self):
        controller, _, verifier = self.controller(
            [plan_result(), completed_result()],
            [verification(True)],
        )
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)

        def mutate_during_verification(commands, cwd):
            self.git.changed_queue.append({"README.md"})

        verifier.callback = mutate_during_verification
        state = controller.run(run_id)
        self.assertEqual("failed", state.status)
        self.assertIn("verification changed repository paths", state.steps[0]["error"])

    def test_verifier_git_index_mutation_fails_run(self):
        controller, _, verifier = self.controller(
            [plan_result(), completed_result()],
            [verification(True)],
        )
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)

        def mutate_index(commands, cwd):
            self.git.index_fingerprint = "changed-index"

        verifier.callback = mutate_index
        state = controller.run(run_id)
        self.assertEqual("failed", state.status)
        self.assertIn("Git index", state.steps[0]["error"])

    def test_successful_run_records_state_transition_events(self):
        controller, _, _ = self.controller(
            [plan_result(), completed_result()],
            [verification(True), verification(True)],
        )
        run_id = controller.plan("Build a small feature")
        controller.approve(run_id)
        controller.run(run_id)
        event_types = [
            __import__("json").loads(line)["type"]
            for line in (self.store.run_dir(run_id) / "events.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertIn("run.running", event_types)
        self.assertIn("step.running", event_types)
        self.assertIn("step.verifying", event_types)
        self.assertIn("run.verifying", event_types)

    def test_default_run_ids_do_not_collide(self):
        self.assertNotEqual(default_run_id("same goal"), default_run_id("same goal"))


if __name__ == "__main__":
    unittest.main()
