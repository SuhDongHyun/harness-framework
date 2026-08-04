import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "harness"
SKILLS = ROOT / ".agents" / "skills"


class PackageLayoutTests(unittest.TestCase):
    def test_harness_design_is_repository_level(self):
        self.assertTrue((ROOT / "HARNESS_DESIGN.md").is_file())
        self.assertFalse((ROOT / "docs" / "HARNESS_DESIGN.md").exists())

    def test_skill_first_workflow_is_available(self):
        plan = (SKILLS / "harness-plan" / "SKILL.md").read_text()
        setup = (SKILLS / "harness-setup" / "SKILL.md").read_text()
        approve = (SKILLS / "harness-approve" / "SKILL.md").read_text()
        approve_metadata = (
            SKILLS / "harness-approve" / "agents" / "openai.yaml"
        ).read_text()
        self.assertIn("ui <run-id> --open-browser", plan)
        self.assertIn("$harness-setup", plan)
        self.assertIn("Never escalate the `plan` command", plan)
        self.assertIn("codex login status", setup)
        self.assertIn("merge_writable_root.py", setup)
        self.assertTrue(
            (SKILLS / "harness-setup" / "scripts" / "merge_writable_root.py").is_file()
        )
        self.assertIn("approve <run-id>", approve)
        self.assertIn("run <run-id>", approve)
        self.assertIn("review <run-id>", approve)
        self.assertIn("Never escalate `run`", approve)
        self.assertIn("Never escalate `review`", approve)
        self.assertIn("allow_implicit_invocation: false", approve_metadata)

    def test_top_level_contains_only_composition_modules(self):
        modules = {path.name for path in HARNESS.glob("*.py")}
        self.assertEqual({"__init__.py", "cli.py", "config.py"}, modules)

    def test_responsibility_packages_exist(self):
        for name in (
            "agents",
            "domain",
            "orchestration",
            "safety",
            "storage",
            "ui",
        ):
            with self.subTest(package=name):
                self.assertTrue((HARNESS / name / "__init__.py").is_file())


if __name__ == "__main__":
    unittest.main()
