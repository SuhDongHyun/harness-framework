from __future__ import annotations

import os
import signal
import subprocess
import threading


def bounded_text(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    prefix = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return prefix + "\n...[truncated]"


def coerce_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def consume_bounded_stream(
    source,
    max_bytes: int,
    result: list[str],
    errors: list[str],
    label: str,
) -> None:
    try:
        captured = bytearray()
        while chunk := source.read(64 * 1024):
            if len(captured) <= max_bytes:
                captured.extend(chunk[: max_bytes + 1 - len(captured)])
        result.append(bounded_text(coerce_text(bytes(captured)), max_bytes))
    except (OSError, ValueError) as error:
        errors.append(f"{label} reader failed: {error}")


def finish_readers(
    process: subprocess.Popen,
    threads: tuple[threading.Thread, ...],
    errors: list[str],
    stuck_message: str,
) -> None:
    for thread in threads:
        thread.join(timeout=1)
    if any(thread.is_alive() for thread in threads):
        terminate_process_tree(process)
        errors.append(stuck_message)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    for thread in threads:
        thread.join(timeout=1)


def terminate_process_tree(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
