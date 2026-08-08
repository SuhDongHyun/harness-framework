import json
import tempfile
import unittest
from pathlib import Path

from harness.domain import Plan, RunState, ValidationError
from harness.orchestration.planning import step_document
from harness.storage import RunStore


def valid_plan() -> dict:
    return {
        "version": 1,
        "goal": "Build a small feature",
        "status": "draft",
        "steps": [
            {
                "id": "step-00",
                "name": "core",
                "depends_on": [],
                "objective": "Implement the core behavior",
                "read_files": ["AGENTS.md"],
                "allowed_paths": ["src/**", "tests/**"],
                "acceptance_commands": [["python3", "-m", "unittest"]],
                "forbidden_changes": ["Do not add dependencies"],
            }
        ],
        "final_acceptance_commands": [["python3", "-m", "unittest"]],
    }


class PlanModelTests(unittest.TestCase):
    def test_round_trip(self):
        plan = Plan.from_dict(valid_plan())
        self.assertEqual(valid_plan(), plan.to_dict())

    def test_step_network_opt_in_round_trip(self):
        payload = valid_plan()
        payload["steps"][0]["network_access"] = True
        plan = Plan.from_dict(payload)
        self.assertTrue(plan.steps[0].network_access)
        self.assertEqual(payload, plan.to_dict())

    def test_step_network_opt_in_must_be_boolean(self):
        payload = valid_plan()
        payload["steps"][0]["network_access"] = "yes"
        with self.assertRaisesRegex(ValidationError, "network_access must be boolean"):
            Plan.from_dict(payload)

    def test_rejects_path_escape(self):
        payload = valid_plan()
        payload["steps"][0]["read_files"] = ["../secret"]
        with self.assertRaises(ValidationError):
            Plan.from_dict(payload)

    def test_rejects_absolute_path(self):
        payload = valid_plan()
        payload["steps"][0]["allowed_paths"] = ["/tmp/**"]
        with self.assertRaises(ValidationError):
            Plan.from_dict(payload)

    def test_rejects_controller_or_git_metadata_as_allowed_path(self):
        for reserved in (".harness/**", ".git/**"):
            with self.subTest(reserved=reserved):
                payload = valid_plan()
                payload["steps"][0]["allowed_paths"] = [reserved]
                with self.assertRaises(ValidationError):
                    Plan.from_dict(payload)

    def test_rejects_shell_string_command(self):
        payload = valid_plan()
        payload["steps"][0]["acceptance_commands"] = ["python3 -m unittest"]
        with self.assertRaises(ValidationError):
            Plan.from_dict(payload)

    def test_rejects_destructive_verification_commands(self):
        commands = [
            ["git", "reset", "--hard"],
            ["bash", "-c", "echo unsafe"],
            ["python3", "-c", "print('unsafe')"],
            ["rm", "-rf", "build"],
        ]
        for command in commands:
            with self.subTest(command=command):
                payload = valid_plan()
                payload["steps"][0]["acceptance_commands"] = [command]
                with self.assertRaises(ValidationError):
                    Plan.from_dict(payload)

    def test_rejects_step_without_acceptance_command(self):
        payload = valid_plan()
        payload["steps"][0]["acceptance_commands"] = []
        with self.assertRaises(ValidationError):
            Plan.from_dict(payload)

    def test_rejects_duplicate_step_ids(self):
        payload = valid_plan()
        payload["steps"].append(dict(payload["steps"][0]))
        with self.assertRaises(ValidationError):
            Plan.from_dict(payload)

    def test_dependencies_must_reference_earlier_steps(self):
        payload = valid_plan()
        payload["steps"][0]["depends_on"] = ["step-01"]
        with self.assertRaisesRegex(ValidationError, "earlier steps"):
            Plan.from_dict(payload)

    def test_accepts_dependency_on_earlier_step(self):
        payload = valid_plan()
        second = dict(payload["steps"][0])
        second["id"] = "step-01"
        second["name"] = "follow-up"
        second["depends_on"] = ["step-00"]
        payload["steps"].append(second)
        plan = Plan.from_dict(payload)
        self.assertEqual(("step-00",), plan.steps[1].depends_on)

    def test_plan_hash_is_stable(self):
        first = Plan.from_dict(valid_plan())
        second = Plan.from_dict(json.loads(json.dumps(valid_plan())))
        self.assertEqual(first.sha256(), second.sha256())

    def test_step_document_includes_safety_and_verification_contract(self):
        step = Plan.from_dict(valid_plan()).steps[0]
        document = step_document(step)
        self.assertIn("## Objective", document)
        self.assertIn("## Allowed paths", document)
        self.assertIn("## Acceptance commands", document)
        self.assertIn("## Executor network access", document)
        self.assertIn("Disabled", document)
        self.assertIn("## Forbidden changes", document)
        self.assertIn("Do not add dependencies", document)

    def test_rejects_more_than_one_hundred_steps(self):
        payload = valid_plan()
        payload["steps"] = []
        for index in range(101):
            step = dict(valid_plan()["steps"][0])
            step["id"] = f"step-{index:02d}"
            step["name"] = f"step-{index:02d}"
            payload["steps"].append(step)
        with self.assertRaises(ValidationError):
            Plan.from_dict(payload)


class RunStateTests(unittest.TestCase):
    def test_state_starts_as_draft(self):
        state = RunState.from_plan("run-1", Plan.from_dict(valid_plan()))
        self.assertEqual("draft", state.status)
        self.assertEqual("pending", state.steps[0]["status"])

    def test_rejects_unsafe_run_id(self):
        with self.assertRaises(ValidationError):
            RunState.from_plan("../run", Plan.from_dict(valid_plan()))

    def test_rejects_unknown_terminal_result(self):
        state = RunState.from_plan("run-1", Plan.from_dict(valid_plan())).to_dict()
        state["terminal_result"] = "maybe"
        with self.assertRaises(ValidationError):
            RunState.from_dict(state)


class RunStoreTests(unittest.TestCase):
    def test_atomic_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.create_run("run-1", "goal")
            store.write_json_atomic("run-1", "state.json", {"version": 1})
            self.assertEqual(
                {"version": 1},
                store.read_json("run-1", "state.json"),
            )

    def test_create_run_owns_expected_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            run_dir = store.create_run("run-1", "goal")
            self.assertTrue((run_dir / "steps").is_dir())
            self.assertTrue((run_dir / "evidence").is_dir())
            self.assertEqual("goal\n", (run_dir / "request.md").read_text())

    def test_append_event_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.create_run("run-1", "goal")
            store.append_event("run-1", {"type": "created", "value": "한글"})
            event = json.loads(
                (store.run_dir("run-1") / "events.jsonl").read_text().strip()
            )
            self.assertEqual("created", event["type"])
            self.assertEqual("한글", event["value"])

    def test_rejects_nested_artifact_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.create_run("run-1", "goal")
            with self.assertRaises(ValidationError):
                store.write_json_atomic("run-1", "../state.json", {})

    def test_restore_runs_validates_snapshot_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.create_run("run-1", "goal")
            state_path = store.run_dir("run-1") / "state.json"
            state_path.write_text('{"status": "safe"}\n')
            snapshot = store.capture_runs_files()
            invalid = dict(snapshot)
            invalid["run-1/evidence/link"] = b"HARNESS-SYMLINK\0target"
            with self.assertRaises(ValidationError):
                store.restore_runs_files(invalid)
            self.assertEqual('{"status": "safe"}\n', state_path.read_text())


if __name__ == "__main__":
    unittest.main()
