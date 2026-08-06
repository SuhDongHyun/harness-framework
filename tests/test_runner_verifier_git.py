import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness.agents.runner import (
    AgentRunRequest,
    CodexRunner,
    CodexSandbox,
    codex_available_models,
    codex_login_status,
    harness_codex_home_status,
    parse_event_stream,
    prepare_harness_codex_home,
    resolve_harness_codex_home,
)
from harness.safety import GitGuard, Verifier, paths_outside_allowed


class RunnerTests(unittest.TestCase):
    def test_resolve_harness_codex_home_uses_linux_state_convention(self):
        self.assertEqual(
            Path("/state/personal-codex-harness/codex-home"),
            resolve_harness_codex_home({"XDG_STATE_HOME": "/state"}),
        )
        self.assertEqual(
            Path("/custom/codex-home"),
            resolve_harness_codex_home(
                {
                    "XDG_STATE_HOME": "/state",
                    "HARNESS_CODEX_HOME": "/custom/codex-home",
                }
            ),
        )

    def test_prepare_and_check_private_harness_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "state" / "codex-home"
            prepare_harness_codex_home(codex_home)
            codex_home.chmod(0o755)
            prepare_harness_codex_home(codex_home)
            auth = codex_home / "auth.json"
            auth.write_text("{}", encoding="utf-8")
            auth.chmod(0o600)
            self.assertEqual(0o700, codex_home.stat().st_mode & 0o777)
            self.assertEqual(
                (True, str(codex_home)),
                harness_codex_home_status(codex_home),
            )

    def test_codex_login_status_uses_dedicated_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "assert sys.argv[1:] == ['login', 'status']\n"
                "print(os.environ['CODEX_HOME'])\n"
            )
            executable.chmod(0o755)
            codex_home = root / "runtime-home"
            self.assertEqual(
                (True, str(codex_home)),
                codex_login_status(str(executable), codex_home),
            )

    def test_codex_available_models_reads_bundled_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "assert sys.argv[1:] == ['debug', 'models', '--bundled']\n"
                "assert os.environ['CODEX_HOME'].endswith('runtime-home')\n"
                "print(json.dumps({'models': ["
                "{'slug': 'planner-model'}, {'slug': 'reader-model'}]}))\n"
            )
            executable.chmod(0o755)
            ok, models, detail = codex_available_models(
                str(executable), root / "runtime-home"
            )
            self.assertTrue(ok)
            self.assertEqual(
                frozenset({"planner-model", "reader-model"}), models
            )
            self.assertEqual("2 bundled models", detail)

    def test_build_command_uses_explicit_sandbox_and_schema(self):
        request = AgentRunRequest(
            prompt="Do the step",
            sandbox="read-only",
            output_schema=Path("/repo/schema.json"),
            cwd=Path("/repo"),
            event_log=Path("/repo/events.jsonl"),
            timeout_seconds=30,
            max_event_log_bytes=10_000,
            max_final_payload_bytes=10_000,
            max_tool_output_bytes=2_000,
            model="planner-model",
            reasoning_effort="high",
        )
        command = CodexRunner("codex").build_command(request, Path("/repo/final.json"))
        self.assertEqual(
            [
                "codex",
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--model",
                "planner-model",
                "-c",
                'approval_policy="never"',
                "-c",
                "sandbox_workspace_write.writable_roots=[]",
                "-c",
                "sandbox_workspace_write.network_access=false",
                "-c",
                (
                    'shell_environment_policy.exclude=["CODEX_HOME",'
                    '"HARNESS_CODEX_HOME","OPENAI_API_KEY","CODEX_API_KEY"]'
                ),
                "-c",
                'model_reasoning_effort="high"',
                "-c",
                "agents.enabled=false",
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

    def test_build_command_configures_bounded_read_only_subagents(self):
        request = AgentRunRequest(
            prompt="Review",
            sandbox="read-only",
            output_schema=Path("/repo/schema.json"),
            cwd=Path("/repo"),
            event_log=Path("/repo/events.jsonl"),
            timeout_seconds=30,
            max_event_log_bytes=10_000,
            max_final_payload_bytes=10_000,
            max_tool_output_bytes=2_000,
            model="reviewer-model",
            reasoning_effort="high",
            subagents_enabled=True,
            max_subagents=3,
            subagent_model="reader-model",
            subagent_reasoning_effort="medium",
        )
        command = CodexRunner("codex").build_command(
            request, Path("/repo/final.json")
        )
        self.assertIn("agents.enabled=true", command)
        self.assertIn("agents.max_concurrent_threads_per_session=3", command)
        self.assertIn('agents.default_subagent_model="reader-model"', command)
        self.assertIn(
            'agents.default_subagent_reasoning_effort="medium"', command
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

    def test_run_collects_terminal_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
                "output.write_text(json.dumps({'value': 'ok'}))\n"
                "print(json.dumps({'type': 'turn.completed', 'usage': "
                "{'input_tokens': 10, 'output_tokens': 4}}))\n"
            )
            executable.chmod(0o755)
            schema = root / "schema.json"
            schema.write_text("{}")
            request = AgentRunRequest(
                prompt="Do the step",
                sandbox="read-only",
                output_schema=schema,
                cwd=root,
                event_log=root / "events.jsonl",
                timeout_seconds=5,
                max_event_log_bytes=10_000,
                max_final_payload_bytes=10_000,
                max_tool_output_bytes=2_000,
                model="planner-model",
                reasoning_effort="high",
            )
            result = CodexRunner(str(executable)).run(request)
            self.assertEqual({"input_tokens": 10, "output_tokens": 4}, result.usage)

    def test_run_sets_dedicated_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
                "output.write_text(json.dumps({'home': os.environ['CODEX_HOME']}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n"
            )
            executable.chmod(0o755)
            schema = root / "schema.json"
            schema.write_text("{}")
            codex_home = root / "runtime-home"
            request = AgentRunRequest(
                prompt="Do the step",
                sandbox="read-only",
                output_schema=schema,
                cwd=root,
                event_log=root / "events.jsonl",
                timeout_seconds=5,
                max_event_log_bytes=10_000,
                max_final_payload_bytes=10_000,
                max_tool_output_bytes=2_000,
                model="planner-model",
                reasoning_effort="high",
            )
            result = CodexRunner(
                str(executable), codex_home=codex_home
            ).run(request)
            self.assertEqual(str(codex_home), result.final_payload["home"])

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
                max_event_log_bytes=10_000,
                max_final_payload_bytes=10_000,
                max_tool_output_bytes=2_000,
                model="executor-model",
                reasoning_effort="xhigh",
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
                max_event_log_bytes=10_000,
                max_final_payload_bytes=10_000,
                max_tool_output_bytes=2_000,
                model="executor-model",
                reasoning_effort="xhigh",
            )
            result = CodexRunner(str(executable)).run(request)
            self.assertFalse(result.process_succeeded)
            self.assertEqual(1, result.malformed_event_count)

    @unittest.skipIf(sys.platform == "win32", "executable fixture uses a POSIX shebang")
    def test_final_payload_has_an_independent_hard_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
                "output.write_text(json.dumps({'value': 'x' * 10000}))\n"
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
                max_event_log_bytes=10_000,
                max_final_payload_bytes=256,
                max_tool_output_bytes=128,
                model="executor-model",
                reasoning_effort="xhigh",
            )
            result = CodexRunner(str(executable)).run(request)
            self.assertFalse(result.process_succeeded)
            self.assertTrue(result.final_payload_truncated)
            self.assertFalse(result.event_log_truncated)

    @unittest.skipIf(sys.platform == "win32", "executable fixture uses a POSIX shebang")
    def test_large_tool_output_is_compacted_before_event_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
                "output.write_text(json.dumps({'value': 'ok'}))\n"
                "print(json.dumps({'type': 'item.completed', 'item': "
                "{'type': 'command_execution', 'aggregated_output': 'x' * 5000}}))\n"
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
                max_event_log_bytes=1_024,
                max_final_payload_bytes=1_000,
                max_tool_output_bytes=256,
                model="executor-model",
                reasoning_effort="xhigh",
            )
            result = CodexRunner(str(executable)).run(request)
            self.assertTrue(result.process_succeeded)
            event = json.loads(request.event_log.read_text().splitlines()[0])
            item = event["item"]
            self.assertTrue(item["aggregated_output_truncated"])
            self.assertEqual(5000, item["aggregated_output_original_bytes"])
            self.assertLessEqual(len(item["aggregated_output"].encode()), 256)

    @unittest.skipIf(sys.platform == "win32", "executable fixture uses a POSIX shebang")
    def test_event_log_truncation_is_advisory_when_terminal_payload_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
                "output.write_text(json.dumps({'value': 'ok'}))\n"
                "for index in range(20):\n"
                "    print(json.dumps({'type': 'item.completed', "
                "'value': str(index) + 'x' * 100}))\n"
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
                max_event_log_bytes=1_024,
                max_final_payload_bytes=1_024,
                max_tool_output_bytes=1_024,
                model="executor-model",
                reasoning_effort="xhigh",
            )
            result = CodexRunner(str(executable)).run(request)
            self.assertTrue(result.process_succeeded)
            self.assertTrue(result.event_log_truncated)
            self.assertFalse(result.final_payload_truncated)
            self.assertEqual("turn.completed", result.terminal_event)

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
                max_event_log_bytes=10_000,
                max_final_payload_bytes=10_000,
                max_tool_output_bytes=2_000,
                model="executor-model",
                reasoning_effort="xhigh",
            )
            started = time.monotonic()
            result = CodexRunner(str(executable)).run(request)
            self.assertLess(time.monotonic() - started, 5)
            self.assertFalse(result.process_succeeded)
            self.assertIn("reader did not terminate", result.stderr)


class VerifierTests(unittest.TestCase):
    def test_build_command_uses_nested_workspace_sandbox(self):
        sandbox = CodexSandbox(
            codex_command="codex",
            codex_home=Path("/state/codex-home"),
        )
        verifier = Verifier(
            timeout_seconds=5,
            max_output_bytes=10_000,
            sandbox=sandbox,
        )
        self.assertEqual(
            [
                "codex",
                "sandbox",
                "--permission-profile",
                ":workspace",
                "--cd",
                "/repo",
                "-c",
                "sandbox_workspace_write.writable_roots=[]",
                "-c",
                "sandbox_workspace_write.network_access=false",
                "--",
                "/usr/bin/env",
                "-u",
                "CODEX_HOME",
                "-u",
                "HARNESS_CODEX_HOME",
                "-u",
                "OPENAI_API_KEY",
                "-u",
                "CODEX_API_KEY",
                "python3",
                "-m",
                "unittest",
            ],
            sandbox.build_command(
                ["python3", "-m", "unittest"], Path("/repo")
            ),
        )
        self.assertIs(sandbox, verifier.sandbox)

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
