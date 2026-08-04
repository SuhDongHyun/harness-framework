from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .process import (
    bounded_text,
    coerce_text,
    consume_bounded_stream,
    finish_readers,
    terminate_process_tree,
)

TERMINAL_EVENT_TYPES = frozenset({"turn.completed", "turn.failed", "error"})


@dataclass(frozen=True)
class AgentRunRequest:
    prompt: str
    sandbox: str
    output_schema: Path
    cwd: Path
    event_log: Path
    timeout_seconds: int
    max_output_bytes: int
    model: str
    reasoning_effort: str
    subagents_enabled: bool = False
    max_subagents: int = 1
    subagent_model: str | None = None
    subagent_reasoning_effort: str | None = None


@dataclass(frozen=True)
class AgentRunResult:
    exit_code: int
    final_payload: dict[str, object] | None
    stderr: str
    timed_out: bool
    terminal_event: str | None
    malformed_event_count: int = 0
    output_truncated: bool = False
    reader_failed: bool = False
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def process_succeeded(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and self.terminal_event == "turn.completed"
            and self.final_payload is not None
            and self.malformed_event_count == 0
            and not self.output_truncated
            and not self.reader_failed
        )


class AgentRunner(Protocol):
    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Run one agent turn and return controller-consumable evidence."""


def parse_event_stream(raw: str) -> tuple[list[dict[str, object]], str | None]:
    events: list[dict[str, object]] = []
    terminal: str | None = None
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise TypeError("event is not an object")
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            event = {
                "type": "harness.malformed_event",
                "line_number": line_number,
                "error": str(error),
                "raw": line[:500],
            }
        events.append(event)
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type in TERMINAL_EVENT_TYPES:
            terminal = event_type
    return events, terminal


class CodexRunner:
    def __init__(
        self,
        codex_command: str = "codex",
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ):
        self.codex_command = codex_command
        self.event_sink = event_sink

    def build_command(
        self, request: AgentRunRequest, final_output_path: Path
    ) -> list[str]:
        if request.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError(f"unsupported sandbox: {request.sandbox!r}")
        command = [
            self.codex_command,
            "exec",
            "--model",
            request.model,
            "-c",
            f'model_reasoning_effort="{request.reasoning_effort}"',
            "-c",
            f"agents.enabled={'true' if request.subagents_enabled else 'false'}",
        ]
        if request.subagents_enabled:
            if not request.subagent_model or not request.subagent_reasoning_effort:
                raise ValueError("enabled subagents require a model and reasoning effort")
            command.extend(
                [
                    "-c",
                    "agents.max_concurrent_threads_per_session="
                    + str(request.max_subagents),
                    "-c",
                    "agents.default_subagent_model="
                    + json.dumps(request.subagent_model),
                    "-c",
                    "agents.default_subagent_reasoning_effort="
                    + json.dumps(request.subagent_reasoning_effort),
                ]
            )
        command.extend(
            [
                "--json",
                "--sandbox",
                request.sandbox,
                "--output-schema",
                str(request.output_schema),
                "-o",
                str(final_output_path),
                request.prompt,
            ]
        )
        return command

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        request.event_log.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".codex-final-",
            suffix=".json",
            dir=request.event_log.parent,
            delete=False,
        ) as handle:
            final_output_path = Path(handle.name)
        with tempfile.NamedTemporaryFile(
            prefix=".codex-events-",
            suffix=".jsonl",
            dir=request.event_log.parent,
            delete=False,
        ) as handle:
            event_output_path = Path(handle.name)

        stderr = ""
        timed_out = False
        exit_code = 1
        terminal: str | None = None
        final_payload: dict[str, object] | None = None
        malformed_event_count = 0
        output_truncated = False
        reader_failed = False
        usage: dict[str, int] = {}
        try:
            process = subprocess.Popen(
                self.build_command(request, final_output_path),
                cwd=request.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name != "nt"),
            )
            event_result: list[tuple[str | None, int, bool, dict[str, int]]] = []
            stderr_result: list[str] = []
            reader_errors: list[str] = []
            stdout_thread = threading.Thread(
                target=_consume_event_stream,
                args=(
                    process.stdout,
                    event_output_path,
                    request.max_output_bytes,
                    event_result,
                    reader_errors,
                    self.event_sink,
                ),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=consume_bounded_stream,
                args=(
                    process.stderr,
                    request.max_output_bytes,
                    stderr_result,
                    reader_errors,
                    "stderr",
                ),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            try:
                process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process_tree(process)
                process.wait()
            finish_readers(
                process,
                (stdout_thread, stderr_thread),
                reader_errors,
                "Codex output reader did not terminate",
            )
            exit_code = process.returncode if process.returncode is not None else 1
            if event_result:
                terminal, malformed_event_count, output_truncated, usage = event_result[0]
            os.replace(event_output_path, request.event_log)
            stderr = stderr_result[0] if stderr_result else ""
            if reader_errors:
                stderr = "\n".join(value for value in [stderr, *reader_errors] if value)
                reader_failed = True
            final_payload, final_truncated = self._read_final_payload(
                final_output_path, request.max_output_bytes
            )
            output_truncated = output_truncated or final_truncated
        except OSError as error:
            stderr = str(error)
            request.event_log.write_text("", encoding="utf-8")
        finally:
            final_output_path.unlink(missing_ok=True)
            event_output_path.unlink(missing_ok=True)

        return AgentRunResult(
            exit_code=exit_code,
            final_payload=final_payload,
            stderr=bounded_text(stderr, request.max_output_bytes),
            timed_out=timed_out,
            terminal_event=terminal,
            malformed_event_count=malformed_event_count,
            output_truncated=output_truncated,
            reader_failed=reader_failed,
            usage=usage,
        )

    @staticmethod
    def _read_final_payload(
        path: Path, max_bytes: int
    ) -> tuple[dict[str, object] | None, bool]:
        try:
            with path.open("rb") as handle:
                raw = handle.read(max_bytes + 1)
        except OSError:
            return None, False
        if len(raw) > max_bytes:
            return None, True
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, False
        return (payload if isinstance(payload, dict) else None), False


def _consume_event_stream(
    source,
    event_log: Path,
    max_bytes: int,
    result: list[tuple[str | None, int, bool, dict[str, int]]],
    errors: list[str],
    event_sink: Callable[[dict[str, object]], None] | None = None,
) -> None:
    terminal: str | None = None
    malformed_count = 0
    truncated = False
    written = 0
    usage: dict[str, int] = {}
    try:
        with event_log.open("wb") as event_handle:
            line_number = 0
            while True:
                raw_line = source.readline(max_bytes + 1)
                if not raw_line:
                    break
                line_number += 1
                if len(raw_line) > max_bytes:
                    truncated = True
                    malformed_count += 1
                    while not raw_line.endswith(b"\n"):
                        raw_line = source.readline(64 * 1024)
                        if not raw_line:
                            break
                    marker = b'{"type":"harness.output_truncated"}\n'
                    if written + len(marker) <= max_bytes:
                        event_handle.write(marker)
                        written += len(marker)
                    continue
                line = coerce_text(raw_line).rstrip("\r\n")
                events, line_terminal = parse_event_stream(line)
                if not events and not line:
                    continue
                event = (
                    events[0]
                    if events
                    else {
                        "type": "harness.malformed_event",
                        "line_number": line_number,
                        "raw": line[:500],
                    }
                )
                if event.get("type") == "harness.malformed_event":
                    event["line_number"] = line_number
                    malformed_count += 1
                if event_sink is not None:
                    _publish_event(event_sink, event)
                encoded = (
                    json.dumps(
                        event,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                if written + len(encoded) > max_bytes:
                    if not truncated:
                        marker = b'{"type":"harness.output_truncated"}\n'
                        if written + len(marker) <= max_bytes:
                            event_handle.write(marker)
                            written += len(marker)
                    truncated = True
                else:
                    event_handle.write(encoded)
                    written += len(encoded)
                if line_terminal:
                    terminal = line_terminal
                if event.get("type") == "turn.completed":
                    raw_usage = event.get("usage")
                    if isinstance(raw_usage, dict):
                        for key, value in raw_usage.items():
                            if (
                                isinstance(key, str)
                                and isinstance(value, int)
                                and not isinstance(value, bool)
                                and value >= 0
                            ):
                                usage[key] = usage.get(key, 0) + value
        result.append((terminal, malformed_count, truncated, usage))
    except (OSError, ValueError) as error:
        errors.append(f"stdout reader failed: {error}")


def _publish_event(
    event_sink: Callable[[dict[str, object]], None],
    event: dict[str, object],
) -> None:
    try:
        event_sink(dict(event))
    except Exception:  # noqa: BLE001 -- telemetry must never fail an agent run
        return
