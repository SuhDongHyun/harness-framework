from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..agents.process import (
    bounded_text,
    consume_bounded_stream,
    finish_readers,
    terminate_process_tree,
)


class VerificationSandbox(Protocol):
    def popen(
        self,
        argv: Sequence[str],
        cwd: Path,
    ) -> subprocess.Popen[bytes]: ...


@dataclass(frozen=True)
class CommandEvidence:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": round(self.duration_seconds, 6),
            "timed_out": self.timed_out,
        }


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    commands: tuple[CommandEvidence, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "commands": [command.to_dict() for command in self.commands],
        }

    def failure_summary(self) -> str:
        if self.ok or not self.commands:
            return ""
        failed = self.commands[-1]
        detail = failed.stderr.strip() or failed.stdout.strip() or "no output"
        return f"command failed ({failed.exit_code}): {' '.join(failed.argv)}\n{detail}"


class Verifier:
    def __init__(
        self,
        timeout_seconds: int,
        max_output_bytes: int,
        sandbox: VerificationSandbox | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.sandbox = sandbox

    def verify(
        self, commands: Sequence[Sequence[str]], cwd: Path
    ) -> VerificationResult:
        evidence: list[CommandEvidence] = []
        for command in commands:
            argv = tuple(command)
            if not argv or any(
                not isinstance(value, str) or not value for value in argv
            ):
                raise ValueError("verification command must be a non-empty argv array")
            started = time.monotonic()
            try:
                if self.sandbox is None:
                    process = subprocess.Popen(
                        list(argv),
                        cwd=cwd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        shell=False,
                        start_new_session=(os.name != "nt"),
                    )
                else:
                    process = self.sandbox.popen(argv, cwd)
                stdout_result: list[str] = []
                stderr_result: list[str] = []
                reader_errors: list[str] = []
                stdout_thread = threading.Thread(
                    target=consume_bounded_stream,
                    args=(
                        process.stdout,
                        self.max_output_bytes,
                        stdout_result,
                        reader_errors,
                        "stdout",
                    ),
                    daemon=True,
                )
                stderr_thread = threading.Thread(
                    target=consume_bounded_stream,
                    args=(
                        process.stderr,
                        self.max_output_bytes,
                        stderr_result,
                        reader_errors,
                        "stderr",
                    ),
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                timed_out = False
                try:
                    process.wait(timeout=self.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_process_tree(process)
                    process.wait()
                finish_readers(
                    process,
                    (stdout_thread, stderr_thread),
                    reader_errors,
                    "verification output reader did not terminate",
                )
                stdout = stdout_result[0] if stdout_result else ""
                stderr = stderr_result[0] if stderr_result else ""
                if reader_errors:
                    stderr = "\n".join(
                        value for value in [stderr, *reader_errors] if value
                    )
                item = CommandEvidence(
                    argv=argv,
                    exit_code=(
                        124
                        if timed_out
                        else 125
                        if reader_errors
                        else process.returncode
                    ),
                    stdout=stdout,
                    stderr=bounded_text(stderr, self.max_output_bytes),
                    duration_seconds=time.monotonic() - started,
                    timed_out=timed_out,
                )
            except OSError as error:
                item = CommandEvidence(
                    argv=argv,
                    exit_code=127,
                    stdout="",
                    stderr=bounded_text(str(error), self.max_output_bytes),
                    duration_seconds=time.monotonic() - started,
                    timed_out=False,
                )
            evidence.append(item)
            if item.exit_code != 0:
                return VerificationResult(False, tuple(evidence))
        return VerificationResult(True, tuple(evidence))
