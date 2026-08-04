from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..domain.errors import ValidationError
from ..domain.validation import validate_relative_path

MAX_PARALLEL_FILE_BYTES = 50_000_000


@dataclass(frozen=True)
class WorkspaceChange:
    path: str
    content: bytes
    mode: int


def copy_repository(source: Path, destination: Path) -> None:
    source = source.resolve()
    git_path = source / ".git"
    if not git_path.is_dir() or git_path.is_symlink():
        raise ValidationError(
            "parallel writers require a repository with a real .git directory"
        )

    def ignore(directory: str, names: list[str]) -> set[str]:
        current = Path(directory).resolve()
        ignored = {"__pycache__", ".ruff_cache"} & set(names)
        if current == source / ".harness" and "runs" in names:
            ignored.add("runs")
        return ignored

    shutil.copytree(source, destination, symlinks=True, ignore=ignore)


def collect_workspace_changes(
    workspace: Path, changed_paths: set[str]
) -> tuple[WorkspaceChange, ...]:
    changes: list[WorkspaceChange] = []
    root = workspace.resolve()
    for relative in sorted(changed_paths):
        validate_relative_path(relative, "parallel workspace change")
        path = root.joinpath(*PurePosixPath(relative).parts)
        _ensure_safe_parents(root, path.parent)
        if not path.exists():
            raise ValidationError(
                f"parallel writers do not support file deletion: {relative}"
            )
        if path.is_symlink() or not path.is_file():
            raise ValidationError(
                f"parallel writers support regular files only: {relative}"
            )
        if path.stat().st_size > MAX_PARALLEL_FILE_BYTES:
            raise ValidationError(f"parallel writer file is too large: {relative}")
        changes.append(
            WorkspaceChange(
                path=relative,
                content=path.read_bytes(),
                mode=stat.S_IMODE(path.stat().st_mode),
            )
        )
    return tuple(changes)


def apply_workspace_changes(root: Path, changes: tuple[WorkspaceChange, ...]) -> None:
    resolved_root = root.resolve()
    for change in changes:
        validate_relative_path(change.path, "parallel workspace change")
        destination = resolved_root.joinpath(*PurePosixPath(change.path).parts)
        _ensure_safe_parents(resolved_root, destination.parent)
        if destination.is_symlink():
            raise ValidationError(
                f"parallel writer cannot replace a symlink: {change.path}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".parallel.tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(change.content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, change.mode)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def allowed_paths_overlap(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    return any(_patterns_overlap(left, right) for left in first for right in second)


def _patterns_overlap(first: str, second: str) -> bool:
    left = _static_prefix(first)
    right = _static_prefix(second)
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _static_prefix(pattern: str) -> str:
    parts = []
    for part in PurePosixPath(pattern).parts:
        if any(character in part for character in "*?["):
            break
        parts.append(part)
    return "/".join(parts)


def _ensure_safe_parents(root: Path, parent: Path) -> None:
    try:
        relative = parent.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise ValidationError("parallel writer path escaped workspace") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValidationError(
                f"parallel writer parent must not be a symlink: {current}"
            )
