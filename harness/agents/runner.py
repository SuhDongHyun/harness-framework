from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
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
HARNESS_CODEX_HOME_ENV = "HARNESS_CODEX_HOME"
MAX_RAW_EVENT_BYTES = 10_000_000


def resolve_harness_codex_home(
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    override = environment.get(HARNESS_CODEX_HOME_ENV)
    if override:
        path = Path(override).expanduser()
    else:
        state_root = environment.get("XDG_STATE_HOME")
        base = (
            Path(state_root).expanduser()
            if state_root
            else Path.home() / ".local" / "state"
        )
        path = base / "personal-codex-harness" / "codex-home"
    if not path.is_absolute():
        raise ValueError(f"{HARNESS_CODEX_HOME_ENV} must be an absolute path")
    return path.absolute()


def prepare_harness_codex_home(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError(f"Codex runtime home is not a regular directory: {path}")
    if path.stat().st_uid != os.getuid():
        raise PermissionError(f"Codex runtime home must be owned by this user: {path}")
    path.chmod(0o700)


def harness_codex_home_status(path: Path) -> tuple[bool, str]:
    if not path.is_dir() or path.is_symlink():
        return False, f"{path} does not exist as a regular directory"
    if path.stat().st_uid != os.getuid():
        return False, f"{path} must be owned by this user"
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return False, f"{path} must have mode 0700"
    if not os.access(path, os.W_OK):
        return False, f"{path} is not writable in the active sandbox"
    auth_path = path / "auth.json"
    if not auth_path.is_file() or auth_path.is_symlink():
        return False, f"{auth_path} is missing; log in with this CODEX_HOME"
    auth_mode = stat.S_IMODE(auth_path.stat().st_mode)
    if auth_mode & 0o077:
        return False, f"{auth_path} must not be accessible by group or other users"
    if not os.access(auth_path, os.R_OK | os.W_OK):
        return False, f"{auth_path} must be readable and writable by this user"
    return True, str(path)


def codex_login_status(
    codex_command: str,
    codex_home: Path,
    timeout_seconds: int = 10,
) -> tuple[bool, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    try:
        result = subprocess.run(
            [codex_command, "login", "status"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    detail = result.stdout.strip() or result.stderr.strip()
    return result.returncode == 0, detail or f"exit code {result.returncode}"


def codex_available_models(
    codex_command: str,
    codex_home: Path,
    timeout_seconds: int = 10,
) -> tuple[bool, frozenset[str], str]:
    """Read the CLI's bundled model catalog without a network refresh."""
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    try:
        result = subprocess.run(
            [codex_command, "debug", "models", "--bundled"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, frozenset(), str(error)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return False, frozenset(), bounded_text(detail, 1000)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return False, frozenset(), f"invalid model catalog JSON: {error}"
    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        return False, frozenset(), "model catalog does not contain a models array"
    models = frozenset(
        value["slug"]
        for value in raw_models
        if isinstance(value, dict)
        and isinstance(value.get("slug"), str)
        and value["slug"]
    )
    if not models:
        return False, models, "model catalog contains no model slugs"
    return True, models, f"{len(models)} bundled models"


class CodexSandbox:
    def __init__(self, codex_command: str, codex_home: Path):
        self.codex_command = codex_command
        self.codex_home = codex_home

    def build_command(self, argv: Sequence[str], cwd: Path) -> list[str]:
        return [
            self.codex_command,
            "sandbox",
            "--permission-profile",
            ":workspace",
            "--cd",
            str(cwd),
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
            *argv,
        ]

    def popen(
        self,
        argv: Sequence[str],
        cwd: Path,
    ) -> subprocess.Popen[bytes]:
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.codex_home)
        return subprocess.Popen(
            self.build_command(argv, cwd),
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )


@dataclass(frozen=True)
class AgentRunRequest:
    prompt: str
    sandbox: str
    output_schema: Path
    cwd: Path
    event_log: Path
    timeout_seconds: int
    max_event_log_bytes: int
    max_final_payload_bytes: int
    max_tool_output_bytes: int
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
    event_log_truncated: bool = False
    final_payload_truncated: bool = False
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
            and not self.final_payload_truncated
            and not self.reader_failed
        )

    @property
    def output_truncated(self) -> bool:
        """Compatibility summary for persisted evidence and older callers."""
        return self.event_log_truncated or self.final_payload_truncated


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
        codex_home: Path | None = None,
    ):
        self.codex_command = codex_command
        self.event_sink = event_sink
        self.codex_home = codex_home

    def build_command(
        self, request: AgentRunRequest, final_output_path: Path
    ) -> list[str]:
        if request.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError(f"unsupported sandbox: {request.sandbox!r}")
        command = [
            self.codex_command,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--model",
            request.model,
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
        event_log_truncated = False
        final_payload_truncated = False
        reader_failed = False
        usage: dict[str, int] = {}
        try:
            process = subprocess.Popen(
                self.build_command(request, final_output_path),
                cwd=request.cwd,
                env=self._environment(),
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
                    request.max_event_log_bytes,
                    request.max_tool_output_bytes,
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
                    request.max_event_log_bytes,
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
                terminal, malformed_event_count, event_log_truncated, usage = (
                    event_result[0]
                )
            os.replace(event_output_path, request.event_log)
            stderr = stderr_result[0] if stderr_result else ""
            if reader_errors:
                stderr = "\n".join(value for value in [stderr, *reader_errors] if value)
                reader_failed = True
            final_payload, final_truncated = self._read_final_payload(
                final_output_path, request.max_final_payload_bytes
            )
            final_payload_truncated = final_truncated
        except OSError as error:
            stderr = str(error)
            request.event_log.write_text("", encoding="utf-8")
        finally:
            final_output_path.unlink(missing_ok=True)
            event_output_path.unlink(missing_ok=True)

        return AgentRunResult(
            exit_code=exit_code,
            final_payload=final_payload,
            stderr=bounded_text(stderr, request.max_event_log_bytes),
            timed_out=timed_out,
            terminal_event=terminal,
            malformed_event_count=malformed_event_count,
            event_log_truncated=event_log_truncated,
            final_payload_truncated=final_payload_truncated,
            reader_failed=reader_failed,
            usage=usage,
        )

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if self.codex_home is not None:
            environment["CODEX_HOME"] = str(self.codex_home)
        return environment

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
    max_event_log_bytes: int,
    max_tool_output_bytes: int,
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
                raw_line = source.readline(MAX_RAW_EVENT_BYTES + 1)
                if not raw_line:
                    break
                line_number += 1
                if len(raw_line) > MAX_RAW_EVENT_BYTES:
                    truncated = True
                    malformed_count += 1
                    while not raw_line.endswith(b"\n"):
                        raw_line = source.readline(64 * 1024)
                        if not raw_line:
                            break
                    marker = b'{"type":"harness.output_truncated"}\n'
                    if written + len(marker) <= max_event_log_bytes:
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
                event = _compact_tool_output(event, max_tool_output_bytes)
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
                if written + len(encoded) > max_event_log_bytes:
                    if not truncated:
                        marker = b'{"type":"harness.output_truncated"}\n'
                        if written + len(marker) <= max_event_log_bytes:
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


def _compact_tool_output(
    event: dict[str, object], max_bytes: int
) -> dict[str, object]:
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "command_execution":
        return event
    compact_item = dict(item)
    changed = False
    for key in ("aggregated_output", "aggregatedOutput"):
        value = compact_item.get(key)
        if not isinstance(value, str):
            continue
        original_bytes = len(value.encode("utf-8", errors="replace"))
        if original_bytes <= max_bytes:
            continue
        compact_item[key] = _summarize_text(value, max_bytes, original_bytes)
        compact_item[f"{key}_truncated"] = True
        compact_item[f"{key}_original_bytes"] = original_bytes
        changed = True
    if not changed:
        return event
    compact_event = dict(event)
    compact_event["item"] = compact_item
    return compact_event


def _summarize_text(text: str, max_bytes: int, original_bytes: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    omitted = max(0, original_bytes - max_bytes)
    marker = f"\n...[tool output truncated; at least {omitted} bytes omitted]...\n".encode()
    available = max(0, max_bytes - len(marker))
    prefix_bytes = available * 2 // 3
    suffix_bytes = available - prefix_bytes
    prefix = encoded[:prefix_bytes].decode("utf-8", errors="ignore")
    suffix = (
        encoded[-suffix_bytes:].decode("utf-8", errors="ignore")
        if suffix_bytes
        else ""
    )
    return prefix + marker.decode() + suffix


def _publish_event(
    event_sink: Callable[[dict[str, object]], None],
    event: dict[str, object],
) -> None:
    try:
        event_sink(dict(event))
    except Exception:  # noqa: BLE001 -- telemetry must never fail an agent run
        return
