import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness.git_guard import GitGuard, paths_outside_allowed
from harness.runner import (
    AgentRunRequest,
    CodexRunner,
    parse_event_stream,
)
from harness.verifier import Verifier


class RunnerTests(unittest.TestCase):
    def test_build_command_uses_explicit_sandbox_and_schema(self):
        request = AgentRunRequest(
            prompt="Do the step",
            sandbox="read-only",
            output_schema=Path("/repo/schema.json"),
            cwd=Path("/repo"),
            event_log=Path("/repo/events.jsonl"),
            timeout_seconds=30,
            max_output_bytes=10_000,
        )
        command = CodexRunner("codex").build_command(request, Path("/repo/final.json"))
        self.assertEqual(
            [
                "codex",
                "exec",
                "--json",
                "--sandbox",
                "read-only",
                "--output-schema",
                "/repo/schema.json",
                "-o",
                "/repo/final.json",
                "Do the step",
            ],
            command,
        )

    def test_parse_event_stream_finds_terminal_event_and_malformed_line(self):
        raw = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "abc"}),
                "not-json",
                json.dumps({"type": "turn.completed"}),
            ]
        )
        events, terminal = parse_event_stream(raw)
        self.assertEqual("turn.completed", terminal)
        self.assertEqual("harness.malformed_event", events[1]["type"])

    def test_parse_event_stream_prefers_last_terminal_event(self):
        raw = "\n".join(
            [
                json.dumps({"type": "turn.failed"}),
                json.dumps({"type": "error"}),
            ]
        )
        _, terminal = parse_event_stream(raw)
        self.assertEqual("error", terminal)

    @unittest.skipIf(sys.platform == "win32", "executable fixture uses a POSIX shebang")
    def test_run_captures_events_and_structured_final_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
                "output.write_text(json.dumps({'value': 'ok'}))\n"
                "print(json.dumps({'type': 'thread.started', 'thread_id': 'abc'}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            executable.chmod(0o755)
            schema = root / "schema.json"
            schema.write_text("{}")
            request = AgentRunRequest(
                prompt="Do the step",
                sandbox="workspace-write",
                output_schema=schema,
                cwd=root,
                event_log=root / "events.jsonl",
                timeout_seconds=5,
                max_output_bytes=10_000,
            )
            result = CodexRunner(str(executable)).run(request)
            self.assertTrue(result.process_succeeded)
            self.assertEqual({"value": "ok"}, result.final_payload)
            event_types = [
                json.loads(line)["type"]
                for line in request.event_log.read_text().splitlines()
            ]
            self.assertEqual(["thread.started", "turn.completed"], event_types)

    @unittest.skipIf(sys.platform == "win32", "executable fixture uses a POSIX shebang")
    def test_malformed_event_prevents_process_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
                "output.write_text(json.dumps({'value': 'ok'}))\n"
                "print('not-json')\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            executable.chmod(0o755)
            schema = root / "schema.json"
            schema.write_text("{}")
            request = AgentRunRequest(
                prompt="Do the step",
                sandbox="workspace-write",
                output_schema=schema,
                cwd=root,
                event_log=root / "events.jsonl",
                timeout_seconds=5,
                max_output_bytes=10_000,
            )
            result = CodexRunner(str(executable)).run(request)
            self.assertFalse(result.process_succeeded)
            self.assertEqual(1, result.malformed_event_count)

    @unittest.skipIf(sys.platform == "win32", "executable fixture uses a POSIX shebang")
    def test_event_and_final_output_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
                "output.write_text(json.dumps({'value': 'x' * 10000}))\n"
                "print(json.dumps({'type': 'item.completed', 'value': 'x' * 10000}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            executable.chmod(0o755)
            schema = root / "schema.json"
            schema.write_text("{}")
            request = AgentRunRequest(
                prompt="Do the step",
                sandbox="workspace-write",
                output_schema=schema,
                cwd=root,
                event_log=root / "events.jsonl",
                timeout_seconds=5,
                max_output_bytes=256,
            )
            result = CodexRunner(str(executable)).run(request)
            self.assertFalse(result.process_succeeded)
            self.assertTrue(result.output_truncated)
            self.assertLess(request.event_log.stat().st_size, 1024)

    @unittest.skipIf(sys.platform == "win32", "fixture relies on POSIX process groups")
    def test_descendant_holding_pipe_does_not_block_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, subprocess, sys\n"
                "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
                "output.write_text(json.dumps({'value': 'ok'}))\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            executable.chmod(0o755)
            schema = root / "schema.json"
            schema.write_text("{}")
            request = AgentRunRequest(
                prompt="Do the step",
                sandbox="workspace-write",
                output_schema=schema,
                cwd=root,
                event_log=root / "events.jsonl",
                timeout_seconds=5,
                max_output_bytes=10_000,
            )
            started = time.monotonic()
            result = CodexRunner(str(executable)).run(request)
            self.assertLess(time.monotonic() - started, 5)
            self.assertFalse(result.process_succeeded)
            self.assertIn("reader did not terminate", result.stderr)


class VerifierTests(unittest.TestCase):
    def test_success(self):
        verifier = Verifier(timeout_seconds=5, max_output_bytes=10_000)
        result = verifier.verify(
            [[sys.executable, "-c", "print('ok')"]],
            Path.cwd(),
        )
        self.assertTrue(result.ok)
        self.assertEqual("ok\n", result.commands[0].stdout)

    def test_stops_on_first_failure(self):
        verifier = Verifier(timeout_seconds=5, max_output_bytes=10_000)
        result = verifier.verify(
            [
                [sys.executable, "-c", "raise SystemExit(7)"],
                [sys.executable, "-c", "print('must not run')"],
            ],
            Path.cwd(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(1, len(result.commands))
        self.assertEqual(7, result.commands[0].exit_code)

    def test_timeout(self):
        verifier = Verifier(timeout_seconds=1, max_output_bytes=10_000)
        result = verifier.verify(
            [[sys.executable, "-c", "import time; time.sleep(5)"]],
            Path.cwd(),
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.commands[0].timed_out)

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group behavior")
    def test_timeout_kills_child_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child-finished"
            child = (
                "import time, pathlib; "
                f"time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('x')"
            )
            parent = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                "time.sleep(10)"
            )
            verifier = Verifier(timeout_seconds=1, max_output_bytes=10_000)
            result = verifier.verify(
                [[sys.executable, "-c", parent]],
                Path.cwd(),
            )
            time.sleep(2)
            self.assertTrue(result.commands[0].timed_out)
            self.assertFalse(marker.exists())

    def test_output_is_truncated(self):
        verifier = Verifier(timeout_seconds=5, max_output_bytes=32)
        result = verifier.verify(
            [[sys.executable, "-c", "print('x' * 1000)"]],
            Path.cwd(),
        )
        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.commands[0].stdout.encode()), 64)
        self.assertIn("truncated", result.commands[0].stdout)


class GitGuardTests(unittest.TestCase):
    def _repo(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=root,
            check=True,
        )
        (root / "tracked.txt").write_text("base\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)

    def test_detects_new_and_preexisting_dirty_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root)
            guard = GitGuard(root)
            (root / "tracked.txt").write_text("dirty before\n")
            before = guard.snapshot()
            (root / "tracked.txt").write_text("dirty after\n")
            (root / "new.txt").write_text("new\n")
            after = guard.snapshot()
            self.assertEqual(
                {"tracked.txt", "new.txt"},
                guard.changed_paths(before, after),
            )

    def test_fingerprint_is_stable_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root)
            guard = GitGuard(root)
            self.assertEqual(
                guard.snapshot().fingerprint,
                guard.snapshot().fingerprint,
            )

    def test_detects_index_only_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root)
            guard = GitGuard(root)
            (root / "tracked.txt").write_text("dirty\n")
            before = guard.snapshot()
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            after = guard.snapshot()
            self.assertNotEqual(
                before.index_fingerprint,
                after.index_fingerprint,
            )
            self.assertEqual(
                {"tracked.txt"},
                guard.changed_paths(before, after),
            )

    def test_paths_outside_allowed(self):
        outside = paths_outside_allowed(
            {"src/core.py", "tests/test_core.py", "README.md"},
            ("src/**", "tests/**"),
        )
        self.assertEqual({"README.md"}, outside)


if __name__ == "__main__":
    unittest.main()
