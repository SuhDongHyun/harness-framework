from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ValidationError
from .validation import (
    allowed_path_tuple,
    commands,
    path_tuple,
    require_exact_keys,
    require_string,
    string_tuple,
    validate_id,
)

PLAN_STATUSES = frozenset({"draft", "approved"})


@dataclass(frozen=True)
class PlanStep:
    id: str
    name: str
    depends_on: tuple[str, ...]
    objective: str
    read_files: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    acceptance_commands: tuple[tuple[str, ...], ...]
    forbidden_changes: tuple[str, ...]
    network_access: bool | None = None

    @classmethod
    def from_dict(cls, data: object) -> PlanStep:
        if not isinstance(data, Mapping):
            raise ValidationError("plan step must be an object")
        require_exact_keys(
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
            {"depends_on", "network_access"},
        )
        network_access: bool | None = None
        if "network_access" in data:
            raw_network_access = data["network_access"]
            if not isinstance(raw_network_access, bool):
                raise ValidationError("network_access must be boolean")
            network_access = raw_network_access
        acceptance_commands = commands(
            data["acceptance_commands"], "acceptance_commands"
        )
        if not acceptance_commands:
            raise ValidationError(
                "each step must contain at least one acceptance command"
            )
        return cls(
            id=validate_id(data["id"], "step id"),
            name=validate_id(data["name"], "step name"),
            depends_on=tuple(
                validate_id(value, "dependency id")
                for value in string_tuple(data.get("depends_on", []), "depends_on")
            ),
            objective=require_string(data["objective"], "step objective"),
            read_files=path_tuple(data["read_files"], "read_files"),
            allowed_paths=allowed_path_tuple(data["allowed_paths"]),
            acceptance_commands=acceptance_commands,
            forbidden_changes=string_tuple(
                data["forbidden_changes"], "forbidden_changes"
            ),
            network_access=network_access,
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "depends_on": list(self.depends_on),
            "objective": self.objective,
            "read_files": list(self.read_files),
            "allowed_paths": list(self.allowed_paths),
            "acceptance_commands": [
                list(command) for command in self.acceptance_commands
            ],
            "forbidden_changes": list(self.forbidden_changes),
        }
        if self.network_access is not None:
            payload["network_access"] = self.network_access
        return payload


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
        require_exact_keys(
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
        prior_ids: set[str] = set()
        for step in steps:
            if len(step.depends_on) != len(set(step.depends_on)):
                raise ValidationError(f"step {step.id} dependencies must be unique")
            unknown = set(step.depends_on) - prior_ids
            if unknown:
                raise ValidationError(
                    f"step {step.id} dependencies must reference earlier steps: "
                    + ", ".join(sorted(unknown))
                )
            prior_ids.add(step.id)
        return cls(
            version=1,
            goal=require_string(data["goal"], "goal"),
            status=str(status),
            steps=steps,
            final_acceptance_commands=commands(
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
