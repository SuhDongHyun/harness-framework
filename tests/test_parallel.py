import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from harness.agents import AgentRunResult
from harness.config import HarnessConfig, NetworkConfig, ParallelWriterConfig
from harness.orchestration import HarnessController
from harness.safety import GitGuard, VerificationResult
from harness.storage import RunStore


def parallel_plan() -> dict[str, object]:
    def step(index: int, path: str) -> dict[str, object]:
        payload = {
            "id": f"step-{index:02d}",
            "name": f"worker-{index}",
            "depends_on": [],
            "objective": f"Write {path}",
            "read_files": ["AGENTS.md"],
            "allowed_paths": [path],
            "acceptance_commands": [["python3", "-m", "unittest"]],
            "forbidden_changes": ["Do not touch other files"],
        }
        if index == 0:
            payload["network_access"] = True
        return payload

    return {
        "version": 1,
        "goal": "Parallel work",
        "status": "draft",
        "steps": [step(0, "src/a.txt"), step(1, "src/b.txt")],
        "final_acceptance_commands": [["python3", "-m", "unittest"]],
    }


class ParallelRunner:
    def __init__(self):
        self.barrier = threading.Barrier(2)
        self.requests = []
        self.lock = threading.Lock()

    def run(self, request):
        with self.lock:
            self.requests.append(request)
        if request.sandbox == "read-only":
            return AgentRunResult(
                0, parallel_plan(), "", False, "turn.completed"
            )
        step_id = "step-00" if "step-00" in request.prompt else "step-01"
        relative = "src/a.txt" if step_id == "step-00" else "src/b.txt"
        destination = request.cwd / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(step_id + "\n")
        self.barrier.wait(timeout=5)
        return AgentRunResult(
            0,
            {
                "outcome": "completed",
                "summary": f"completed {step_id}",
                "changed_files": [relative],
                "error_message": None,
                "blocked_reason": None,
                "required_action": None,
            },
            "",
            False,
            "turn.completed",
        )


class AlwaysVerifier:
    def verify(self, commands, cwd):
        return VerificationResult(True, ())


class ParallelControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.root,
            check=True,
        )
        (self.root / "AGENTS.md").write_text("test\n")
        (self.root / ".gitignore").write_text(".harness/runs/\n")
        (self.root / "schemas").mkdir()
        for name in ("plan.schema.json", "step-result.schema.json", "review-result.schema.json"):
            (self.root / "schemas" / name).write_text("{}")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)

    def test_parallel_workers_integrate_disjoint_verified_changes(self):
        runner = ParallelRunner()
        store = RunStore(self.root / ".harness" / "runs")
        config = HarnessConfig(
            max_retries=1,
            parallel_writers=ParallelWriterConfig(enabled=True, max_workers=2),
            network=NetworkConfig(executor_enabled=True),
        )
        controller = HarnessController(
            root=self.root,
            store=store,
            runner=runner,
            verifier=AlwaysVerifier(),
            git_guard=GitGuard(self.root),
            config=config,
            run_id_factory=lambda goal: "run-1",
        )
        run_id = controller.plan("Parallel work")
        controller.approve(run_id)
        state = controller.run(run_id)
        self.assertEqual("completed", state.status)
        self.assertEqual("step-00\n", (self.root / "src" / "a.txt").read_text())
        self.assertEqual("step-01\n", (self.root / "src" / "b.txt").read_text())
        self.assertTrue(
            store.evidence_path(run_id, "step-00-attempt-01-agent.json").is_file()
        )
        evidence = json.loads(
            store.evidence_path(
                run_id, "step-00-attempt-01-agent.json"
            ).read_text()
        )
        self.assertTrue(evidence["isolated"])
        execution_requests = [
            request for request in runner.requests if request.sandbox == "workspace-write"
        ]
        by_step = {
            "step-00" if "step-00" in request.prompt else "step-01": request
            for request in execution_requests
        }
        self.assertTrue(by_step["step-00"].network_access)
        self.assertFalse(by_step["step-01"].network_access)


if __name__ == "__main__":
    unittest.main()
