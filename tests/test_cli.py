import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from harness.cli import main, run_doctor
from harness.domain import Plan, RunState
from harness.orchestration import HarnessError
from tests.test_models_store import valid_plan


class FakeController:
    def __init__(self):
        self.calls = []
        self.state = RunState.from_plan("run-1", Plan.from_dict(valid_plan()))

    def plan(self, goal):
        self.calls.append(("plan", goal))
        return "run-1"

    def approve(self, run_id):
        self.calls.append(("approve", run_id))
        self.state.status = "approved"

    def run(self, run_id):
        self.calls.append(("run", run_id))
        return self.state

    def status(self, run_id):
        self.calls.append(("status", run_id))
        return self.state

    def review(self, run_id):
        self.calls.append(("review", run_id))
        return type(
            "Review",
            (),
            {"to_dict": lambda self: {
                "version": 1,
                "observed_status": "draft",
                "summary": "reviewed",
                "findings": [],
            }},
        )()


class CliTests(unittest.TestCase):
    def invoke(self, args, controller=None):
        controller = controller or FakeController()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                args,
                root=Path("/repo"),
                controller_factory=lambda root: controller,
            )
        return code, stdout.getvalue(), stderr.getvalue(), controller

    def test_plan_routes_goal_and_prints_json(self):
        code, stdout, stderr, controller = self.invoke(
            ["plan", "Build", "the", "feature"]
        )
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual([("plan", "Build the feature")], controller.calls)
        self.assertEqual("run-1", json.loads(stdout)["run_id"])

    def test_approve_routes_run_id(self):
        code, stdout, _, controller = self.invoke(["approve", "run-1"])
        self.assertEqual(0, code)
        self.assertEqual([("approve", "run-1")], controller.calls)
        self.assertEqual("approved", json.loads(stdout)["status"])

    def test_run_exit_codes_follow_terminal_result(self):
        for status, expected in (("completed", 0), ("failed", 1), ("blocked", 2)):
            with self.subTest(status=status):
                controller = FakeController()
                controller.state.status = status
                controller.state.terminal_result = status
                code, stdout, _, _ = self.invoke(["run", "run-1"], controller)
                self.assertEqual(expected, code)
                self.assertEqual(status, json.loads(stdout)["status"])

    def test_status_is_read_only_route(self):
        code, stdout, _, controller = self.invoke(["status", "run-1"])
        self.assertEqual(0, code)
        self.assertEqual([("status", "run-1")], controller.calls)
        self.assertEqual("draft", json.loads(stdout)["status"])

    def test_review_routes_run_id(self):
        code, stdout, _, controller = self.invoke(["review", "run-1"])
        self.assertEqual(0, code)
        self.assertEqual([("review", "run-1")], controller.calls)
        self.assertEqual("reviewed", json.loads(stdout)["review"]["summary"])

    def test_harness_error_returns_two(self):
        controller = FakeController()
        controller.plan = lambda goal: (_ for _ in ()).throw(HarnessError("bad goal"))
        code, _, stderr, _ = self.invoke(["plan", "bad"], controller)
        self.assertEqual(2, code)
        self.assertIn("bad goal", stderr)

    def test_doctor_uses_diagnostic_result(self):
        report = {"ok": False, "checks": [{"name": "Codex", "ok": False}]}
        with patch("harness.cli.run_doctor", return_value=report):
            code, stdout, _, _ = self.invoke(["doctor"])
        self.assertEqual(2, code)
        self.assertEqual(report, json.loads(stdout))

    def test_doctor_checks_configured_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.toml").write_text(
                '[harness]\ncodex_command = "custom-codex"\n'
            )
            (root / "schemas").mkdir()
            for name in (
                "plan.schema.json",
                "step-result.schema.json",
                "review-result.schema.json",
                "state.schema.json",
            ):
                (root / "schemas" / name).write_text("{}")
            git_result = type(
                "Result",
                (),
                {"returncode": 0, "stdout": str(root), "stderr": ""},
            )()
            with (
                patch("harness.cli.subprocess.run", return_value=git_result),
                patch(
                    "harness.cli.shutil.which",
                    return_value="/bin/custom-codex",
                ) as which,
            ):
                report = run_doctor(root)
            which.assert_called_once_with("custom-codex")
            self.assertTrue(report["ok"])

    def test_doctor_reports_missing_git_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.toml").write_text("[harness]\n")
            (root / "schemas").mkdir()
            for name in (
                "plan.schema.json",
                "step-result.schema.json",
                "review-result.schema.json",
                "state.schema.json",
            ):
                (root / "schemas" / name).write_text("{}")
            with (
                patch(
                    "harness.cli.subprocess.run",
                    side_effect=FileNotFoundError("git not found"),
                ),
                patch("harness.cli.shutil.which", return_value="/bin/codex"),
            ):
                report = run_doctor(root)
            self.assertFalse(report["ok"])
            git_check = next(
                check for check in report["checks"] if check["name"] == "Git repository"
            )
            self.assertFalse(git_check["ok"])


if __name__ == "__main__":
    unittest.main()
