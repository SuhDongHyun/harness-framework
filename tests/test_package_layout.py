import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "harness"


class PackageLayoutTests(unittest.TestCase):
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
