#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
import tomllib
from pathlib import Path

TABLE_PATTERN = re.compile(
    r"^\s*\[\s*sandbox_workspace_write\s*\]\s*(?:#.*)?$"
)
HEADER_PATTERN = re.compile(r"^\s*\[")
ROOTS_PATTERN = re.compile(r"^\s*writable_roots\s*=")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge the Harness runtime home into Codex writable_roots."
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    return parser.parse_args()


def default_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return base / "config.toml"


def format_roots(values: list[str]) -> list[str]:
    lines = ["writable_roots = [\n"]
    lines.extend(f"  {json.dumps(value, ensure_ascii=False)},\n" for value in values)
    lines.append("]\n")
    return lines


def array_assignment_end(lines: list[str], start: int) -> int:
    depth = 0
    saw_array = False
    quote: str | None = None
    escaped = False
    for index in range(start, len(lines)):
        comment = False
        segment = lines[index].split("=", 1)[1] if index == start else lines[index]
        for character in segment:
            if comment:
                continue
            if quote is not None:
                if quote == '"' and escaped:
                    escaped = False
                elif quote == '"' and character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
                continue
            if character == "#":
                comment = True
            elif character in {'"', "'"}:
                quote = character
            elif character == "[":
                saw_array = True
                depth += 1
            elif character == "]":
                depth -= 1
                if saw_array and depth == 0:
                    return index + 1
        if quote is not None:
            raise ValueError("multiline strings are not supported in writable_roots")
    raise ValueError("could not find the end of writable_roots")


def merge(content: str, root: str) -> tuple[str, bool]:
    try:
        data = tomllib.loads(content) if content.strip() else {}
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"existing Codex config is invalid TOML: {error}") from error
    table = data.get("sandbox_workspace_write", {})
    if not isinstance(table, dict):
        raise ValueError("sandbox_workspace_write must be a TOML table")
    existing = table.get("writable_roots", [])
    if not isinstance(existing, list) or any(
        not isinstance(value, str) for value in existing
    ):
        raise ValueError("sandbox_workspace_write.writable_roots must be a string array")
    if root in existing:
        return content, False

    desired = [*existing, root]
    lines = content.splitlines(keepends=True)
    table_start = next(
        (index for index, line in enumerate(lines) if TABLE_PATTERN.match(line)),
        None,
    )
    if table_start is None:
        if content and not content.endswith("\n"):
            lines.append("\n")
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend(["[sandbox_workspace_write]\n", *format_roots(desired)])
    else:
        table_end = next(
            (
                index
                for index in range(table_start + 1, len(lines))
                if HEADER_PATTERN.match(lines[index])
            ),
            len(lines),
        )
        roots_start = next(
            (
                index
                for index in range(table_start + 1, table_end)
                if ROOTS_PATTERN.match(lines[index])
            ),
            None,
        )
        if roots_start is None:
            lines[table_end:table_end] = format_roots(desired)
        else:
            roots_end = array_assignment_end(lines, roots_start)
            lines[roots_start:roots_end] = format_roots(desired)

    updated = "".join(lines)
    verified = tomllib.loads(updated)["sandbox_workspace_write"]["writable_roots"]
    if verified != desired:
        raise ValueError("updated writable_roots did not verify")
    return updated, True


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise OSError(f"Codex config directory must not be a symlink: {path.parent}")
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise OSError(f"Codex config must be a regular file: {path}")
        if path.stat().st_uid != os.getuid():
            raise PermissionError(f"Codex config must be owned by this user: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
    else:
        mode = 0o600
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    args = parse_args()
    if not sys.platform.startswith("linux"):
        raise SystemExit("harness-setup supports Linux and WSL only")
    root = args.root.expanduser()
    if not root.is_absolute():
        raise SystemExit("--root must be an absolute path")
    root = root.absolute()
    config = (args.config or default_config_path()).expanduser().absolute()
    if config.parent == root:
        raise SystemExit("refusing to edit the dedicated Harness Codex home")
    content = config.read_text(encoding="utf-8") if config.exists() else ""
    try:
        updated, changed = merge(content, str(root))
        if changed:
            atomic_write(config, updated)
    except (OSError, PermissionError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(
        json.dumps(
            {"config": str(config), "root": str(root), "changed": changed},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
