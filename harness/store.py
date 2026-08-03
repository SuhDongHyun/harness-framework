from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .models import ValidationError, validate_id


class RunStore:
    """Owns controller-managed run artifacts under one runs root."""

    def __init__(self, runs_root: Path):
        self.runs_root = runs_root.resolve()

    def run_dir(self, run_id: str) -> Path:
        validate_id(run_id, "run id")
        return self.runs_root / run_id

    def create_run(self, run_id: str, goal: str) -> Path:
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "steps").mkdir()
        (run_dir / "evidence").mkdir()
        self.write_text_atomic(run_id, "request.md", goal.rstrip() + "\n")
        return run_dir

    def artifact_path(self, run_id: str, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ValidationError(f"unsafe artifact name: {name!r}")
        return self.run_dir(run_id) / name

    def evidence_path(self, run_id: str, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ValidationError(f"unsafe evidence name: {name!r}")
        return self.run_dir(run_id) / "evidence" / name

    def read_json(self, run_id: str, name: str) -> dict[str, object]:
        path = self.artifact_path(run_id, name)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValidationError(f"cannot read {path}: {error}") from error
        if not isinstance(value, dict):
            raise ValidationError(f"{path} must contain a JSON object")
        return value

    def write_json_atomic(
        self, run_id: str, name: str, data: Mapping[str, object]
    ) -> None:
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        self.write_text_atomic(run_id, name, payload)

    def write_evidence_atomic(
        self, run_id: str, name: str, data: Mapping[str, object]
    ) -> None:
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        self._atomic_write(self.evidence_path(run_id, name), payload)

    def write_text_atomic(self, run_id: str, name: str, text: str) -> None:
        self._atomic_write(self.artifact_path(run_id, name), text)

    def write_step_text(
        self, run_id: str, index: int, step_name: str, text: str
    ) -> Path:
        validate_id(step_name, "step name")
        if index < 0 or index > 99:
            raise ValidationError("step index must be between 0 and 99")
        path = self.run_dir(run_id) / "steps" / f"{index:02d}-{step_name}.md"
        self._atomic_write(path, text)
        return path

    def clear_step_documents(self, run_id: str) -> None:
        steps_dir = self.run_dir(run_id) / "steps"
        for path in steps_dir.glob("[0-9][0-9]-*.md"):
            if path.is_file() and not path.is_symlink():
                path.unlink()

    def capture_runs_files(self) -> dict[str, bytes]:
        self.runs_root.mkdir(parents=True, exist_ok=True)
        if self.runs_root.is_symlink():
            raise ValidationError("runs root must not be a symlink")
        captured: dict[str, bytes] = {}
        for current, directory_names, file_names in os.walk(
            self.runs_root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            for name in list(directory_names):
                path = current_path / name
                if path.is_symlink():
                    relative = path.relative_to(self.runs_root).as_posix()
                    captured[relative] = b"HARNESS-SYMLINK\0" + os.readlink(
                        path
                    ).encode("utf-8", errors="surrogateescape")
                    directory_names.remove(name)
            for name in file_names:
                path = current_path / name
                relative = path.relative_to(self.runs_root).as_posix()
                if path.is_symlink():
                    captured[relative] = b"HARNESS-SYMLINK\0" + os.readlink(
                        path
                    ).encode("utf-8", errors="surrogateescape")
                else:
                    captured[relative] = path.read_bytes()
        return captured

    def restore_runs_files(self, snapshot: Mapping[str, bytes]) -> None:
        if self.runs_root.is_symlink():
            raise ValidationError("runs root must not be a symlink")
        for relative, payload in snapshot.items():
            if payload.startswith(b"HARNESS-SYMLINK\0"):
                raise ValidationError("run snapshots cannot restore symlinks")
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValidationError(f"unsafe run snapshot path: {relative!r}")
        for relative, payload in snapshot.items():
            destination = self.runs_root / relative
            self._ensure_real_parents(destination.parent)
            self._atomic_write_bytes(destination, payload)
        current = self.capture_runs_files()
        for relative in set(current) - set(snapshot):
            path = self.runs_root / relative
            if path.is_file() or path.is_symlink():
                path.unlink()

    def _ensure_real_parents(self, directory: Path) -> None:
        relative = directory.relative_to(self.runs_root)
        current = self.runs_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink() or current.exists() and not current.is_dir():
                current.unlink()
            current.mkdir(exist_ok=True)

    def append_event(self, run_id: str, event: Mapping[str, object]) -> None:
        payload = dict(event)
        payload.setdefault("timestamp", datetime.now(UTC).isoformat())
        path = self.artifact_path(run_id, "events.jsonl")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        RunStore._atomic_write_bytes(path, text.encode("utf-8"))

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
