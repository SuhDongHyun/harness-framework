from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
PLAN_STATUSES = frozenset({"draft", "approved"})
RUN_STATUSES = frozenset(
    {"draft", "approved", "running", "verifying", "completed", "failed", "blocked"}
)
STEP_STATUSES = frozenset(
    {"pending", "running", "verifying", "retrying", "completed", "failed", "blocked"}
)
STEP_OUTCOMES = frozenset({"completed", "failed", "blocked"})


class ValidationError(ValueError):
    """Raised when a harness contract is invalid."""


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


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _require_exact_keys(
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


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    return tuple(_require_string(item, f"{label} item") for item in value)


def _path_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    return tuple(validate_relative_path(item, f"{label} item") for item in value)


def _allowed_path_tuple(value: object) -> tuple[str, ...]:
    paths = _path_tuple(value, "allowed_paths")
    for path in paths:
        first = PurePosixPath(path).parts[0]
        if first in {".git", ".harness"}:
            raise ValidationError(
                f"allowed_paths cannot include controller metadata: {path!r}"
            )
    return paths


def _commands(value: object, label: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    commands: list[tuple[str, ...]] = []
    for index, command in enumerate(value):
        if not isinstance(command, list) or not command:
            raise ValidationError(f"{label}[{index}] must be a non-empty argv array")
        argv = tuple(
            _require_string(argument, f"{label}[{index}] argument")
            for argument in command
        )
        _validate_verification_command(argv, f"{label}[{index}]")
        commands.append(argv)
    return tuple(commands)


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


@dataclass(frozen=True)
class PlanStep:
    id: str
    name: str
    objective: str
    read_files: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    acceptance_commands: tuple[tuple[str, ...], ...]
    forbidden_changes: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: object) -> PlanStep:
        if not isinstance(data, Mapping):
            raise ValidationError("plan step must be an object")
        _require_exact_keys(
            data,
            {
                "id",
                "name",
                "objective",
                "read_files",
                "allowed_paths",
                "acceptance_commands",
                "forbidden_changes",
            },
        )
        acceptance_commands = _commands(
            data["acceptance_commands"], "acceptance_commands"
        )
        if not acceptance_commands:
            raise ValidationError(
                "each step must contain at least one acceptance command"
            )
        return cls(
            id=validate_id(data["id"], "step id"),
            name=validate_id(data["name"], "step name"),
            objective=_require_string(data["objective"], "step objective"),
            read_files=_path_tuple(data["read_files"], "read_files"),
            allowed_paths=_allowed_path_tuple(data["allowed_paths"]),
            acceptance_commands=acceptance_commands,
            forbidden_changes=_string_tuple(
                data["forbidden_changes"], "forbidden_changes"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "objective": self.objective,
            "read_files": list(self.read_files),
            "allowed_paths": list(self.allowed_paths),
            "acceptance_commands": [
                list(command) for command in self.acceptance_commands
            ],
            "forbidden_changes": list(self.forbidden_changes),
        }


@dataclass(frozen=True)
class Plan:
    version: int
    goal: str
    status: str
    steps: tuple[PlanStep, ...]
    final_acceptance_commands: tuple[tuple[str, ...], ...]

    @classmethod
    def from_dict(cls, data: object) -> Plan:
        if not isinstance(data, Mapping):
            raise ValidationError("plan must be an object")
        _require_exact_keys(
            data,
            {"version", "goal", "status", "steps", "final_acceptance_commands"},
        )
        if data["version"] != 1:
            raise ValidationError("plan version must be 1")
        status = data["status"]
        if status not in PLAN_STATUSES:
            raise ValidationError(f"invalid plan status: {status!r}")
        raw_steps = data["steps"]
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValidationError("plan steps must be a non-empty array")
        if len(raw_steps) > 100:
            raise ValidationError("plan steps must contain at most 100 items")
        steps = tuple(PlanStep.from_dict(step) for step in raw_steps)
        ids = [step.id for step in steps]
        if len(ids) != len(set(ids)):
            raise ValidationError("step ids must be unique")
        expected_ids = [f"step-{index:02d}" for index in range(len(steps))]
        if ids != expected_ids:
            raise ValidationError(
                f"step ids must be sequential: expected {expected_ids!r}"
            )
        return cls(
            version=1,
            goal=_require_string(data["goal"], "goal"),
            status=str(status),
            steps=steps,
            final_acceptance_commands=_commands(
                data["final_acceptance_commands"], "final_acceptance_commands"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "goal": self.goal,
            "status": self.status,
            "steps": [step.to_dict() for step in self.steps],
            "final_acceptance_commands": [
                list(command) for command in self.final_acceptance_commands
            ],
        }

    def with_status(self, status: str) -> Plan:
        if status not in PLAN_STATUSES:
            raise ValidationError(f"invalid plan status: {status!r}")
        return Plan(
            version=self.version,
            goal=self.goal,
            status=status,
            steps=self.steps,
            final_acceptance_commands=self.final_acceptance_commands,
        )

    def sha256(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class StepResult:
    outcome: str
    summary: str
    changed_files: tuple[str, ...]
    error_message: str | None
    blocked_reason: str | None
    required_action: str | None

    @classmethod
    def from_dict(cls, data: object) -> StepResult:
        if not isinstance(data, Mapping):
            raise ValidationError("step result must be an object")
        _require_exact_keys(
            data,
            {
                "outcome",
                "summary",
                "changed_files",
                "error_message",
                "blocked_reason",
                "required_action",
            },
        )
        outcome = data["outcome"]
        if outcome not in STEP_OUTCOMES:
            raise ValidationError(f"invalid step outcome: {outcome!r}")
        summary = data["summary"]
        if not isinstance(summary, str):
            raise ValidationError("step summary must be a string")
        return cls(
            outcome=str(outcome),
            summary=summary,
            changed_files=_path_tuple(data["changed_files"], "changed_files"),
            error_message=_optional_string(data["error_message"], "error_message"),
            blocked_reason=_optional_string(data["blocked_reason"], "blocked_reason"),
            required_action=_optional_string(
                data["required_action"], "required_action"
            ),
        )


@dataclass
class RunState:
    version: int
    run_id: str
    status: str
    plan_sha256: str | None
    approved_git_fingerprint: str | None
    current_step: str | None
    steps: list[dict[str, Any]]
    terminal_result: str | None
    blocked_reason: str | None
    required_action: str | None

    @classmethod
    def from_plan(cls, run_id: str, plan: Plan) -> RunState:
        validate_id(run_id, "run id")
        return cls(
            version=1,
            run_id=run_id,
            status="draft",
            plan_sha256=None,
            approved_git_fingerprint=None,
            current_step=None,
            steps=[
                {
                    "id": step.id,
                    "name": step.name,
                    "status": "pending",
                    "attempts": 0,
                    "summary": None,
                    "error": None,
                }
                for step in plan.steps
            ],
            terminal_result=None,
            blocked_reason=None,
            required_action=None,
        )

    @classmethod
    def from_dict(cls, data: object) -> RunState:
        if not isinstance(data, Mapping):
            raise ValidationError("run state must be an object")
        _require_exact_keys(
            data,
            {
                "version",
                "run_id",
                "status",
                "plan_sha256",
                "approved_git_fingerprint",
                "current_step",
                "steps",
                "terminal_result",
                "blocked_reason",
                "required_action",
            },
        )
        if data["version"] != 1:
            raise ValidationError("state version must be 1")
        run_id = validate_id(data["run_id"], "run id")
        status = data["status"]
        if status not in RUN_STATUSES:
            raise ValidationError(f"invalid run status: {status!r}")
        raw_steps = data["steps"]
        if not isinstance(raw_steps, list):
            raise ValidationError("state steps must be an array")
        steps: list[dict[str, Any]] = []
        for raw in raw_steps:
            if not isinstance(raw, Mapping):
                raise ValidationError("state step must be an object")
            _require_exact_keys(
                raw, {"id", "name", "status", "attempts", "summary", "error"}
            )
            step_status = raw["status"]
            if step_status not in STEP_STATUSES:
                raise ValidationError(f"invalid step status: {step_status!r}")
            attempts = raw["attempts"]
            if (
                not isinstance(attempts, int)
                or isinstance(attempts, bool)
                or attempts < 0
            ):
                raise ValidationError("step attempts must be a non-negative integer")
            validate_id(raw["id"], "state step id")
            validate_id(raw["name"], "state step name")
            _optional_string(raw["summary"], "state step summary")
            _optional_string(raw["error"], "state step error")
            steps.append(dict(raw))
        return cls(
            version=1,
            run_id=run_id,
            status=str(status),
            plan_sha256=_optional_string(data["plan_sha256"], "plan_sha256"),
            approved_git_fingerprint=_optional_string(
                data["approved_git_fingerprint"], "approved_git_fingerprint"
            ),
            current_step=_optional_id(data["current_step"], "current_step"),
            steps=steps,
            terminal_result=_terminal_result(data["terminal_result"]),
            blocked_reason=_optional_string(data["blocked_reason"], "blocked_reason"),
            required_action=_optional_string(
                data["required_action"], "required_action"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "status": self.status,
            "plan_sha256": self.plan_sha256,
            "approved_git_fingerprint": self.approved_git_fingerprint,
            "current_step": self.current_step,
            "steps": self.steps,
            "terminal_result": self.terminal_result,
            "blocked_reason": self.blocked_reason,
            "required_action": self.required_action,
        }

    def step(self, step_id: str) -> dict[str, Any]:
        for step in self.steps:
            if step["id"] == step_id:
                return step
        raise ValidationError(f"state does not contain step {step_id!r}")


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string or null")
    return value


def _optional_id(value: object, label: str) -> str | None:
    if value is None:
        return None
    return validate_id(value, label)


def _terminal_result(value: object) -> str | None:
    if value is None:
        return None
    if value not in {"completed", "failed", "blocked"}:
        raise ValidationError(f"invalid terminal_result: {value!r}")
    return str(value)
