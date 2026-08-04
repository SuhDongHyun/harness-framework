from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath

from .errors import ValidationError

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
RUN_STATUSES = frozenset(
    {"draft", "approved", "running", "verifying", "completed", "failed", "blocked"}
)
STEP_STATUSES = frozenset(
    {"pending", "running", "verifying", "retrying", "completed", "failed", "blocked"}
)


def validate_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValidationError(f"{label} must match {ID_PATTERN.pattern}: {value!r}")
    return value


def validate_relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValidationError(f"{label} must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValidationError(f"{label} must stay inside the repository: {value!r}")
    return value


def require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    return value.strip()


def require_exact_keys(
    data: Mapping[str, object],
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = required_set - set(data)
    extra = set(data) - allowed
    if missing:
        raise ValidationError(f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise ValidationError(f"unknown fields: {', '.join(sorted(extra))}")


def string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    return tuple(require_string(item, f"{label} item") for item in value)


def path_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    return tuple(validate_relative_path(item, f"{label} item") for item in value)


def allowed_path_tuple(value: object) -> tuple[str, ...]:
    paths = path_tuple(value, "allowed_paths")
    for path in paths:
        first = PurePosixPath(path).parts[0]
        if first in {".git", ".harness"}:
            raise ValidationError(
                f"allowed_paths cannot include controller metadata: {path!r}"
            )
    return paths


def commands(value: object, label: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    parsed: list[tuple[str, ...]] = []
    for index, command in enumerate(value):
        if not isinstance(command, list) or not command:
            raise ValidationError(f"{label}[{index}] must be a non-empty argv array")
        argv = tuple(
            require_string(argument, f"{label}[{index}] argument")
            for argument in command
        )
        _validate_verification_command(argv, f"{label}[{index}]")
        parsed.append(argv)
    return tuple(parsed)


def optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string or null")
    return value


def optional_id(value: object, label: str) -> str | None:
    if value is None:
        return None
    return validate_id(value, label)


def terminal_result(value: object) -> str | None:
    if value is None:
        return None
    if value not in {"completed", "failed", "blocked"}:
        raise ValidationError(f"invalid terminal_result: {value!r}")
    return str(value)


def _validate_verification_command(argv: tuple[str, ...], label: str) -> None:
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable in {
        "bash",
        "cmd",
        "cmd.exe",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "pwsh",
        "sh",
        "zsh",
    }:
        raise ValidationError(f"{label} cannot invoke a command shell")
    if executable in {
        "chmod",
        "chown",
        "dd",
        "mv",
        "rm",
        "rmdir",
        "truncate",
        "unlink",
    }:
        raise ValidationError(
            f"{label} cannot invoke destructive executable {executable!r}"
        )
    if executable in {"python", "python3", "node", "ruby", "perl"} and any(
        argument in {"-c", "-e", "--eval"} for argument in argv[1:]
    ):
        raise ValidationError(f"{label} cannot execute inline interpreter code")
    if executable in {"git", "git.exe"}:
        allowed = {
            "diff",
            "grep",
            "log",
            "ls-files",
            "rev-parse",
            "show",
            "status",
        }
        subcommand = _git_subcommand(argv[1:])
        if subcommand not in allowed:
            raise ValidationError(
                f"{label} cannot invoke mutating or unknown Git command {subcommand!r}"
            )


def _git_subcommand(arguments: tuple[str, ...]) -> str | None:
    options_with_value = {
        "-C",
        "-c",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            break
        option_name = argument.split("=", 1)[0]
        if option_name in options_with_value and "=" not in argument:
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return argument
    return arguments[index] if index < len(arguments) else None
