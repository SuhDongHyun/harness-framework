import tempfile
import unittest
from pathlib import Path

from harness.domain import ValidationError
from harness.safety.workspace import (
    WorkspaceChange,
    allowed_paths_overlap,
    apply_workspace_changes,
    collect_workspace_changes,
)


class WorkspaceTests(unittest.TestCase):
    def test_detects_prefix_and_glob_overlap(self):
        self.assertTrue(allowed_paths_overlap(("src/**",), ("src/api.py",)))
        self.assertFalse(allowed_paths_overlap(("src/**",), ("tests/**",)))

    def test_collects_and_applies_regular_file_change(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            workspace = Path(first)
            root = Path(second)
            (workspace / "src").mkdir()
            (workspace / "src" / "feature.py").write_text("value = 1\n")
            changes = collect_workspace_changes(workspace, {"src/feature.py"})
            apply_workspace_changes(root, changes)
            self.assertEqual("value = 1\n", (root / "src" / "feature.py").read_text())

    def test_rejects_deletion_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaisesRegex(ValidationError, "deletion"):
                collect_workspace_changes(workspace, {"missing.txt"})
            (workspace / "target.txt").write_text("safe")
            (workspace / "link.txt").symlink_to("target.txt")
            with self.assertRaisesRegex(ValidationError, "regular files"):
                collect_workspace_changes(workspace, {"link.txt"})

    def test_refuses_to_replace_destination_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "target.txt").write_text("safe")
            (root / "link.txt").symlink_to("target.txt")
            change = WorkspaceChange("link.txt", b"unsafe", 0o644)
            with self.assertRaisesRegex(ValidationError, "symlink"):
                apply_workspace_changes(root, (change,))


if __name__ == "__main__":
    unittest.main()
