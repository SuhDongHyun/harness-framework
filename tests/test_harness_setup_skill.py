import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (
    ROOT
    / ".agents"
    / "skills"
    / "harness-setup"
    / "scripts"
    / "merge_writable_root.py"
)


class HarnessSetupSkillTests(unittest.TestCase):
    def run_script(self, config: Path, runtime_root: Path):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(config),
                "--root",
                str(runtime_root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_merges_root_without_replacing_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "outer-codex" / "config.toml"
            config.parent.mkdir()
            config.write_text(
                'model = "example"\n\n'
                "[sandbox_workspace_write]\n"
                "network_access = true\n",
                encoding="utf-8",
            )
            runtime_root = base / "state" / "codex-home"
            result = self.run_script(config, runtime_root)
            self.assertEqual(0, result.returncode, result.stderr)
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual("example", parsed["model"])
            self.assertTrue(parsed["sandbox_workspace_write"]["network_access"])
            self.assertEqual(
                [str(runtime_root)],
                parsed["sandbox_workspace_write"]["writable_roots"],
            )
            self.assertTrue(json.loads(result.stdout)["changed"])

    def test_appends_to_existing_roots_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.toml"
            config.write_text(
                "[sandbox_workspace_write]\n"
                'writable_roots = ["/existing"]\n',
                encoding="utf-8",
            )
            runtime_root = base / "runtime"
            first = self.run_script(config, runtime_root)
            second = self.run_script(config, runtime_root)
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(
                ["/existing", str(runtime_root)],
                parsed["sandbox_workspace_write"]["writable_roots"],
            )
            self.assertFalse(json.loads(second.stdout)["changed"])

    def test_refuses_to_edit_dedicated_runtime_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp) / "runtime"
            runtime_root.mkdir()
            result = self.run_script(runtime_root / "config.toml", runtime_root)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("dedicated Harness Codex home", result.stderr)


if __name__ == "__main__":
    unittest.main()
